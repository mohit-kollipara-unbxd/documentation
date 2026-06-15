# Reranker — Personalization & Recommendations Architecture & Reference

> **One-line summary:** On branch **v2**, reranker is a **Go** personalization service (`goreranker`) that re-scores search result lists (`/rerank`), serves recommendations (`/recommend`), and exposes user events (`/events`) — with a legacy **Python** container (`pyreranker`) still deployed in K8s for unmigrated traffic, routed by an Envoy sidecar.

---

## Table of Contents

1. [v2 Branch & Go Migration](#v2-branch--go-migration)
2. [Architecture Overview](#architecture-overview)
3. [Envoy Routing (goreranker vs pyreranker)](#envoy-routing-goreranker-vs-pyreranker)
4. [Components](#components)
5. [API Reference](#api-reference)
6. [Exact cURL Commands](#exact-curl-commands)
7. [Core Business Logic](#core-business-logic)
8. [Config System (Albus / Configstore)](#config-system-albus--configstore)
9. [Data Storage](#data-storage)
10. [Background Jobs / Workers](#background-jobs--workers)
11. [APAC vs US vs EU](#apac-vs-us-vs-eu)
12. [External Dependencies](#external-dependencies)
13. [Environment Variables](#environment-variables)
14. [Deployment (Helm / Kubernetes)](#deployment-helm--kubernetes)
15. [Common Failure Modes & Debugging](#common-failure-modes--debugging)

---

## v2 Branch & Go Migration

**Document scope:** branch `v2` (current production migration path). The Python `app/` tree was removed from this branch — all API logic lives under `cmd/reranker/` and `internal/`.

| Aspect | v2 (Go) | Legacy (Python) |
|---|---|---|
| **Source in repo** | `cmd/reranker/`, `internal/` | Removed from v2; old code on `master` |
| **K8s container name** | `go-reranker` | `py-reranker` |
| **Envoy cluster** | `goreranker` → `127.0.0.1:{go.port}` (5005) | `pyreranker` → `127.0.0.1:{py.port}` (5004) |
| **Image tag (example)** | `v1.3.8` (GCP pilot) / `v1.3.4` (AWS) | `v0.19.1` (frozen legacy image) |
| **Local docker-compose** | Go binary on `:5004` only | Not started locally on v2 |
| **Health probe** | `GET /monitor` | TCP socket on :5004 |

Migration is incremental: sites are moved to Go by adding their path regex to `sidecar.goRoutes` (or `goRoutesSemantic` / `goRoutesUnbxd`) in the region's Helm values. Everything else still hits Python via the Envoy **default route**.

**v2-only features in Go:** Aragorn vector search path, CTL widgets, image search (Jarvis/Vision), HEIC/HEIF transcoding (`utils/heif_transcode.go`, requires `libheif-examples` in the Docker image).

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         UPSTREAM (Search / Recs UI)                          │
│                                                                              │
│  Search API / Recs widgets  ──▶  POST /rerank   (re-score result list)      │
│                               ──▶  GET/POST /recommend  (fetch recs)         │
│                               ──▶  GET /events  (user clickstream)           │
└──────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│              ENVOY SIDECAR (K8s service → targetPort sidecar.port)           │
│                                                                              │
│  Route precedence (first match wins):                                        │
│    goRoutesSemantic + campaignId  ──▶  goreranker  (Go :5005)               │
│    goRoutesUnbxd + platform=unbxd ──▶  goreranker                           │
│    goRoutes (site regex)          ──▶  goreranker                           │
│    pyRoutesImageSearch            ──▶  pyreranker  (Python :5004)           │
│    default ^.*                    ──▶  pyreranker  (legacy catch-all)      │
│                                                                              │
│  goreranker = Go binary (all logic in internal/* on v2 branch)              │
│  pyreranker = legacy Python image (v0.19.1), not built from v2 source       │
└──────────────────────────────────────────────────────────────────────────────┘
          │
          ├─▶ Albus/Configstore  ── deployments + sibling_sites per handler
          ├─▶ Aerospike          ── user events, topsellers, category topsellers
          ├─▶ Olympus            ── realtime features / events (platform=olympus)
          ├─▶ AI Pipeline        ── Kubeflow inference (rerank, recs, embeddings)
          ├─▶ Hodor Docstore     ── product filter / post-process
          ├─▶ Redis (bundled)    ── Jarvis/Vision cache, CTL maps, QS, embeddings
          ├─▶ Aragorn            ── vector search (semantic_search + enabled flag)
          ├─▶ Mimir              ── category lookup (CTL widgets)
          ├─▶ Jarvis / Vision    ── image attribution / object detection
          └─▶ OpenAI / Asterix   ── query suggestions (semantic search)
```

**Where reranker sits:** Downstream of search indexing (Mimir/Solr) and upstream feature materialization (Olympus consumer, Aerospike batch jobs). Search passes a candidate product list + query + userId to `/rerank`; recommendation widgets call `/recommend` with an algo (`vav`, `rfu`, `top-sellers`, `semantic_search`, etc.).

---

## Envoy Routing (goreranker vs pyreranker)

Config: `helm/reranker/templates/configmap.yaml`. Clusters are loopback — both containers run in the same pod.

| Priority | Match | Query params | Cluster |
|---|---|---|---|
| 1 | Regex from `sidecar.goRoutesSemantic` | `platform=semantic_search`, `campaignId={exact}` | **goreranker** |
| 2 | `^/v2.0/sites/.*/recommend` | `platform=semantic_search` | **pyreranker** |
| 3 | Regex from `sidecar.goRoutesUnbxd` | `platform=unbxd` | **goreranker** |
| 4 | Regex from `sidecar.pyRoutesImageSearch` | `platform=image_search` | **pyreranker** |
| 5 | Regex from `sidecar.imgApacAzaRedirect` | `platform=image_search` | **apac_reranker** (external DNS rewrite to APAC host) |
| 6 | `^/v2.0/sites/.*/recommend` | `platform=unbxd` | **pyreranker** |
| 7 | Regex from `sidecar.goRoutes` | *(none)* | **goreranker** |
| 8 | `^.*` | *(none)* | **pyreranker** (default) |

**Important routing caveats:**

1. **Default is Python** — unless a site is listed in `goRoutes` (or matches semantic/unbxd rules above), traffic goes to `pyreranker`.
2. **Envoy matches `query_parameters`, not JSON body** — for `POST /v2.0/.../recommend`, `platform` in the body alone does **not** affect Envoy routing. Only a matching `goRoutes` site regex (or adding `?platform=...` to the URL) steers to Go.
3. **`/rerank` and `/events`** — only reach Go if the site path matches `goRoutes` or there is no Python handler (rerank is Go-only in source; production still depends on Envoy listing the site).
4. **US `goRoutesUnbxd` is often empty** — `platform=unbxd` on v2 recommend routes to Python unless the site is in `goRoutes`.

**Direct access (bypass Envoy):** Port-forward the Go container and call `:5005` to test Go behavior regardless of routing rules.

```bash
kubectl port-forward pod/<reranker-pod> 5005:5005
curl -sS "http://localhost:5005/monitor"
```

---

## Components

| Component | Path | Purpose |
|---|---|---|
| **Go API (`goreranker`)** | `cmd/reranker/` | CLI flags, dependency wiring, HTTP on `HTTP_PORT` (5005 in K8s) |
| **Rerank module** | `internal/rerank/` | `POST /v1.0/sites/:sitekey/rerank` — deeprec + netcore strategies |
| **Recommend module** | `internal/recommend/` | `GET /v1.0/.../recommend`, `POST /v2.0/.../recommend` |
| **Events module** | `internal/events/` | `GET /v1.0/sites/:sitekey/events` |
| **Downstreams** | `internal/downstreams/` | Albus, Aerospike, Redis cache, AI Pipeline, Docstore, Olympus, Jarvis, Vision, Mimir, Aragorn, query suggestions |
| **Image utils** | `utils/heif_transcode.go`, `utils/helper.go` | HEIC/HEIF → JPEG via `heif-convert` for base64 image_search |
| **Python app (`pyreranker`)** | *not in v2 source* | Legacy `v0.19.1` image still deployed; Gunicorn on :5004; catch-all via Envoy |
| **Envoy sidecar** | `helm/reranker/templates/configmap.yaml` | Routes to `goreranker` or `pyreranker` clusters |
| **Helm chart** | `helm/reranker/` | 3-container pod: Go + Python + Envoy; bundled Redis sub-chart |
| **Local fakers** | `scripts/faker/` | Mock Albus, AI Pipeline, Docstore, Jarvis, Olympus, Aragorn |
| **Seed scripts** | `scripts/aerospike.sh`, `scripts/redis.sh` | Local Aerospike + Redis test data |
| **Postman tests** | `scripts/tests/*.postman_collection.json` | API contract examples |

---

## API Reference

Base URL (internal K8s): `http://reranker/` or `http://reranker.prod.<region>.infra/`  
Local docker-compose (v2): `http://localhost:5004` — **Go only**, no Envoy/Python  
K8s: Service port `80` → Envoy `sidecar.port` (5010) → `goreranker` (:5005) or `pyreranker` (:5004)

Error envelope (all APIs):

```json
{ "error": "<message>" }
```

HTTP status: `400` for validation errors, `500` for downstream / internal failures.

---

### `GET /monitor` and `GET /pong`

Health check endpoints (configured via `HTTP_MONITOR`, default both paths).

**Response:** HTTP 200 (body from go-base monitor handler).

---

### `POST /v1.0/sites/{sitekey}/rerank`

Re-rank a list of product IDs for a user + query using personalization models.

**Path params:**

| Param | Description |
|---|---|
| `sitekey` | Site identifier (e.g. `test_site`, `ss-unbxd-prod-eng-thailand13731636957760`) |

**Request body (JSON):**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `userId` | string | Yes* | — | User identifier |
| `netcoreId` | string | No | — | If set, overrides `userId` and uses Netcore Aerospike set |
| `query` | string | No | `""` | Search query string |
| `platform` | string | No | `""` → deeprec | `unbxd` (deeprec) or `netcore` |
| `campaignId` | string | No | `""` | Albus deployment id; empty → first deployment |
| `products` | string[] | Yes | — | Candidate product IDs to rerank |
| `deviceType` | string | No | `""` | Device hint passed to model |
| `count` | int | No | `len(products)` | Max products to return |
| `norm` | float64 | No | `40` | Score normalization ceiling |
| `debug` | bool | No | `false` | Include debug block in response |
| `rankingContext` | object | No | — | Pins, slots, boosts (see below) |

\* `userId` not required when `platform=semantic_search`.

**`rankingContext`:** `pins` and `slots` are JSON **strings** (double-encoded) in the wire format; `boosts` is a native JSON array.

```json
{
  "pins": "{\"pins\":[{\"uniqueId\":\"2MD01251\",\"position\":1}]}",
  "slots": "{\"slots\":[{\"start\":3,\"end\":5}]}",
  "boosts": [{"uniqueId": "ABG02272", "score": 71.45}]
}
```

**Success response:**

```json
{
  "userId": "uid-1212",
  "products": [
    { "uniqueId": "ABG02272", "score": 39.5, "original_score": 8.0 }
  ],
  "msTaken": 42,
  "debug": null
}
```

With `debug=true`, `debug` contains `params`, `products`, `interactions`, `msTaken`.

---

### `GET /v1.0/sites/{sitekey}/recommend`

Recommend products (query-string API, v1).

**Path params:** `sitekey`

**Query params:**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `userId` | string | Yes* | — | User ID |
| `netcoreId` | string | No | — | Overrides `userId`; uses Netcore event anchor |
| `platform` | string | No | `unbxd` | `unbxd`, `netcore`, `semantic_search`, `image_search`, `ctl` |
| `query` | string | No | `""` | Query string |
| `algo` | string | No | — | `vav`, `bab`, `rfu`, `top-sellers`, `category-top-sellers`, `more-like-these` |
| `campaignId` | string | No | `""` | Albus deployment selector |
| `deviceType` | string | No | `""` | Device type |
| `norm` | float64 | No | `40` | Score normalization |
| `count` | int | No | `25` | Result count |
| `filters` | JSON string | No | — | Docstore filter object (URL-encoded JSON) |
| `boostedFilters` | JSON string | No | — | Boosted filter object |
| `pids` | JSON array string | No | `[]` | Anchor product IDs (e.g. `["214-SF-111"]`) |
| `categoryPaths` | JSON array string | No | `[]` | Required for `algo=category-top-sellers` |
| `debug` | bool | No | `false` | Debug trace |

\* Not required for `platform=semantic_search`.

**Success response:**

```json
{
  "userId": "uid-1212",
  "products": [
    { "uniqueId": "KB7211", "score": 38.2, "original_score": 0.95 }
  ],
  "query": "red shirt",
  "msTaken": 120,
  "debug": null
}
```

CTL platform may return `ctlWidgets` instead of `products`. Image search may return `boxes`, `bbid`, `tags`.

---

### `POST /v2.0/sites/{sitekey}/recommend`

Same logical API as v1 but parameters are in the JSON body. Preferred for complex `filters` / `boostedFilters`.

**Request body:** `internal/recommend/models.Request`

| Field | JSON key | Type | Default |
|---|---|---|---|
| Site key | — | path param | — |
| User ID | `userId` | string | — |
| Netcore ID | `netcoreId` | string | overrides `userId` |
| Query | `query` | string | — |
| Algo | `algo` | string | — |
| Platform | `platform` | string | — |
| Campaign | `campaignId` | string | — |
| Count | `count` | int | `25` |
| Norm | `norm` | float64 | `40` (v1 only; v2 does not re-default norm) |
| Filters | `filters` | object | — |
| Boosted filters | `boostedFilters` | object | — |
| Anchor PIDs | `pids` | string[] | — |
| Category paths | `categoryPaths` | string[] | required for CTS |
| Device type | `devicetype` | string | — |
| Image URL | `image_url` | string | image_search |
| Box ID | `boxId` | string | image_search |
| Source | `source` | string | `URL` or `base64` |
| Debug | `debug` | bool | — |
| Supporting cat count | `supportingCatCount` | int | CTL |
| Min token count | `min_token_count` | int | semantic |

---

### `GET /v1.0/sites/{sitekey}/events`

Fetch recent user behavioral events.

**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `userId` | string | required | User ID |
| `netcoreId` | string | — | Overrides `userId` |
| `eventType` | CSV | `CLICK,CART,ORDER` | Event types to return |
| `count` | int | `10` | Max events |
| `platform` | string | `aerospike` | `aerospike` or `olympus` |

**Success response:**

```json
{
  "events": [
    { "itemId": "ROX603-TH", "timestamp": 1615929597, "eventType": "CLICK" }
  ],
  "msTaken": 15
}
```

---

## Exact cURL Commands

Replace host and sitekey for your environment:

| Environment | Base URL |
|---|---|
| Local docker-compose | `http://localhost:5004` |
| US prod | `http://reranker.prod.use-1d.infra` |
| EU prod | `http://reranker.prod.eu-west-2.infra` |
| APAC prod | `http://reranker.prod.ap-southeast-1.infra` |

---

### Health check

```bash
curl -sS -X GET "http://localhost:5004/monitor"
```

```bash
curl -sS -X GET "http://localhost:5004/pong"
```

---

### Rerank — deeprec (unbxd platform)

```bash
curl -sS -X POST "http://localhost:5004/v1.0/sites/test_site/rerank" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "deviceType": "UNKNOWN",
    "norm": 40,
    "platform": "unbxd",
    "products": [
      "2MD01251",
      "2MD01260",
      "2MD01265",
      "5SA01306",
      "ABG02272",
      "ABG02478",
      "ABG02537",
      "ABG02546",
      "ABG02551",
      "ABG02555"
    ],
    "query": "red shirt",
    "userId": "uid-1212"
  }'
```

---

### Rerank — netcore platform

```bash
curl -sS -X POST "http://localhost:5004/v1.0/sites/test_site/rerank" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "deviceType": "UNKNOWN",
    "norm": 40,
    "platform": "netcore",
    "products": [
      "2MD01251",
      "2MD01260",
      "2MD01265",
      "5SA01306",
      "ABG02272",
      "ABG02478",
      "ABG02537",
      "ABG02546",
      "ABG02551",
      "ABG02555"
    ],
    "query": "red shirt",
    "userId": "uid-1212"
  }'
```

---

### Rerank — netcore with subgroup boosts

```bash
curl -sS -X POST "http://localhost:5004/v1.0/sites/test_site/rerank" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "deviceType": "UNKNOWN",
    "norm": 40,
    "platform": "netcore",
    "products": [
      "ABG02272",
      "ABG02478",
      "ABG02537",
      "ABG02546",
      "ABG02551",
      "ABG02555"
    ],
    "query": "red shirt",
    "userId": "uid-1212",
    "rankingContext": {
      "boosts": [
        {"uniqueId": "ABG02272", "score": 71.45},
        {"uniqueId": "ABG02478", "score": 70.38},
        {"uniqueId": "ABG02537", "score": 68.88},
        {"uniqueId": "ABG02546", "score": 43.98},
        {"uniqueId": "ABG02551", "score": 40.91},
        {"uniqueId": "ABG02555", "score": 40.90}
      ]
    }
  }'
```

---

### Rerank — with pins and slots (double-encoded JSON strings)

```bash
curl -sS -X POST "http://localhost:5004/v1.0/sites/test_site/rerank" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "deviceType": "UNKNOWN",
    "platform": "unbxd",
    "products": [
      "2MD01251",
      "2MD01260",
      "2MD01265",
      "5SA01306",
      "ABG02272",
      "ABG02478",
      "ABG02537",
      "ABG02546",
      "ABG02551",
      "ABG02555"
    ],
    "query": "red shirt",
    "userId": "uid-1212",
    "rankingContext": {
      "pins": "{\"pins\":[{\"uniqueId\":\"2MD01251\",\"position\":1},{\"uniqueId\":\"9474875\",\"position\":10},{\"uniqueId\":\"9605572\",\"position\":17}]}",
      "slots": "{\"slots\":[{\"filter\":null,\"parsedFilter\":\"\",\"start\":51,\"end\":54},{\"filter\":null,\"parsedFilter\":\"\",\"start\":3,\"end\":5}]}"
    }
  }'
```

---

### Rerank — debug mode

```bash
curl -sS -X POST "http://localhost:5004/v1.0/sites/test_site/rerank" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 5,
    "platform": "unbxd",
    "products": ["2MD01251", "2MD01260", "2MD01265"],
    "query": "red shirt",
    "userId": "uid-1212",
    "debug": true
  }'
```

---

### Rerank — netcoreId (Netcore user anchor)

```bash
curl -sS -X POST "http://localhost:5004/v1.0/sites/test_site/rerank" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "platform": "netcore",
    "products": ["ABG02272", "ABG02478"],
    "query": "red shirt",
    "netcoreId": "nc-user-998877"
  }'
```

---

### Recommend v1 — unbxd deeprec

```bash
curl -sS -G "http://localhost:5004/v1.0/sites/test_site/recommend" \
  --data-urlencode "query=red shirt" \
  --data-urlencode "norm=40" \
  --data-urlencode "count=10" \
  --data-urlencode "platform=unbxd" \
  --data-urlencode "userId=uid-1212"
```

---

### Recommend v1 — netcore VAV algo

```bash
curl -sS -G "http://localhost:5004/v1.0/sites/test_site/recommend" \
  --data-urlencode "userId=1" \
  --data-urlencode "platform=netcore" \
  --data-urlencode "algo=vav" \
  --data-urlencode "count=8"
```

---

### Recommend v1 — netcore BAB algo

```bash
curl -sS -G "http://localhost:5004/v1.0/sites/test_site/recommend" \
  --data-urlencode "userId=1" \
  --data-urlencode "platform=netcore" \
  --data-urlencode "algo=bab"
```

---

### Recommend v1 — semantic search (no userId required)

```bash
curl -sS -G "http://localhost:5004/v1.0/sites/test_site/recommend" \
  --data-urlencode "query=0 3 month boi" \
  --data-urlencode "norm=40" \
  --data-urlencode "count=50" \
  --data-urlencode "platform=semantic_search"
```

---

### Recommend v2 — netcore VAV with filters

```bash
curl -sS -X POST "http://localhost:5004/v2.0/sites/test_site/recommend" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "pids": ["214-SF-111"],
    "deviceType": "UNKNOWN",
    "norm": 40,
    "platform": "netcore",
    "algo": "vav",
    "query": "red shirt",
    "userId": "uid-1212",
    "filters": {
      "componentFilter1": {
        "component_filters": [
          {
            "fieldName": "category",
            "values": ["shirts"],
            "condition": 1,
            "type": 1
          }
        ]
      }
    }
  }'
```

---

### Recommend v2 — top-sellers algo

```bash
curl -sS -X POST "http://localhost:5004/v2.0/sites/test_site/recommend" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "platform": "netcore",
    "algo": "top-sellers",
    "userId": "uid-1212",
    "filters": {}
  }'
```

---

### Recommend v2 — category-top-sellers (categoryPaths required)

```bash
curl -sS -X POST "http://localhost:5004/v2.0/sites/test_site/recommend" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "platform": "netcore",
    "algo": "category-top-sellers",
    "categoryPaths": ["cat1", "cat1^_^cat2"],
    "userId": "uid-1212",
    "filters": {}
  }'
```

---

### Recommend v2 — more-like-these with boosted filters

```bash
curl -sS -X POST "http://localhost:5004/v2.0/sites/test_site/recommend" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "pids": ["214-SF-111"],
    "norm": 40,
    "platform": "netcore",
    "algo": "more-like-these",
    "userId": "uid-1212",
    "filters": {
      "componentFilter1": {
        "component_filters": [
          {"fieldName": "category", "values": ["shirts"], "condition": 1, "type": 1}
        ]
      }
    },
    "boostedFilters": {
      "boostedFilters_1": {
        "component_filters": [
          {"fieldName": "category", "values": ["shirts"], "condition": 1, "type": 1}
        ]
      }
    }
  }'
```

---

### Recommend v2 — semantic search (full)

> **Prod routing note:** To hit **goreranker** through Envoy, the site must be in `goRoutes`/`goRoutesSemantic`, or add `?platform=semantic_search` to the URL (Envoy reads query params, not the JSON body). Local `:5004` is always Go.

```bash
curl -sS -X POST "http://localhost:5004/v2.0/sites/test_site/recommend" \
  -H "Content-Type: application/json" \
  -H "accept: application/json" \
  -H "unbxd-request-id: dd4c8c6b-2bd7-4ea1-a1b1-2ca1797ed909" \
  -H "x-request-id: 18bbd962-402c-4b81-a9c8-83863f234293" \
  -d '{
    "count": 100,
    "pids": ["214-SF-111"],
    "deviceType": "UNKNOWN",
    "norm": 10,
    "platform": "semantic_search",
    "query": "basic apple phone",
    "userId": "uid-1701960336154-15890",
    "filters": {
      "componentFilter1": {
        "component_filters": [
          {"fieldName": "category", "values": ["shirts"], "condition": 1, "type": 1}
        ]
      }
    }
  }'
```

---

### Recommend v2 — image search (Jarvis / Vision)

```bash
curl -sS -X POST "http://localhost:5004/v2.0/sites/test_site/recommend" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://cdn.shopify.com/s/files/1/0536/3594/0515/products/77b9a100-f020-dd25-b08e-648df234d3dd_green-stretch-satin-01_800x.jpg?v=1646375282",
    "platform": "image_search",
    "norm": 40,
    "source": "URL"
  }'
```

---

### Events — Aerospike (default)

```bash
curl -sS -G "http://localhost:5004/v1.0/sites/test_site/events" \
  --data-urlencode "userId=uid-1212"
```

---

### Events — specific event types

```bash
curl -sS -G "http://localhost:5004/v1.0/sites/test_site/events" \
  --data-urlencode "userId=uid-1212" \
  --data-urlencode "eventType=ORDER" \
  --data-urlencode "count=20"
```

---

### Events — Olympus realtime (Redis-backed)

```bash
curl -sS -G "http://localhost:5004/v1.0/sites/test_site/events" \
  --data-urlencode "userId=123" \
  --data-urlencode "platform=olympus"
```

---

### Events — multiple event types (comma-separated)

```bash
curl -sS -G "http://localhost:5004/v1.0/sites/test_site/events" \
  --data-urlencode "userId=uid-1212" \
  --data-urlencode "eventType=CLICK,CART,ORDER" \
  --data-urlencode "count=10"
```

---

### Prod examples (US)

```bash
curl -sS -X POST "http://reranker.prod.use-1d.infra/v1.0/sites/YOUR_SITEKEY/rerank" \
  -H "Content-Type: application/json" \
  -d '{"userId":"uid-1212","platform":"unbxd","query":"shoes","products":["pid1","pid2"],"count":10}'
```

```bash
curl -sS -G "http://reranker.prod.use-1d.infra/v1.0/sites/YOUR_SITEKEY/recommend" \
  --data-urlencode "userId=uid-1212" \
  --data-urlencode "platform=unbxd" \
  --data-urlencode "count=25"
```

---

## Core Business Logic

### Rerank flow (`internal/rerank/service.go`)

```
1. Decode POST body → validate userId (except semantic_search)
2. Split products into reserved (pins/slots) vs unreserved
3. If ≤1 unreserved product → skip model, pass through
4. Fetch Albus deployment: handler = {platform}#rerank
5. Fetch sibling_sites
6. Strategy selection:
     platform=netcore  → netcore reranker (AI Pipeline netcore model)
     platform=unbxd/default → deeprec reranker
7. Deeprec internals:
     a. Fetch CLICK/CART/ORDER events (Aerospike or Olympus per deployment.events_platform)
     b. If event count < min_hist_count → return relevance-score fallback
     c. If boost_score=1.0 with events → skip model, return event-boosted order
     d. Call AI Pipeline inference with user history + candidate products
     e. Apply event-boost reranking within similar relevance bands
8. Interpolate original_score → score on [0.0001, norm-0.1]
9. Re-insert pinned/slotted products at reserved positions
10. Emit Datadog counter reranker_rerank_num_requests
```

### Recommend flow (`internal/recommend/service.go`)

```
1. Decode v1 query params or v2 JSON body
2. Fetch deployment:
     semantic_search → handler aragorn#recommend (for vector config)
     else → {platform}#recommend
3. Strategy by platform:
     netcore        → VAV/BAB/RFU/TS/CTS/MLT via AI Pipeline + Aerospike
     semantic_search→ embeddings or Aragorn vector search + Docstore filter
     image_search   → Jarvis tags + Vision detection + encoder inference
     ctl            → Mimir category + S3 mapping widgets
     unbxd/default  → deeprec recommend
4. Normalize scores to norm (except semantic_search path)
5. Return products[] or ctlWidgets[] or image boxes/tags
```

### Events flow

```
platform=aerospike (default):
  Key: {EVENTS_NAMESPACE}/{EVENTS_SET or nc_user_data}/{sitekey}.{userId}
  Bins: clicked_pids, carted_pids, ordered_pids (map pid → timestamp ms)

platform=olympus:
  POST Olympus /api/sites/{site}/feature_group/user/features
  features=click_pids,cart_pids,order_pids
```

---

## Config System (Albus / Configstore)

Configuration is loaded at request time from **Albus** via `github.com/unbxd/albus-client-go`.

| Setting | Env var | Default (local) |
|---|---|---|
| Configstore URL | `CONFIGSTORE_HOST` | `http://faker_albus:7688` |
| Service name | `CONFIGSTORE_SERVICE_NAME` | `reranker` |
| Deployment property | `CONFIG_PROPERTY_DEPLOYMENT` | `deployments` |
| Sibling sites property | `CONFIG_PROPERTY_SIBLINGSITES` | `sibling_sites` |
| Timeout | `CONFIGSTORE_TIMEOUT` | `200` ms (prod: `600` ms) |
| NATS | `NATS_HOST` | `nats://nats:4222` |

**Handler key pattern:** `{platform}#{api}`

| API | Example handler |
|---|---|
| Rerank unbxd | `unbxd#rerank` |
| Rerank netcore | `netcore#rerank` |
| Recommend netcore | `netcore#recommend` |
| Semantic search (deployment) | `aragorn#recommend` |
| Image encoder vertical | `image_encoder#default` |

**Campaign selection:** If `campaignId` is empty, the first deployment in the array is used. If set, must match `deployment_id`.

**Missing config behavior:**
- Albus fetch failure → 500 `failed to get config from albus`
- Handler/property not found → returns `nil` deployment (may cause nil-pointer downstream)
- Invalid `campaignId` → 500 `campaignId {id} not found in the configstore`

**Example deployment schema** (`scripts/faker/albus/responses/deployments/unbxd#rerank.json`):

```json
{
  "deployment_id": "d1",
  "deployment_token": "68d7c49b06c0e6bcccd0d43b68a244e44313828a1",
  "filter_id": "fi1",
  "model_id": "model1",
  "model_version": "v1",
  "model_metadata": {
    "model": "youtube_dnn",
    "events_platform": "olympus",
    "algo_configs": { "search": { "min_hist_count": 0, "boost_score": 0.8 } }
  }
}
```

**Default algo configs** (when not in Albus): `internal/downstreams/configstore/consts.go` — e.g. search `min_hist_count=0`, `boost_score=0.8`, `event_boost={CLICK:0.1, CART:0.5, ORDER:-1}`.

---

## Data Storage

No Postgres. Primary stores: **Aerospike** (events/recipes) + **Redis** (cache/auxiliary).

### Aerospike

| Use | Namespace (prod) | Set | Key format | Bins |
|---|---|---|---|---|
| User events | `user_data` | `user_data` | `{sitekey}.{userId}` | `clicked_pids`, `carted_pids`, `ordered_pids` |
| Netcore events | `user_data` | `nc_user_data` | `{sitekey}.{userId}` | same |
| Top sellers | `recommendations` | `recipe_topseller` | `{sitekey}.{recipeId}` | `TS` (list of PIDs) |
| Category top sellers | `recommendations` | `recipe_topseller` | `{sitekey}.{recipeId}.{categoryPath}` | `CTS` (list) |

Default recipe IDs: topsellers `2`, category topsellers `9`. Category path separator: `^_^` (e.g. `cat1^_^cat2`).

**Bin format:** Maps of `productId → timestamp_ms`. Order events may store comma-separated PIDs in the key.

**Inspect locally:**

```bash
aql -c "SELECT * FROM test.test WHERE PK='test_site.uid-1212'" -h as1 --no-config-file
aql -c "SELECT * FROM test.test WHERE PK='test_site.2'" -h as1 --no-config-file
aql -c "SELECT * FROM test.test WHERE PK='test_site.9.cat1^_^cat2'" -h as1 --no-config-file
```

**Prod hosts (examples):**

| Region | `DATASTORE_HOSTS` |
|---|---|
| US | `as1.nodes.prod.use-1d.infra,as2.nodes.prod.use-1d.infra,as3.nodes.prod.use-1d.infra` |
| EU | `as-xdr01.nodes.prod.eu-west-2.infra,...` |
| APAC | `as-xdr01.nodes.prod.ap-southeast-1.infra,...` |

Port: `3000`. Transaction timeout: `500` ms.

---

### Redis (bundled chart + cache)

Connection: `RERANKER_REDIS_URL` → Helm injects `{release}-redis-master:6379`.

| Key pattern | Type | TTL | Purpose |
|---|---|---|---|
| `reranker.jarvis.{image_url}` | STRING (zlib compressed) | `RERANKER_REDIS_TTL` hours (default 24h) | Jarvis attribution cache |
| `reranker.vision.{image_url}.{maxResults}.{bboxId}.{productSet}` | STRING (compressed) | 24h | Vision detection cache |
| `category_mapping:{s3path}` | STRING | `RERANKER_REDIS_CTL_TTL` hours (24h) | CTL category → supporting category map |
| `ctl_filter_mapping:{field}:{s3path}` | STRING | 24h | CTL filter field mappings |
| `{sitekey}.query_blacklist` | STRING (JSON array) | no explicit TTL in code | Blocked queries |
| `{sitekey}.query_suggestions` | HASH (field=cleaned query) | — | Query synonym/suggestion overrides |
| SHA256(`query+model+campaignId+version`) | STRING (JSON float[]) | `EMBEDDINGS_CACHE_TTL` sec (3600) | Query embedding vectors |
| SHA256 (same fields) | STRING (JSON string[]) | `SEMANTIC_SEARCH_SIMILAR_QUERY_CACHE_TTL` sec (600) | Similar query suggestions |

**Inspect locally:**

```bash
redis-cli -h redis GET "test_site.query_blacklist"
redis-cli -h redis HGET "test_site.query_suggestions" "lucky power"
redis-cli -h redis KEYS "reranker.*"
```

Python sidecar additionally uses external Redis instances for LTR/facet affinity (`LTR_REDIS`, `FACET_AFFINITY_REDIS_HOST` in Helm py env) — separate from the bundled cache Redis.

---

## Background Jobs / Workers

**No Kafka consumers or cron jobs** exist in the Go codebase.

The only background goroutine is the **AI Pipeline cookie refresher** (`internal/downstreams/aipipeline/inference_svc.go`):

- Runs when `AIPIPELINE_CLUSTER_LOCAL=false` (local/dev)
- Interval: `AIPIPELINE_COOKIE_REFRESH_INTERVAL` seconds (default `720`, prod `7200`)
- Refreshes Kubeflow `authservice_session` cookie for inference calls

Olympus consumer (separate service) writes realtime events to Redis DB 3 that reranker reads when `events_platform=olympus`.

---

## APAC vs US vs EU

Regional behavior is driven by **Helm values files** — no `if APAC` branches in Go code.

| Setting | US (`us-east-1`) | EU (`eu-west-2`) | APAC (`ap-southeast-1`) |
|---|---|---|---|
| **Ingress host** | `reranker.prod.use-1d.infra` | `reranker.prod.eu-west-2.infra` | `reranker.prod.ap-southeast-1.infra` |
| **Replicas** | 4 | 2 | (see values file) |
| **Configstore** | `configstore.prod.use-1d.infra` | `configstore.prod.eu-west-2.infra` | `configstore.prod.ap-southeast-1.infra` |
| **Olympus** | `olympus.prod.use-1d.infra` | not in base EU go env | `olympus.prod.ap-southeast-1.infra` |
| **AI Pipeline** | `workflow-kubeflow.prod.use-1d.infra` | `workflow-kubeflow.prod.eu-west-2.infra` | `workflow-kubeflow.prod.ap-southeast-1.infra` |
| **Docstore** | `hodor.prod.use-1d.infra` | `hodor.prod.eu-west-2.infra` | `hodor.prod.ap-southeast-1.infra` |
| **Mimir** | `mimir.prod.use-1d.infra` | — | `mimir.prod.ap-southeast-1.infra` |
| **Vision location** | `us-east1` | `europe-west1` (implicit) | `asia-east1` |
| **Semantic char threshold** | 4 (py) / 2 (go) | 2 | 2 |
| **Semantic score threshold** | 0.2 (py) / 0.3 (go) | 0.3 | 0.3 |
| **Pipeline IFSVC suffix** | `predictor-default` | `predictor-default` | `predictor` |
| **Embeddings secret** | disabled | enabled | enabled |
| **Envoy goRoutes** | site-specific regex list | EU customer sites | APAC dev sites + redirects |

**GCP pilot (`gcp-pilotrc01-use4-values.yaml`):** Envoy can **rewrite host** to `reranker.prod.ap-southeast-1.infra` for selected image-search / APAC customer routes (`imgApacAzaRedirect` in configmap).

**Practical implications:**

1. **Envoy routing differs per region** — a site migrated to Go in US may still hit Python in EU unless its regex is added to `sidecar.goRoutes` in that region's values file.
2. **Olympus events** require `OLYMPUS_HOST` configured and `platform=olympus` on `/events`, or `events_platform: olympus` in deployment metadata for rerank/recs.
3. **Semantic search** uses `aragorn#recommend` handler for deployment config; Aragorn host varies (`http://aragorn:80` in-cluster).

---

## External Dependencies

| Service | Env var | Purpose |
|---|---|---|
| **Albus / Configstore** | `CONFIGSTORE_HOST`, `NATS_HOST` | Per-site deployment config, sibling sites, vertical configs |
| **Aerospike** | `DATASTORE_HOSTS`, `EVENTS_*`, `TOPSELLERS_*` | User events, topseller recipes |
| **Redis** | `RERANKER_REDIS_URL` | Response cache (Jarvis, Vision, embeddings, CTL, QS) |
| **AI Pipeline / Kubeflow** | `AIPIPELINE_HOST`, `AIPIPELINE_*` | Model inference (rerank, recs, text encoders) |
| **Hodor Docstore** | `DOCSTORE_HOST` | `POST /sites/{sitekey}/products/_filter?query_tag=reranker` |
| **Olympus** | `OLYMPUS_HOST` | Feature store / realtime events |
| **Mimir** | `MIMIR_HOST` | Search/category APIs for CTL |
| **Aragorn** | `ARAGORN_HOST` | Vector search for semantic recs |
| **Jarvis** | `JARVIS_HOST` | Image attribution tags |
| **Google Vision** | `VISION_SERVICE_ACCOUNT`, `VISION_IMAGE_*` | Object detection for image search |
| **OpenAI** | `OPENAI_URL`, `EMBEDDINGS_API_KEY` | Query suggestions (external QS model) |
| **Asterix** | `ASTERIX_ENDPOINT` | Alternative QS / suggestion model |
| **AWS S3** | credentials via `ds-reranker` secret | CTL category mapping files |
| **Datadog / StatsD** | `METRICS_URL` | Request counters, latency histograms |

---

## Environment Variables

### Application / HTTP

| Variable | Default | Description |
|---|---|---|
| `ENV` | `dev` | Environment tag |
| `REGION` | `us-east-1` | Region tag |
| `HTTP_HOST` | `0.0.0.0` | Bind address |
| `HTTP_PORT` | `5004` | HTTP port (Go local); prod K8s Go uses `5005` |
| `HTTP_MONITOR` | `/pong`, `/monitor` | Health paths |
| `LOG_LEVEL` | `debug` | `info`, `error`, `warn`, `debug` |
| `LOG_ENCODING` | `console` | `console` or `json` |
| `LOG_OUTPUT` | `stdout` | Log sink |

### Metrics

| Variable | Default | Description |
|---|---|---|
| `METRICS_ENABLED` | `false` | Enable StatsD metrics |
| `METRICS_URL` | `datadog:8125` | StatsD endpoint |
| `METRICS_NAMESPACE` | `reranker` | Metric namespace |
| `METRICS_TAGS` | `app:reranker,name:reranker` | Datadog tags |

### Redis

| Variable | Default | Description |
|---|---|---|
| `RERANKER_REDIS_URL` | `host.docker.internal:6379` | Redis address |
| `RERANKER_REDIS_READ_TIMEOUT` | `100` | Read timeout (ms) |
| `RERANKER_REDIS_WRITE_TIMEOUT` | `100` | Write timeout (ms) |
| `RERANKER_REDIS_CONN_TIMEOUT` | `50` | Dial timeout (ms) |
| `RERANKER_REDIS_TTL` | `24` | Jarvis/Vision cache TTL (hours) |
| `RERANKER_REDIS_CTL_TTL` | `24` | CTL mapping cache TTL (hours) |

### Aerospike / Events

| Variable | Default (code) | Prod example |
|---|---|---|
| `DATASTORE_HOSTS` | `as1` | `as1.nodes.prod.use-1d.infra,...` |
| `DATASTORE_PORT` | `3000` | `3000` |
| `DATASTORE_CONN_TIMEOUT` | `1000` | `1000` |
| `DATASTORE_TRANSACTION_TIMEOUT` | `500` | `500` |
| `EVENTS_NAMESPACE` | `test` | `user_data` |
| `EVENTS_SET` | `test` | `user_data` |
| `NETCORE_EVENTS_SET` | `nc_user_data` | `nc_user_data` |
| `TOPSELLERS_NAMESPACE` | `test` | `recommendations` |
| `TOPSELLERS_SET` | `test` | `recipe_topseller` |
| `TOPSELLERS_RECIPEID` | `2` | `2` |
| `TOPSELLERS_COLUMN` | `TS` | `TS` |
| `CATEGORY_TOPSELLERS_NAMESPACE` | `test` | `recommendations` |
| `CATEGORY_TOPSELLERS_SET` | `test` | `recipe_topseller` |
| `CATEGORY_TOPSELLERS_RECIPEID` | `9` | `9` |
| `CATEGORY_TOPSELLERS_COLUMNS` | `CTS` | `CTS` |

### Configstore / NATS

| Variable | Default | Description |
|---|---|---|
| `CONFIGSTORE_HOST` | `http://faker_albus:7688` | Albus URL |
| `CONFIGSTORE_SERVICE_NAME` | `reranker` | Service name in Albus |
| `CONFIG_PROPERTY_DEPLOYMENT` | `deployments` | Deployment property |
| `CONFIG_PROPERTY_SIBLINGSITES` | `sibling_sites` | Sibling sites property |
| `CONFIGSTORE_TIMEOUT` | `200` | Fetch timeout (ms) |
| `NATS_HOST` | `nats://nats:4222` | NATS for config push |

### AI Pipeline

| Variable | Default | Description |
|---|---|---|
| `AIPIPELINE_HOST` | `http://faker_aipipeline:12000` | Kubeflow / pipeline host |
| `AIPIPELINE_USERNAME` | `""` | From `kubeflow-user-secret` in prod |
| `AIPIPELINE_PASSWORD` | `""` | From secret |
| `AIPIPELINE_CONN_TIMEOUT` | `50` | Connection timeout (ms) |
| `AIPIPELINE_TIMEOUT` | `200` | Request timeout (ms); prod `600` |
| `AIPIPELINE_COOKIE_REFRESH_INTERVAL` | `720` | Cookie refresh (sec); prod `7200` |
| `AIPIPELINE_INFSVC_NAMESPACE` | `search` | Inference service namespace |
| `AIPIPELINE_CLUSTER_LOCAL` | `false` | Skip cookie refresh when `true` |
| `PIPELINE_IFSVC_SUFFIX` | `predictor-default` | ISVC name suffix; APAC: `predictor` |

### Docstore

| Variable | Default | Description |
|---|---|---|
| `DOCSTORE_HOST` | `http://faker_docstore:12001` | Hodor base URL |
| `DOCSTORE_CONN_TIMEOUT` | `50` | Connection timeout (ms) |
| `DOCSTORE_FILTER_TIMEOUT` | `500` | Filter API timeout (ms) |
| `DOCSTORE_FILTER_MAX_PRODUCTS` | `10000` | Max products per filter call |

### Semantic search

| Variable | Default | Description |
|---|---|---|
| `SEMANTIC_SEARCH_CHAR_THRESHOLD` | `5` | Min query length (chars) |
| `SEMANTIC_SEARCH_TOKEN_THRESHOLD` | `2` | Min token count |
| `SEMANTIC_SEARCH_SCORE_THRESHOLD` | `0.6` | Min similarity score |
| `EMBEDDINGS_CACHE_TTL` | `3600` | Query vector cache (sec) |
| `SEMANTIC_SEARCH_SIMILAR_QUERY_CACHE_TTL` | `600` | Similar-query cache (sec) |
| `TORCH_TEXT_ENCODERS` | `e5` | Comma-separated encoder models |
| `ENABLE_SC_EXTERNAL_QS` | `false` | External query suggestions |
| `QUERY_SUGGESTION_COUNT` | `5` | Max QS results |

### Vision / Jarvis / Olympus / Mimir / Aragorn / OpenAI

| Variable | Default | Description |
|---|---|---|
| `JARVIS_HOST` | `http://jarvis:80` | Jarvis service |
| `JARVIS_CONN_TIMEOUT` | `300` | ms |
| `JARVIS_TIMEOUT` | `10000` | ms |
| `VISION_SERVICE_ACCOUNT` | `""` | GCP SA JSON (secret) |
| `VISION_IMAGE_PROJECT` | `unbxdgcp` | GCP project |
| `VISION_IMAGE_LOCATION` | `us-east1` | GCP region |
| `VISION_TIMEOUT` | `4000` | ms |
| `VISION_RETRY_COUNT` | `2` | Retries |
| `VISION_BACKOFF_TIME` | `50` | ms |
| `OLYMPUS_HOST` | `http://host.docker.internal:12006` | Olympus feature store |
| `OLYMPUS_CONN_TIMEOUT` | `300` | ms |
| `OLYMPUS_TIMEOUT` | `10000` | ms |
| `MIMIR_HOST` | `http://mimir.pilot-rc-unbxd.infra` | Mimir search |
| `MIMIR_CONN_TIMEOUT` | `300` | ms |
| `MIMIR_TIMEOUT` | `1000` | ms |
| `ARAGORN_HOST` | `http://faker_aragorn:7271` | Vector search |
| `ARAGORN_CONN_TIMEOUT` | `50` | ms |
| `ARAGORN_TIMEOUT` | `200` | ms |
| `OPENAI_URL` | `https://api.openai.com/v1` | OpenAI base |
| `EMBEDDINGS_API_KEY` | — | OpenAI key (secret in prod) |
| `ASTERIX_ENDPOINT` | `http://faker_aipipeline:12000` | Asterix QS |
| `ASTERIX_CONN_TIMEOUT` | `50` | ms |
| `ASTERIX_TIMEOUT` | `200` | ms |

### Cache (legacy flag group)

| Variable | Default | Description |
|---|---|---|
| `CACHE_HOST` | `redis:6379` | Alternate cache host flag |
| `CACHE_CONN_TIMEOUT` | `5000` | ms |
| `CACHE_READ_TIMEOUT` | `50` | ms |
| `CACHE_WRITE_TIMEOUT` | `50` | ms |

---

## Deployment (Helm / Kubernetes)

### Chart structure

```
helm/reranker/
├── Chart.yaml
├── values.yaml                    # EU defaults
├── values/
│   ├── values-us-east-1.yaml
│   ├── values-eu-west-2.yaml
│   ├── values-ap-southeast-1.yaml
│   ├── values-ap-southeast-2.yaml
│   ├── values-australia-southeast1.yaml
│   ├── values-use-demo.yaml
│   ├── gcp-ssdev-values.yaml
│   └── gcp-pilotrc01-use4-values.yaml
└── templates/
    ├── deployment.yaml            # Go + Python + Envoy
    ├── service.yaml               # LB port 80 → Envoy
    ├── ingress.yaml
    ├── configmap.yaml             # Envoy routing rules
    ├── rediscm.yaml               # Redis health scripts
    ├── service-monitor.yaml
    ├── serviceaccount.yaml
    └── _helpers.tpl
```

Redis sub-chart: `redis/redis-stack-server:7.2.0-v6`, master `2 CPU / 2Gi`, persistence `10Gi`.

### Container layout

| Container | K8s name | Envoy cluster | Image tag (AWS example) | Port | Probe |
|---|---|---|---|---|---|
| Go | `goreranker` | `goreranker` | `v1.3.4` (pilot: `v1.3.8`) | `5005` | `GET /monitor` |
| Python | `pyreranker` | `pyreranker` | `v0.19.1` (legacy) | `5004` | TCP |
| Envoy | `envoy` | — | `envoyproxy/envoy:v1.17.0` | `5010` (`sidecar.port`) | — |

Service `targetPort` = `sidecar.port` (5010), not Go/Python directly.

### Dockerfile (Go binary, v2)

```dockerfile
FROM ubuntu:jammy
RUN apt-get install -y ca-certificates curl libheif-examples  # HEIC/HEIF transcoding
ADD bin/reranker.bin /reranker/op.bin
CMD ["/reranker/op.bin", "start"]
```

### Resource sizing (production defaults)

| Component | CPU request | CPU limit | Memory |
|---|---|---|---|
| Go | 250–300m | 500–600m | 256–512Mi |
| Python | 500m | 1 | 512–768Mi |
| Envoy sidecar | 100–200m | 200m | 128–256Mi |
| Redis master | 2 | 2 | 2Gi |

### Secrets mounted

| Secret name | Keys | Used for |
|---|---|---|
| `ds-reranker` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_PERSONALIZE_ARN` | S3 CTL maps, Personalize |
| `kubeflow-user-secret` | `user`, `password` | AI Pipeline auth |
| `vision-secret` | `VISION_SERVICE_ACCOUNT` | Google Vision |
| `embedding-secret` | `EMBEDDINGS_API_KEY`, `AZURE_OPENAI_API_KEY` | OpenAI QS (when enabled) |

---

## Common Failure Modes & Debugging

### `{"error":"userId not present: bad request"}`

**HTTP:** 400  
**Cause:** Missing `userId` on rerank/recommend when `platform` is not `semantic_search`.  
**Fix:** Pass `userId` or `netcoreId`.

---

### `{"error":"category name mandatory for category-top-sellers: bad request"}`

**HTTP:** 400  
**Cause:** `algo=category-top-sellers` without `categoryPaths`.  
**Fix:** Add `categoryPaths` JSON array (v2) or query param (v1).

---

### `failed to get config from albus`

**HTTP:** 500  
**Cause:** Configstore unreachable, timeout (`CONFIGSTORE_TIMEOUT`), or site not configured.  
**Check:**

```bash
curl -sS "http://configstore.prod.use-1d.infra/sites/test_site/config?service=reranker"
```

Verify handler `unbxd#rerank` or `netcore#recommend` exists with `deployments` property.

---

### `campaignId dX not found in the configstore`

**HTTP:** 500  
**Cause:** `campaignId` query/body does not match any `deployment_id` in Albus.  
**Fix:** Use correct campaign id or omit to use first deployment.

---

### Rerank returns original order / no personalization

1. Check event history — deeprec skips model if `event_count < min_hist_count`:
   ```bash
   curl -sS -G "http://localhost:5004/v1.0/sites/test_site/events" \
     --data-urlencode "userId=uid-1212"
   ```
2. Verify Aerospike key exists:
   ```bash
   aql -c "SELECT clicked_pids FROM user_data.user_data WHERE PK='test_site.uid-1212'" \
     -h as-xdr01.nodes.prod.ap-southeast-1.infra --no-config-file
   ```
3. Check `events_platform` in deployment — if `olympus`, events come from Redis via Olympus:
   ```bash
   curl -sS -G "http://localhost:5004/v1.0/sites/test_site/events" \
     --data-urlencode "userId=uid-1212" \
     --data-urlencode "platform=olympus"
   ```

---

### AI Pipeline / inference timeouts

**Symptoms:** 500 with inference error in logs.  
**Check:** `AIPIPELINE_TIMEOUT` (prod `600` ms), Kubeflow cookie refresh, `AIPIPELINE_CLUSTER_LOCAL=true` in prod.  
**Test pipeline health:**

```bash
curl -sS "http://workflow-kubeflow.prod.use-1d.infra/monitor"
```

---

### Request hits Python (`pyreranker`) instead of Go (`goreranker`)

**Cause:** Envoy default route is `^.*` → `pyreranker`. Your site is not in `sidecar.goRoutes` for that region.  
**Also check:** For `POST /v2.0/.../recommend`, `platform` in the JSON body does **not** steer Envoy — only URL query params or `goRoutes` regex match.  
**Verify which backend served the request:** Compare response shape or port-forward Go directly:

```bash
kubectl port-forward deployment/reranker 5005:5005
curl -sS -X POST "http://localhost:5005/v2.0/sites/YOUR_SITE/recommend" -H "Content-Type: application/json" -d '{"platform":"netcore","algo":"vav","userId":"u1","count":5}'
```

**Fix:** Add site regex to `sidecar.goRoutes` (or `goRoutesSemantic` / `goRoutesUnbxd`) in `helm/reranker/values/values-<region>.yaml` and redeploy.

---

### Redis cache / embedding issues

```bash
# List reranker cache keys
redis-cli -h reranker-redis-master.search KEYS "reranker.*"

# Query suggestions
redis-cli -h reranker-redis-master.search HGETALL "test_site.query_suggestions"

# CTL mapping
redis-cli -h reranker-redis-master.search GET "category_mapping:s3://your-bucket/path.csv"
```

---

### Docstore filter failures

**Symptoms:** Empty `products` after semantic search or netcore recs.  
**Test docstore directly:**

```bash
curl -sS -X POST "http://hodor.prod.use-1d.infra/sites/test_site/products/_filter?query_tag=reranker" \
  -H "Content-Type: application/json" \
  -d '{"filters": {}}'
```

---

### Local development stack

```bash
# From reranker repo root
docker-compose up --build

# Seed data runs via setup container (aerospike.sh + redis.sh)
# Reranker listens on localhost:5004
curl -sS "http://localhost:5004/monitor"
```

Postman collections: `scripts/tests/rerank.postman_collection.json`, `recommend.postman_collection.json`, `events.postman_collection.json` with environment `reranker.host=localhost:5004`.
