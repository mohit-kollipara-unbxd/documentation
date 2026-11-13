# Mimir Downstream HTTP Client — Latency Analysis & Recommended Configuration

**Context:** Peak-hour (11:00–14:00 IST) latency spikes observed at mimir. Search database and all 16 downstream microservices reported healthy server-side latency, but mimir's client-side view of every downstream call was slow. Restarting mimir and adding a replica resolved the incident.

**Conclusion:** The root cause is client-side resource exhaustion inside mimir — not the downstreams. The HTTP client code (`downstream/client.go`) has several issues that produce exactly this signature.

---

## 1. Symptom → Cause Mapping

| Observation | What it tells us |
|---|---|
| Downstream server-side latency fine, client-side latency high | Time is spent **before** the request reaches the downstream: connection setup, pool wait, or CPU starvation in mimir |
| Only happens at peak QPS (11 AM–2 PM) | A resource threshold is being crossed (connection pool, FDs, CPU) |
| Restart fixed it | Leaked resources (connections, goroutines, FDs) accumulated over time |
| Adding a replica also helped | mimir was CPU-bound at peak (likely CFS throttling) |

## 2. Issues in the Current Client Code

### 2.1 Default transport → connection pool starvation (primary suspect)

`httpclient.NewClient()` without `WithHTTPClient(...)` uses Go's `http.DefaultTransport` settings, where **`MaxIdleConnsPerHost = 2`**.

At peak, mimir fans out to 16 downstreams per search request. With only 2 idle connections kept per host, every request beyond that opens a fresh TCP connection: dial + handshake, plus `TIME_WAIT` buildup and ephemeral port pressure. The downstream executes fast once the request arrives — the delay is entirely client-side, which is exactly what the metrics showed.

### 2.2 Client instantiation pattern

If `NewHTTPClient` is called per-request rather than once per downstream at startup, there is zero connection reuse and each call creates a new transport with its own pool. This leaks idle connections and file descriptors over time — consistent with "restart fixed it."

**Action:** Construct one client per downstream at boot; reuse for the process lifetime.

### 2.3 Unclosed / undrained response bodies

Any call path that does not fully drain and close `resp.Body` prevents the connection from returning to the pool. Audit every call site:

```go
defer func() {
    io.Copy(io.Discard, resp.Body)
    resp.Body.Close()
}()
```

### 2.4 Retry amplification

`WithRetryCount(n)` without a configured retrier makes heimdall retry **immediately with no backoff**. Under stress, every slow call multiplies into 1+n calls against an already-starved pool — a self-sustaining retry storm. With 16 downstreams, 1 retry each means one search request can become up to 32 downstream calls.

### 2.5 Silent timeout bug

```go
func WithTimeout(timeout time.Duration) ClientOption {
    return func(hc *httpClient) {
        if timeout != defaultTimeout {   // BUG: silently skips applying the timeout
            ...
        }
    }
}
```

Callers passing the default get heimdall's built-in 30s timeout instead. Slow calls then hold goroutines and connections for up to 30s, accelerating the pileup. **Remove the guard — apply the timeout unconditionally.**

### 2.6 CPU saturation / CFS throttling

In Kubernetes, Go sees the **node's** core count, not the pod's 3-CPU limit, so it spawns too many OS threads and gets CFS-throttled. Throttling makes everything in the process appear slow — including client-side measurement of downstream calls.

---

## 3. Recommended Configuration (16 downstreams, 3 CPUs)

### 3.1 Sizing formula

```
concurrent conns per downstream ≈ peak QPS on mimir × avg downstream latency (sec)
```

Example: 500 QPS × 50ms = ~25 concurrent connections per downstream. The values below assume ~4× headroom on that; adjust to actual measured QPS and p95.

### 3.2 Transport — one shared transport, one client per downstream

```go
func newTransport() *http.Transport {
    return &http.Transport{
        DialContext: (&net.Dialer{
            Timeout:   1 * time.Second,   // fail fast on dial
            KeepAlive: 30 * time.Second,
        }).DialContext,
        MaxIdleConns:          1600,      // 16 hosts × 100
        MaxIdleConnsPerHost:   100,       // the critical fix (default is 2)
        MaxConnsPerHost:       0,         // unlimited; cap via semaphore if needed
        IdleConnTimeout:       90 * time.Second,
        TLSHandshakeTimeout:   2 * time.Second,
        ForceAttemptHTTP2:     false,     // keep HTTP/1.1 unless downstreams are h2
    }
}
```

Key points:

- **`MaxIdleConnsPerHost: 100`** eliminates connection churn. Idle conns cost a few KB each — negligible.
- **`MaxIdleConns` must be ≥ 16 × `MaxIdleConnsPerHost`.** If the global cap is lower, it silently evicts per-host idle connections and churn returns. Common misconfiguration.
- **One shared `http.Transport`** across all 16 clients (the pool is keyed by host internally), but **one heimdall client per downstream** so each gets its own timeout/retry policy.

### 3.3 Updated client constructor

```go
func NewHTTPClient(opts ...ClientOption) HTTPClient {
    hc := &httpClient{
        heimdallOptions: []httpclient.Option{
            httpclient.WithHTTPClient(&http.Client{Transport: sharedTransport}),
        },
    }
    for _, opt := range opts {
        opt(hc)
    }
    hc.Client = httpclient.NewClient(hc.heimdallOptions...)
    return hc
}
```

```go
// WithTimeout — apply unconditionally (remove the != defaultTimeout guard)
func WithTimeout(timeout time.Duration) ClientOption {
    return func(hc *httpClient) {
        hc.heimdallOptions = append(hc.heimdallOptions,
            httpclient.WithHTTPTimeout(timeout))
    }
}
```

### 3.4 Runtime — 3 CPUs

- **Set `GOMAXPROCS` to the CPU limit.** Use `go.uber.org/automaxprocs` (one import) or `GOMAXPROCS=3` in the pod spec. Without this, CFS throttling at peak is near-guaranteed.
- **Fan out to the 16 downstreams in parallel** (`errgroup` / `WaitGroup`). Sequential calls put the latency floor at 16 × avg-latency.
- CPU at peak should stay ≤ 60% per pod. JSON encode/decode of 16 responses per request is the main CPU consumer; beyond ~60–70%, add replicas rather than tune further.

### 3.5 Budgets — timeouts and retries per downstream

Work backwards from the customer-facing search SLA (e.g., p95 ≈ 800ms) with parallel fan-out:

```go
nerClient   := NewHTTPClient(WithTimeout(300*time.Millisecond), WithRetry(1))
qcpClient   := NewHTTPClient(WithTimeout(300*time.Millisecond), WithRetry(1))
merchClient := NewHTTPClient(WithTimeout(500*time.Millisecond)) // no retry if non-critical
```

- **Per-downstream timeout: 300–500ms**, never the 30s heimdall default. A slow downstream should degrade one feature, not hold resources hostage.
- **At most 1 retry, with backoff, only for idempotent calls:**

```go
backoff := heimdall.NewConstantBackoff(20*time.Millisecond, 50*time.Millisecond)
retrier := heimdall.NewRetrier(backoff)
// httpclient.WithRetrier(retrier), httpclient.WithRetryCount(1)
```

- Consider a **hystrix circuit breaker per downstream** (`hystrix.NewClient`) so a degraded downstream gets cut off instead of retried.
- Define **graceful degradation semantics**: NER or synonyms timing out should mean "search without that enrichment," not a failed search. This matters more to p95 than any transport knob.

### 3.6 Replica / HPA guidance

With 16-way fan-out per request, each pod does ~17× the work of a simple proxy. Set HPA on CPU at a **55–60% target** so scaling happens during the 11 AM ramp, not after latency alarms. Yesterday's incident (one extra replica resolving it) indicates the fleet was right at the edge.

---

## 4. Verification Plan (before next peak)

Add these to the mimir Datadog dashboard and watch from ~10:30 AST onward:

1. **Goroutine count** — monotonic climb from morning = leak (body-close / per-request client).
2. **Open file descriptors** — same signal.
3. **TCP connections per downstream host** — high `TIME_WAIT` counts = connection churn (pool too small).
4. **`container.cpu.throttled`** — spikes at peak = CFS throttling (`GOMAXPROCS` / replica fix).
5. **Client-side vs server-side latency per downstream** — the gap between the two is the connection/CPU overhead being eliminated.

## 5. Rollout Checklist

- [ ] Add shared `http.Transport` with `MaxIdleConnsPerHost: 100`, `MaxIdleConns: 1600`
- [ ] Ensure `NewHTTPClient` is called once per downstream at startup (16 singletons)
- [ ] Remove `timeout != defaultTimeout` guard in `WithTimeout`
- [ ] Set explicit 300–500ms timeouts per downstream
- [ ] Add `heimdall.NewRetrier` with backoff; cap retries at 1, idempotent calls only
- [ ] Audit all call sites for drain + `resp.Body.Close()`
- [ ] Add `automaxprocs` / set `GOMAXPROCS=3`
- [ ] Confirm parallel fan-out to downstreams
- [ ] HPA target CPU 55–60%
- [ ] Dashboard: goroutines, FDs, TCP states, CPU throttling, client-vs-server latency gap

---

*Open inputs needed to finalize numbers: mimir peak QPS and per-downstream p95 latency. These convert the placeholder values (100 idle conns/host, 300ms timeouts) into exact figures.*