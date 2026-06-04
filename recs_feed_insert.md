# Recs Feed Insert — Full Architecture & Flow

> **Services involved:** Mozart → Deadpool → RabbitMQ → Deadpool Worker → Hodor → Aerospike + PostgreSQL + Redis

---

## Architecture Diagram

```
                         ┌──────────────────────────────────────────────────────────────────────┐
                         │                        DEADPOOL  (:6969)                             │
                         │                                                                      │
Mozart / Caller ─────────▶  POST /recs/feed/snapshot                                           │
  (Event JSON)           │       │                                                              │
                         │       │ go service.NewIngestEvent()   ← async, returns 200 immediately
                         │       │                                                              │
                         │       ▼                                                              │
                         │  AMQP Publisher                                                      │
                         │  exchange: amq.direct                                                │
                         │  routing key: recs.feed.snapshot                                     │
                         └──────────────────────┬───────────────────────────────────────────────┘
                                                │ json.Marshal(Event)
                                                ▼
                                    ┌───────────────────────┐
                                    │  RabbitMQ             │
                                    │  Queue: recs.feed.queue│
                                    └──────────┬────────────┘
                                               │ consume
                         ┌─────────────────────▼────────────────────────────────────────────────┐
                         │               DEADPOOL FEED WORKER  (10 goroutines)                  │
                         │                                                                       │
                         │  1. Fetch config       ──▶  Albus (field_mapping, variants.fields)   │
                         │  2. Fetch field meta   ──▶  Odin (getIndexFields)                    │
                         │  3. Create/alter schema──▶  Hodor POST /products/schema              │
                         │  4. Load prev snapshot ──▶  Redis SMEMBERS deadpool:products:{site}  │
                         │  5. For each S3 key in event.Keys:                                   │
                         │       a. Download JSONL ──▶  S3 GetObject                            │
                         │       b. Parse each line (map[string]interface{})                    │
                         │       c. CatPathTransform (expand categoryPath / categoryPathId)     │
                         │       d. Project fields (keep only configured fields)                │
                         │       e. Hash product   ──▶  Redis HGET deadpool:producthash:{site}  │
                         │            ├─ unchanged: skip                                        │
                         │            └─ changed:   Redis HSET + add to batch                  │
                         │       f. Every 200 products: InsertBatch ──▶  Hodor                  │
                         │  6. Post-processing:                                                  │
                         │       a. IDs in prev-set but NOT seen = deleted products             │
                         │          ──▶  Hodor DELETE /products?id=... (batches of 50)          │
                         │          ──▶  Redis SREM deadpool:products:{site}                    │
                         │       b. New IDs ──▶ Redis SADD deadpool:products:{site}             │
                         │       c. Clear Hodor Redis cache (SCAN + DEL)                        │
                         └──────────────────────────┬────────────────────────────────────────────┘
                                                    │
                                                    │  POST /v2/sites/{site}/products/_insertbatch?isfilter=true
                                                    ▼
                         ┌──────────────────────────────────────────────────────────────────────┐
                         │                         HODOR  (:7171)                               │
                         │                                                                       │
                         │  InsertBatchV2:                                                       │
                         │    1. Get active.set.version from Albus (V1 or V2)                   │
                         │    2. Always:    Aerospike Put (inactive set)                        │
                         │    3. isfilter=true: PostgreSQL UPSERT                               │
                         └────────────┬──────────────────────┬─────────────────────────────────┘
                                      │                       │
                          ┌───────────▼──────────┐  ┌────────▼──────────────┐
                          │     AEROSPIKE         │  │     POSTGRESQL        │
                          │  (fast detail reads)  │  │  (filterable catalog) │
                          └───────────────────────┘  └───────────────────────┘
```

---

## Step-by-Step Flow

### Step 1 — Mozart triggers Deadpool

Mozart (or any upstream caller) sends:

```
POST http://deadpool:6969/recs/feed/snapshot
Content-Type: application/json

{
  "id": "e1f56121a89acb573b10e72",
  "site": "test-site-1",
  "timestamp": 1658772407,
  "count": 1,
  "keys": [
    "deadpool/build/test-files/0.json"
  ]
}
```

- `site` — the site key (used as namespace everywhere)
- `keys` — list of S3 object paths containing the snapshot JSONL files

Deadpool immediately returns `200 {"response": "Event inserted into queue for processing"}` and fires the rest **asynchronously**.

---

### Step 2 — Publish to RabbitMQ

| Setting | Value |
|---------|-------|
| Exchange | `amq.direct` |
| Routing key | `recs.feed.snapshot` |
| Queue | `recs.feed.queue` (durable) |
| Body | `json.Marshal(Event)` — same struct as HTTP body |
| Delivery | `SendAndForget` — no reply waited on |

The publish is **fire-and-forget**. The HTTP response does not depend on it succeeding.

---

### Step 3 — AMQP Consumer picks up the message

The Deadpool feed worker pool (10 goroutines by default) consumes from `recs.feed.queue`.

1. `json.Unmarshal(body)` → `event_en.Event`
2. Calls `service.NewFeedEvent(ctx, event)`
3. On success: `m.Ack(false)`

---

### Step 4 — Feed Worker: Config & Schema Setup

Before touching products, for each feed event:

1. **Albus** — fetches `recommender` / `default` / `field_mapping` and `hodor` / `default` / `variants.fields`
2. **Odin** — `GET {odin.url}/api/{siteKey}/getIndexFields` — returns field metadata (name, type, searchable/filterable flags)
3. **Hodor** — `POST /sites/{siteKey}/products/schema` — creates or alters the PostgreSQL table to have the required columns

---

### Step 5 — Load Previous Snapshot from Redis

```
Redis command:  SMEMBERS deadpool:products:{siteKey}
Returns:        set of uniqueId strings from the LAST run
```

This set is loaded into an in-memory `sync.Map` called `prevSnapshotProducts`. As products are seen in the current run, their IDs are deleted from this map. Anything still remaining at the end = no longer in the catalog.

---

### Step 6 — S3 Download + Per-Product Processing

For each S3 key in `event.Keys`:

**Download:**
```
AWS S3 GetObject
  Bucket: io-feed-snapshots-us-east-1/  (env: DP_FEED_S3_BUCKET)
  Region: us-east-1                     (env: DP_FEED_S3_REGION)
  Key:    {value from event.Keys[i]}
```

File format: newline-delimited JSON (JSONL). Each line is one product object.

**Per product line:**

```
a. json.Unmarshal(line) → map[string]interface{}
b. CatPathTransform:
     - Expands "categoryPath" and "categoryPathId" into leveled fields:
       categoryPath1, categoryPath2, ... categoryPathN (max depth 30)
       categoryPath_uFilter, u_categoryPathId
     - Input: "id1|Electronics>id2|Phones"
     - Output: adds prefix-path entries at each level
c. Field projection:
     - Keep only fields listed in Albus field_mapping + variant fields
     - Result: projectedProduct = map[string]interface{}
d. Change detection (Redis):
     redisKey = "deadpool:producthash:{siteKey}"
     stored  = HGET redisKey {uniqueId}
     current = hashstructure.Hash(sortedProjectedProduct, FormatV2)
     - If current == stored  → SKIP (no write to Hodor)
     - If current != stored  → HSET redisKey {uniqueId} {current}  + add to batch
e. Snapshot membership:
     - If uniqueId in prevSnapshotProducts → delete from prevSnapshotProducts (still alive)
     - Else → mark as new in newProducts map
```

**Batching:**
- Batch accumulates up to **200** products
- On reaching 200 (or end of file) → `callDownstreamForInsertion` → Hodor

---

### Step 7 — Insert Batch to Hodor

```
POST {hodor_url}/v2/sites/{siteKey}/products/_insertbatch?isfilter=true
Body: JSON array of projected product maps ([]map[string]interface{})
```

`isfilter=true` tells Hodor to write to **both** Aerospike and PostgreSQL.

---

### Step 8 — Hodor Writes to Aerospike (always)

**Blue/Green Set Logic:**

The Aerospike sets are versioned (V1 / V2) to support atomic catalog flips. Hodor always writes to the **inactive** set:

| Albus `active.set.version` | Products written to | Variants written to |
|----------------------------|---------------------|---------------------|
| `V1` (V1 is live for reads) | `{siteKey}_V2` | `v_{siteKey}_V2` |
| `V2` (V2 is live for reads) | `{siteKey}_V1` | `v_{siteKey}_V1` |

- **Record key:** `uniqueId` (products), `variantId` (variants)
- **Bins:** all projected fields; bin names shortened to ≤14 chars via `ShortenBinName`
- **Write policy:** `RecordExistsAction = UPDATE` — full record overwrite
- **Namespace:** configured via `aerospike.ns` / `AERO_NS` env (default: `test`)

Readers (`_detail` API) always read the **active** set (opposite of where insert writes).

---

### Step 9 — Hodor Writes to PostgreSQL (when `isfilter=true`)

Dynamic per-site tables:

| Table | Primary Key | Notes |
|-------|-------------|-------|
| `{siteKey}` | `uniqueId TEXT` | All product fields |
| `v_{siteKey}` | `variantId TEXT` | Variant fields + `productId` FK |

**Upsert SQL (goqu-generated):**
```sql
INSERT INTO "{siteKey}" ("uniqueId", "field1", "field2", ...)
VALUES (...)
ON CONFLICT ("uniqueId") DO UPDATE SET
  "field1" = EXCLUDED."field1",
  "field2" = EXCLUDED."field2",
  ...
```

- Wrapped in a transaction (`BEGIN` / `COMMIT`)
- Array fields stored as `pq.Array` (PostgreSQL native arrays)
- Columns discovered dynamically from `information_schema.columns`
- Variants upsert runs in the same transaction on conflict `variantId`

---

### Step 10 — Post-Processing: Deletions + Cache Clear

After all S3 files are processed:

**Deletions:**
```
prevSnapshotProducts map still contains IDs = these products are GONE from new snapshot

For each removed ID (in batches of 50):
  → Hodor:  DELETE /sites/{siteKey}/products?id=id1,id2,...
  → Hodor:  DELETE from "{siteKey}" WHERE "uniqueId" IN (...)
  → Hodor:  DELETE from "v_{siteKey}" WHERE "productId" IN (...)
  → Hodor:  Aerospike Delete (note: uses hardcoded "V1" set — known bug)
  → Redis:  SREM deadpool:products:{siteKey}  [removed IDs]
```

**New product IDs registered:**
```
Redis:  SADD deadpool:products:{siteKey}  [new IDs seen this run]
```

**Hodor cache invalidation (Deadpool clears on Hodor's Redis):**
```
SCAN hodor:query:{siteKey}:*   → DEL all matches
SCAN hodor:products:{siteKey}:*→ DEL all matches
```

---

## Data Stored Per System

### Deadpool Redis (change tracking + snapshot membership)

| Key | Type | Content | TTL |
|-----|------|---------|-----|
| `deadpool:products:{siteKey}` | SET | All `uniqueId`s from the last snapshot | None |
| `deadpool:producthash:{siteKey}` | HASH | `uniqueId` → hash string of projected product | None |

### Hodor Redis (query + detail cache)

| Key | Type | Content | TTL |
|-----|------|---------|-----|
| `hodor:query:{siteKey}:{queryHash}` | STRING | snappy-compressed JSON of filter results | 24h (configurable) |
| `hodor:products:{siteKey}:{md5(fields)}` | HASH | `{productId}_{variantIds}` → snappy-compressed product JSON | None |
| `hodor:odin:{siteKey}` | STRING | Cached Odin field metadata | 24h |

Cache is written on **read** (filter queries, detail fetches) — never on insert. It's invalidated by Deadpool post-snapshot or by `DELETE /v3/sites/{sitekey}/cache`.

### Aerospike (Hodor)

| What | Detail |
|------|--------|
| Namespace | `aerospike.ns` (env `AERO_NS`, default `test`) |
| Product sets | `{siteKey}_V1`, `{siteKey}_V2` (blue/green) |
| Variant sets | `v_{siteKey}_V1`, `v_{siteKey}_V2` |
| Store sets | `s_{siteKey}`, `s_p_{siteKey}`, `s_v_{siteKey}` |
| Record key | `uniqueId` (products), `variantId` (variants) |
| Bins | All projected fields (bin names ≤14 chars) |
| Write policy | `RecordExistsAction = UPDATE` (full overwrite) |
| Purpose | Fast product detail lookups (`_detail` API) |

### PostgreSQL (Hodor)

| What | Detail |
|------|--------|
| Product table | `{siteKey}` |
| Variant table | `v_{siteKey}` |
| Store tables | `s_{siteKey}`, `s_p_{siteKey}`, `s_v_{siteKey}` |
| Schema | Dynamic — created/altered from Odin field metadata |
| Upsert | `ON CONFLICT ("uniqueId") DO UPDATE` |
| Purpose | Filterable catalog queries (`_filter` API) |

---

## How Updates Work

1. **Deadpool** detects the change via hash comparison (Redis `deadpool:producthash`). Unchanged products → skipped entirely.
2. **Changed products** are sent to Hodor's `_insertbatch` endpoint.
3. **Hodor** does a full-field upsert:
   - Aerospike: `Put` with `UPDATE` policy — overwrites all bins
   - PostgreSQL: `INSERT ... ON CONFLICT DO UPDATE` — overwrites all columns

There is no partial/merge update. If a field is absent from the new snapshot, it becomes `NULL` in Postgres on the next upsert.

---

## How Deletions Work

Products **not present** in the current S3 snapshot are deleted at the end of each run. This is a full catalog diff:

```
prev_snapshot (Redis SET)
  minus
current_snapshot (seen during this run)
  =
products to delete → Hodor DELETE → Aerospike + PostgreSQL
```

- First ingest: Redis set is empty → no deletions (safe cold start)
- Batched: 50 IDs per Hodor DELETE call
- Redis snapshot set is updated atomically after deletion

> **Known bug:** Hodor's Aerospike delete code uses hardcoded set name `"V1"` instead of `{siteKey}_V1` or `{siteKey}_V2`, meaning Aerospike records may not actually be deleted if the active version is V2.

---

## External Dependencies Summary

| Dependency | Used By | Purpose |
|------------|---------|---------|
| **Mozart** | upstream caller | Triggers Deadpool with Event + S3 keys |
| **S3** | Deadpool | Stores JSONL snapshot files |
| **RabbitMQ** | Deadpool | Decouples ingest HTTP from feed processing |
| **Albus / configstore** | Deadpool + Hodor | Field mappings, variant config, active set version |
| **Odin** | Deadpool + Hodor | Index field metadata for schema + query building |
| **Redis (Deadpool)** | Deadpool | Snapshot set, product hash for change detection |
| **Redis (Hodor)** | Hodor | Query + detail cache; cleared by Deadpool post-run |
| **Aerospike** | Hodor | Fast product detail reads (blue/green sets) |
| **PostgreSQL** | Hodor | Filterable catalog queries |
