# Olympus — Feature Store Architecture & Reference

> **One-line summary:** Olympus is the real-time + batch feature serving layer that powers personalized search and recommendations — it combines Kafka-ingested behavioral events (click/cart/order/search) stored in Redis with offline-materialized Feathr features, and exposes them via a unified HTTP API consumed by reranker, recs, and other downstream services.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Components](#components)
3. [API Reference](#api-reference)
4. [Feature Groups](#feature-groups)
5. [Config System (Albus / Configstore)](#config-system-albus--configstore)
6. [Consumer Job — Event Ingestion](#consumer-job--event-ingestion)
7. [Redis Keyspace](#redis-keyspace)
8. [APAC vs Other Regions](#apac-vs-other-regions)
9. [External Dependencies](#external-dependencies)
10. [Environment Variables](#environment-variables)
11. [Deployment (Helm / Kubernetes)](#deployment-helm--kubernetes)
12. [Common Failure Modes & Debugging](#common-failure-modes--debugging)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         EVENT PIPELINE                                   │
│                                                                          │
│  Tracker / Browser  ──▶  Kafka (topic: requests / tracker-events)        │
│                              │                                           │
│                              ▼                                           │
│                   Olympus Consumer Job (Python)                          │
│                    UID filter + event filter                             │
│                              │                                           │
│                ┌─────────────┴──────────────┐                           │
│          click/cart/order                 search queries                 │
│          (Redis LIST, DB 3)         (Redis ZSET+SET, DB 3)              │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                      BATCH FEATURE PIPELINE                              │
│                                                                          │
│  S3 / Data warehouse  ──▶  Spark + Feathr  ──▶  Redis (DB 0 / cluster)  │
│                              user, query, user_product tables            │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                        OLYMPUS API (FastAPI)                             │
│                                                                          │
│  Albus/Configstore  ──▶  load deployment config                         │
│  Feathr / Redis (DB 0)  ──▶  offline user/product/query features        │
│  Redis (DB 3)           ──▶  realtime event lists + query metadata       │
│                                                                          │
│  /api/sites/{sitekey}/feature_group/{fg}/features                        │
│  /api/sites/{sitekey}/features/_batch                                    │
│  /api/sites/{sitekey}/users/{userId}/products                            │
└──────────────────────────────────────────────────────────────────────────┘
              │
              ▼
   Reranker, Recs, Search ranking services
```

---

## Components

| Component | Path | Purpose |
|---|---|---|
| **API service** | `app/` | FastAPI app serving feature group endpoints |
| **Consumer job** | `job/` | Kafka consumer writing realtime events to Redis |
| **Ingestion scripts** | `build/ingestion/` | Batch Feathr/Spark feature materialization into Redis |
| **Helm chart** | `helm/olympus/` | Kubernetes deployment, region-specific values |
| **Faker** | `scripts/faker/` | Local mock of configstore for dev/docker-compose |

---

## API Reference

Base URL (internal): `http://olympus/` (or `http://olympus.prod.<region>.infra/`)  
Port: `5004` (container), `80` (service)

---

### `GET /monitor`

Health check.

**Response:** `"Hello from Feature Store!!!"`

---

### `GET /api/sites/{sitekey}/feature_group/{feature_group}/features`

Fetch features for a given site + feature group. This is the primary read path used by reranker and recs.

**Path params:**
- `sitekey` — site identifier (e.g. `ss-unbxd-prod-eng-thailand13731636957760`)
- `feature_group` — one of `user`, `user_product`, `query`

**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `uid` | string | `""` | User ID |
| `products` | CSV string | `""` | Product IDs to fetch features for |
| `queries` | CSV string | `""` | Query strings (for `query` feature group) |
| `features` | CSV string | `"*"` | Feature names to return; `*` means all |
| `filter` | repeated string | `[]` | Filter expressions e.g. `score>0.5` |
| `filter_op` | string | `"AND"` | `AND` or `OR` for multiple filters |
| `debug` | bool-like | `false` | Return debug trace in response |
| `ruleset` | string | `"search"` | Influences query suffix rewriting (`search` vs `recs`) |

**Response shape:**
```json
{
  "data": {
    "features": [ { "uniqueId": "...", "feature1": 0.9, ... } ],
    "metadata": { "feature1": { "min": 0.0, "max": 1.0 } }
  },
  "error": null,
  "debug": null,
  "msTaken": 12
}
```

On failure:
```json
{
  "data": null,
  "error": { "msg": "Feature Retrieval has failed due to 500: Config Retrieval has failed due to No configuration has been set for this site", "code": 500 }
}
```

---

### `POST /api/sites/{sitekey}/feature_group/{feature_group}/features`

Same as GET but params come from JSON body.

**Body:**
```json
{
  "uid": "uid-123",
  "products": ["pid1", "pid2"],
  "queries": ["blue shoes"],
  "features": "click_pids,cart_pids,recent_queries",
  "filter": [],
  "filter_op": "AND",
  "debug": false,
  "ruleset": "search"
}
```

---

### `POST /api/sites/{sitekey}/features/_batch`

Run multiple feature group requests in a single call (runs concurrently via `asyncio.gather`). Useful when a caller needs both `user` and `query` features in one round-trip.

**Body:** object keyed by arbitrary request name:
```json
{
  "user_req": {
    "feature_group": "user",
    "uid": "uid-123",
    "features": "click_pids,cart_pids"
  },
  "query_req": {
    "feature_group": "query",
    "queries": ["blue shoes"],
    "ruleset": "search"
  }
}
```

**Response:**
```json
{
  "data": {
    "features": {
      "user_req": [ { "uniqueId": "uid-123", "click_pids": [...] } ],
      "query_req": [ { "uniqueId": "blue shoes", "query_score": 0.8 } ]
    }
  }
}
```

Max output per request capped at `MAX_BATCH_OUTPUT` (default `1000`).

---

### `GET /api/sites/{sitekey}/users/{userId}/products`

Returns product IDs associated with a user from Redis.

> **Note:** This endpoint has a known key-format mismatch — it parses keys with `#` as separator while the consumer writes dot-separated keys. Typically returns empty unless the ingestion format matches.

---

## Feature Groups

Feature group dispatch is in `app/features.py`:

```
CLASS_MAP = {
  "user_product": UserProductCondensedFeatures,
  "user":         UserFeatures,
  "query":        QueryFeatures,
}
```

### `user` — Realtime + Offline User Features

**File:** `app/feature_groups/user/user_feature.py`

Combines:
1. **Offline** user features from Feathr table `<site>_user_<suffix>` (Redis DB 0)
2. **Realtime** behavioral events from Redis DB 3:
   - `click_pids` → `LRANGE <site>.<uid>.click 0 -1`
   - `cart_pids` → `LRANGE <site>.<uid>.cart 0 -1`
   - `order_pids` → `LRANGE <site>.<uid>.order 0 -1`
   - `recent_queries` → `ZREVRANGE <site>.<uid>.query_metadata 0 -1 WITHSCORES`

Events are deduped in 5-second buckets to collapse rapid duplicate interactions. Realtime events are merged on top of offline features, with offline features acting as the fallback.

**Typical usage:**
```
GET /api/sites/{sitekey}/feature_group/user/features?uid=uid-123&features=click_pids,cart_pids,order_pids,recent_queries
```

---

### `user_product` — Per-User Product Affinity Features

**File:** `app/feature_groups/user_product/user_product_condensed_feature.py`

Reads per-user product-level features from Redis (DB 0 / cluster). Supports two storage modes:

| Mode | Env var | Redis key | Redis type |
|---|---|---|---|
| Compressed JSON | `ENCODED_KEY_SUFFIX=enc` | `<site>_user_product_<suffix>_enc:<uid>` | STRING (zlib compressed) |
| Plain hash | `ENCODED_KEY_SUFFIX=""` | `<site>_user_product_<suffix>:<uid>` | HASH fields: `<pid>.<feature>` |

When `json_encoded=true` is in deployment metadata and the compressed key exists, it decompresses and parses the blob. Otherwise falls back to `HGETALL`.

**Typical usage:**
```
GET /api/sites/{sitekey}/feature_group/user_product/features?uid=uid-123&products=pid1,pid2
```

---

### `query` — Query-Level Features

**File:** `app/feature_groups/query/query_feature.py`

Reads query features from Feathr table `<site>_query_<suffix>` (Redis DB 0).

Applies ruleset mapping:
- `ruleset=search` → may substitute `category` suffix with `search`
- `ruleset=recs` → opposite substitution

Returns one row per query string, keyed by `uniqueId`.

**Typical usage:**
```
GET /api/sites/{sitekey}/feature_group/query/features?queries=blue+shoes,red+boots&ruleset=search
```

---

## Config System (Albus / Configstore)

All three feature groups require site config to be present in the config store before they can serve requests.

### How config is loaded

On request, `app/utils/utils.py` calls:

```python
deployments = config_manager.get_config(site_key, "olympus", feature_group, "deployments")
if not deployments:
    deployments = config_manager.get_config(site_key, "olympus", "default", "deployments")
    if not deployments:
        raise Exception("No configuration has been set for this site")
```

So the lookup order is:
1. `service=olympus`, `handler=<feature_group>`, `property=deployments`
2. Fallback: `service=olympus`, `handler=default`, `property=deployments`
3. If both are missing → **500 error**

### Required config structure

The config store endpoint: `GET http://configstore/sites/{sitekey}/config?service=olympus`

Must return (under `configs.olympus`):

```json
{
  "configs": {
    "olympus": {
      "user": [
        {
          "name": "deployments",
          "value": [
            {
              "deployment_id": "dep-1",
              "model_id": "model-1",
              "model_metadata": {
                "features": [
                  {
                    "click_pids":    { "suffix": "v1", "type": "string" },
                    "cart_pids":     { "suffix": "v1", "type": "string" },
                    "order_pids":    { "suffix": "v1", "type": "string" },
                    "recent_queries":{ "suffix": "v1", "type": "string" }
                  }
                ],
                "features_metadata": {
                  "v1": {
                    "retrieval_order": 0,
                    "json_encoded": false,
                    "compression": false
                  }
                }
              }
            }
          ]
        }
      ],
      "default": [
        {
          "name": "deployments",
          "value": [ ... ]
        }
      ]
    }
  }
}
```

Optional property: `clickstream_sitekey` — overrides the site key used when reading realtime Redis events (useful when a site's clickstream events were written under a different sitekey).

### Albus client env vars

| Var | Default | Description |
|---|---|---|
| `ALBUSCLIENT_ALBUS_HOST` | `http://configstore-int.search` | Configstore base URL |
| `ALBUSCLIENT_NATS_HOSTS` | `nats://bridge-nats.search:4222` | NATS for live config updates |
| `ALBUSCLIENT_ENABLE_HANDLER_CACHE` | `False` | Cache handler lookups |
| `ALBUSCLIENT_ENABLE_PROPERTY_CACHE` | `False` | Cache property lookups |
| `ALBUSCLIENT_ENABLE_SITE_CACHE` | `True` | Cache site-level lookups |

---

## Consumer Job — Event Ingestion

**File:** `job/src/main.py`

The consumer job bridges Kafka clickstream events into Redis DB 3 so the API can serve realtime user behavior.

### Flow

```
Kafka (topic: requests / tracker-events)
    │
    ▼ (NUM_THREADS=4 consumer threads)
Parse event payload
    │
    ├── site allowed? (ALLOWED_SITES list, refreshed from Albus each loop)
    ├── event type allowed? (ALLOWED_EVENTS)
    └── uid starts with UID_PREFIX?
              │
    ┌─────────┴───────────┐
  search event         click/cart/order
    │                      │
    ▼                      ▼
ZADD + SETSADD          RPUSH <site>.<uid>.<event>
query_metadata          LTRIM to MAX_LIST_SIZE
query_dedup_set         EXPIRE DEFAULT_TTL
```

### Event payload formats

**v1 format** (`EVENTS_FORMAT=v1`, topic: `requests`):
```json
{ "isitename": "site123", "userId": "uid-xxx", "action": "click", "pid": "p1", "date": 1700000000, "query": "" }
```

**v2 format** (topic: `tracker-events`):
```json
{
  "context": { "site": "site123" },
  "user": { "id": "uid-xxx" },
  "raw": { "action": "click" },
  "products": [{ "id": "p1" }],
  "misc": { "query": "blue shoes" },
  "meta": { "date": 1700000000, "customerId": "cid-1", "netcoreId": "nc-1" }
}
```

In v2, the job also writes events for `customerId` and `netcoreId` (from `cid_list`), regardless of the UID prefix check.

### Key configuration

| Env var | Default | Description |
|---|---|---|
| `ALLOWED_EVENTS` | `click,cart,order,search` | Event types to process |
| `UID_PREFIX` | `uid-` | Required prefix for primary UID writes (US) or empty (APAC) |
| `MAX_LIST_SIZE` | `100` | Max events per user per type |
| `DEFAULT_TTL` | `259200` | Redis key TTL in seconds (3 days) |
| `KAFKA_TOPIC` | `requests` (v1) / `tracker-events` (v2) | Kafka topic |
| `NUM_THREADS` | `4` | Consumer thread count |

---

## Redis Keyspace

### DB 3 — Realtime Events (written by consumer, read by `user` feature group)

| Key | Type | Format | TTL |
|---|---|---|---|
| `<site>.<uid>.click` | LIST | `pid:timestamp` per entry | 3 days |
| `<site>.<uid>.cart` | LIST | `pid:timestamp` per entry | 3 days |
| `<site>.<uid>.order` | LIST | `pid:timestamp` per entry | 3 days |
| `<site>.<uid>.search` | LIST | `pid:timestamp` per entry | 3 days |
| `<site>.<uid>.query_metadata` | ZSET | member=query, score=`ts*100+freq` | 3 days |
| `<site>.<uid>.query_dedup_set` | SET | dedup keys `cleanQuery:timebucket` | 3 days |

List capped to last 100 entries (`LTRIM`). Query ZSET capped to top 100 by score.

**To inspect directly:**
```bash
redis-cli -n 3 LRANGE "<site>.<uid>.click" 0 -1
redis-cli -n 3 LRANGE "<site>.<uid>.cart" 0 -1
redis-cli -n 3 LRANGE "<site>.<uid>.order" 0 -1
redis-cli -n 3 ZREVRANGE "<site>.<uid>.query_metadata" 0 -1 WITHSCORES
```

### DB 0 — Offline / Materialized Features (written by Feathr/Spark ingestion)

| Key | Type | Description |
|---|---|---|
| `<site>_user_<suffix>:<uid>` | HASH | Offline user features from Feathr |
| `<site>_query_<suffix>:<query>` | HASH | Offline query features |
| `<site>_user_product_<suffix>:<uid>` | HASH | Per-user product affinity, fields `<pid>.<feature>` |
| `<site>_user_product_<suffix>_enc:<uid>` | STRING | Compressed JSON variant (APAC) |

---

## APAC vs Other Regions

No `if APAC` branches exist in Python code. Regional behavior is driven entirely by **Helm values per region**.

| Setting | APAC (`ap-southeast-1`) | US (`us-east-1`) |
|---|---|---|
| **Redis topology** | Redis Cluster (5 nodes, 50Gi each) | Redis standalone (1 master, 10Gi) |
| **`ENCODED_KEY_SUFFIX`** | `enc` (compressed JSON keys) | *(not set — plain hash keys)* |
| **`UID_PREFIX`** | `""` (no prefix filter — write for ALL UIDs) | `"uid-"` (only UIDs starting with `uid-`) |
| **`ALLOWED_EVENTS`** | `click,cart,order,search` | `click,cart,order` (no search) |
| **Consumer replicas** | 1 | 2 |
| **Consumer CPU** | 3 cores / 1Gi | 1 core / 512Mi |
| **Kafka brokers** | `platform-tracker-kafka.platform:9092` (single entry) | 5 dedicated Kafka broker hosts |
| **Configstore host** | `http://configstore.prod.ap-southeast-1.infra` | `http://configstore.prod.use-1d.infra` |
| **NATS host** | `nats://nats-bridge-internal.prod.ap-southeast-1.infra:4222` | `nats://nats-bridge-internal.prod.use-1d.infra:4222` |
| **Statsd host** | `statsd.prod.ap-southeast-1.infra` | `statsd.prod.use-1d.infra` |

**Key implications:**

1. **UID_PREFIX is empty in APAC** — the consumer writes events for all user IDs, not just those starting with `uid-`. If you're looking up events for a user in APAC, their UID does not need to have a prefix.

2. **Encoded keys in APAC** — `user_product` features are stored as compressed JSON blobs (`_enc` suffix) rather than flat Redis hashes. The API handles decompression automatically, but ingestion must write the compressed format.

3. **Search events only in APAC** — US consumers are configured with only `click,cart,order`. APAC additionally captures `search` events → `query_metadata` sorted sets.

4. **Cluster vs standalone Redis** — APAC uses a 5-node Redis Cluster; US uses a single Redis master. Code handles both via `REDIS_CLUSTER_ENABLE` flag — the `user` feature group and consumer job both branch on this.

---

## External Dependencies

| Service | Used by | Purpose |
|---|---|---|
| **Albus / Configstore** | App + Consumer | Dynamic deployment config, site-level feature schema |
| **NATS** | App (via Albus client) | Live config push updates |
| **Redis** (standalone or cluster) | App + Consumer | Realtime events (DB 3) + offline features (DB 0) |
| **Kafka** | Consumer job | Clickstream event ingestion |
| **Feathr** (+ Spark jar) | App + Ingestion | Online feature retrieval / batch materialization |
| **Datadog / StatsD** | App + Consumer | Metrics: request latency, event lag, Redis memory |
| **AWS S3** | Ingestion scripts | Source data for batch materialization |

---

## Environment Variables

### App service (`app/config/config.py`)

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `redis` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | `""` | Redis auth |
| `REALTIME_DATA_DB` | `3` | Redis DB for realtime events |
| `REDIS_CACHE_TTL` | `24` | Feature cache TTL in hours |
| `REDIS_CLUSTER_ENABLE` | `False` | Use Redis Cluster client |
| `ENCODED_KEY_SUFFIX` | `enc` | Suffix for compressed user_product keys; set to `""` for plain hashes |
| `ALBUS_SERVICE` | `olympus` | Service name for config lookups |
| `DEPLOYMENTS_PROPERTY` | `deployments` | Config property name for deployment list |
| `CLICKSTREAM_SITEKEY_PROPERTY` | `clickstream_sitekey` | Config property to override Redis event sitekey |
| `ALBUSCLIENT_ALBUS_HOST` | `http://configstore-int.search` | Configstore URL |
| `ALBUSCLIENT_NATS_HOSTS` | `nats://bridge-nats.search:4222` | NATS URL |
| `NATS_REGION` | `ap-southeast-1` | Region for NATS |
| `NATS_ENV` | `prod` | Environment for NATS |
| `MAX_BATCH_OUTPUT` | `1000` | Max features returned per batch sub-request |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `STATSD_HOST` | `stats1` | Datadog StatsD host |
| `STATSD_PORT` | `8125` | Datadog StatsD port |
| `REGION` | `travis` | Deployment region tag |
| `ENV` | `travis` | Deployment environment tag |

### Consumer job (`job/src/main.py`)

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `reranker-redis-master.search` | Redis host (same Redis as reranker) |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | `""` | Redis auth |
| `REDIS_CLUSTER_ENABLE` | `False` | Use Redis Cluster |
| `KAFKA_BOOTSTRAP_SERVERS` | 5-broker default list | Kafka brokers |
| `KAFKA_TOPIC` | `requests` (v1) | Kafka topic |
| `ALLOWED_SITES` | 3-site default | CSV of site keys to process |
| `ALLOWED_EVENTS` | `click,cart,order,search` | Event types to process |
| `UID_PREFIX` | `uid-` | Prefix filter for primary UID writes |
| `NUM_THREADS` | `4` | Kafka consumer threads |
| `MAX_LIST_SIZE` | `100` | Max events stored per user per event type |
| `DEFAULT_TTL` | `259200` | Redis key TTL (3 days) |
| `EVENTS_FORMAT` | `v1` | Payload format: `v1` or other |
| `ALBUSCLIENT_ALBUS_HOST` | `http://configstore.prod.use-1d.infra` | Configstore (job uses `watcher` service) |

---

## Deployment (Helm / Kubernetes)

### Chart structure

```
helm/olympus/
├── Chart.yaml
├── values.yaml               # base defaults
├── templates/
│   ├── deployment.yaml       # API service deployment
│   ├── consumer-deployment.yaml  # consumer job deployment
│   ├── service.yaml
│   ├── ingress.yaml
│   └── hpa.yaml
└── values/
    ├── us-east-1.yaml
    ├── eu-west-2.yaml
    ├── ap-southeast-1.yaml
    ├── ap-southeast-1-demo.yaml
    ├── ap-southeast-2.yaml
    ├── ap-southeast-2-demo.yaml
    └── gcp-pilotrc01-use4.yaml
```

The chart optionally deploys a bundled `redis` (standalone) or `redis-cluster` (APAC) via Bitnami sub-charts, controlled by `redis.enabled` and `redis-cluster.enablechart` in region values.

### Resource sizing (production)

| Component | CPU | Memory |
|---|---|---|
| API service | 2 cores (request+limit) | 1Gi req / 1.5Gi limit |
| Consumer (US) | 1 core | 512Mi |
| Consumer (APAC) | 3 cores | 1Gi |
| Redis standalone (US) | 3 cores | 6Gi |
| Redis cluster node (APAC) | 3 cores | 12Gi each × 5 nodes |

---

## Common Failure Modes & Debugging

### "No configuration has been set for this site"

**HTTP:** 500  
**Cause:** Configstore has no `deployments` property under `service=olympus` for the given sitekey (neither handler-specific nor `default`).  
**Check:**
```bash
curl "http://configstore/sites/{sitekey}/config?service=olympus"
```
If `configs` is `{}` or missing the `olympus` key, the site is not configured.  
**Fix:** Add deployment config to Albus for the site under service `olympus`.

---

### "Failed to fetch Events" from reranker

**Cause:** Reranker's `/v1.0/sites/{sitekey}/events?platform=olympus` calls the Olympus `feature_group=user` API internally. The same config-missing error propagates up.  
**Also check:** `platform=olympus` must be explicitly passed; without it, reranker defaults to Aerospike.

---

### Events present in Redis DB 3 but API returns empty features

1. Verify the key exists with the correct sitekey:
   ```bash
   redis-cli -n 3 KEYS "<site>.<uid>.*"
   ```
2. Check if `clickstream_sitekey` is configured in Albus and differs from the API sitekey.
3. Check `UID_PREFIX` — in US, if the UID doesn't start with `uid-`, the consumer never wrote it.

---

### `user_product` returning empty in APAC

APAC uses compressed JSON keys. Verify:
- `ENCODED_KEY_SUFFIX=enc` is set on the API service.
- The ingestion job wrote to `<site>_user_product_<suffix>_enc:<uid>` (not the plain hash key).

---

### Consumer lag growing

- Check Kafka lag for group `udata-group` on the topic.
- `ALLOWED_SITES` is refreshed from Albus each consumer loop; if Albus is down, it falls back to the env-var list.
- Redis push failures increment `olympus.failure` metric in Datadog and block offset commit, causing re-processing.

---

### Querying a user's events directly (bypass API)

```bash
# Replace <site> and <uid> with actual values
redis-cli -h <redis-host> -a <password> -n 3 LRANGE "<site>.<uid>.click" 0 -1
redis-cli -h <redis-host> -a <password> -n 3 LRANGE "<site>.<uid>.cart" 0 -1
redis-cli -h <redis-host> -a <password> -n 3 LRANGE "<site>.<uid>.order" 0 -1
redis-cli -h <redis-host> -a <password> -n 3 ZREVRANGE "<site>.<uid>.query_metadata" 0 -1 WITHSCORES
```

Values in event lists are `pid:timestamp`.
