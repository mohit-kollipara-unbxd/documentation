# Feathr and FeathrStore — explained simply

Imagine you run a shop. You keep notes about customers: what they clicked, what they searched, what products they might like.

**Redis** is the big notebook where all notes are stored.

**Feathr** is like a special helper who knows *how to write notes in a certain format* and *how to read them back*. It does not have its own notebook. It always uses Redis.

In your project (Olympus), Feathr is that helper. Redis is the real storage.

---

## Part 1: What are FeathrStore and FeathrClient?

### FeathrClient — the helper

`FeathrClient` is the main object from the Feathr library. You talk to it when you want to:

- Read features from Redis (online)
- Write features to Redis via Spark jobs (batch/offline)

Think of it as a **translator between your app and Redis**.

Your code says:

> “Give me `recent_queries` for user `abc123` from table `mysite_user_monthly`.”

Feathr figures out **which Redis keys to open** and **how to read the values**.

### FeathrStore — your wrapper

`FeathrStore` is **your own small class** in `app/feathrstore.py`. It is not part of Feathr itself.

It does one job: **set up FeathrClient when the API starts**.

```10:47:app/feathrstore.py
class FeathrStore:
    def __init__(self):
        jar_name = glob.glob("./*.jar")[0]
        # builds feathr_config.yaml pointing at Redis
        self.client = FeathrClient(str(feathr_workspace_folder))
```

On startup it:

1. Finds a Java file (`feathr_2.12-1.0.0.jar`) on disk
2. Writes a config file saying “use Redis at this host/port”
3. Creates `FeathrClient`

Then in `router.py`:

```22:23:app/router.py
fs = FeathrStore()
fsclient = fs.get_client()
```

Every API request gets `fsclient` passed in so feature code can read from Redis through Feathr.

**Simple summary:**

| Thing | What it is |
|---|---|
| `FeathrStore` | Your setup code — creates the client once at startup |
| `FeathrClient` | The actual Feathr library object — reads/writes Redis |

---

## Part 2: What does Feathr actually do in Olympus?

Feathr has two jobs here: **writing** (batch) and **reading** (API).

### Job 1: Writing — batch ingestion (offline data)

Someone has a big CSV file on S3 with user or query features.

The ingestion script (`build/ingestion/ingestion.py`) does this:

1. **Read CSV** — Feathr calls this `HdfsSource` (just a file path, not necessarily HDFS)
2. **Define what columns mean** — `Feature`, `FeatureAnchor`, `TypedKey` (like a schema: “uid is the key, recent_queries is a string column”)
3. **Register schema** — `client.build_features()`
4. **Run a Spark job** — `client.materialize_features()` with `RedisSink`
5. **Spark writes rows into Redis** under a table name like `mysite_user_monthly`

So Feathr + Spark are the **factory** that takes a spreadsheet and puts structured data into Redis.

For `user` and `query` feature groups, ingestion uses this path.

For `user_product`, you **already skip Feathr** and write to Redis yourself with pandas + `redis-py`.

### Job 2: Reading — API serving (online data)

When a reranker or search service calls your API:

1. API gets request: site, user id, which features
2. Code calls `feathrClient.multi_get_online_features(table_name, [userId], [field1, field2, ...])`
3. Feathr talks to Redis, finds the right keys, returns values
4. Your Python code does extra work (merge realtime data, filters, etc.)
5. API returns JSON

Used in:

- **User offline features** — `user_feature.py` line 96
- **Query features** — `query_feature.py` line 27 (fully through Feathr)

---

## Part 3: How does it work inside? (step by step)

### When the API pod starts

This is important and a bit surprising.

1. `FeathrStore()` runs
2. It loads **Spark** (a big data engine) and a **Java JAR**
3. It connects to Redis
4. `FeathrClient` is ready

So every API pod boots Spark + Java **even though serving only reads from Redis**. That is heavy. Feathr was built for batch jobs; here it is also used as a Redis reader.

### When someone asks for user features

Example: get `recent_queries` and `click_pids` for user `u123` on site `shop`.

**Step A — offline part (through Feathr)**

```python
feathrClient.multi_get_online_features("shop_user_monthly", ["u123"], ["recent_queries", "click_pids"])
```

Inside Feathr (conceptually):

1. Table name = `shop_user_monthly` (site + feature group + suffix)
2. Entity id = `u123`
3. Feathr builds Redis keys in **its own format** (your code uses `_construct_redis_key` — keys use `#` as separator, e.g. something like `shop_user_monthly#u123#recent_queries`)
4. Redis `MGET` or similar
5. Returns `{ "u123": [value_for_recent_queries, value_for_click_pids] }`

**Step B — realtime part (NOT through Feathr)**

For `click_pids`, your code also reads Redis directly:

```152:153:app/feature_groups/user/user_feature.py
        redis_key = f'{sitekey}.{uid}.{action}'
        values = self.redis_client.lrange(redis_key, 0, -1)
```

Key looks like `shop.u123.click` — a **different format**, written by the Kafka consumer, not Feathr.

**Step C — merge in Python**

Offline `click_pids` might be a JSON string from batch. Realtime is a list like `pid:timestamp`. Your code combines them, deduplicates, sorts. Feathr does not do this — **your Python code does**.

So for `user` features, Feathr is only half the story.

### When someone asks for query features

Simpler: **everything goes through Feathr**.

```python
feathrClient.multi_get_online_features("shop_query_search", ["laptop", "phone"], fields)
```

No direct Redis in `query_feature.py`. Feathr reads, you format, you return.

### When someone asks for user_product features

**Feathr is not used at all.**

`UserProductCondensedFeatures` uses `redis-py` directly. The `feathrClient` parameter is even typed as `None` — it is ignored.

Two storage styles:

1. **Hash** — key `shop_user_product_monthly:u123`, fields like `productId.score`
2. **JSON + compression** — key `shop_user_product_monthly_enc:u123`, one zlib-compressed JSON blob with all products

That is **your** design, not Feathr’s.

### When batch ingestion runs

For `user` / `query`:

```
CSV file → Spark (via Feathr JAR) → transform rows → RedisSink → Redis keys in Feathr format
```

For `user_product`:

```
CSV file → pandas → loop users → redis.hset() or redis.set(compressed_json) → your key format
```

---

## Part 4: What features does Feathr support? (full platform vs what you use)

Feathr as a **platform** can do a lot. You use a small slice.

### What Feathr can do (in general)

| Capability | What it means |
|---|---|
| Offline stores | Read training data from S3, ADLS, Snowflake, JDBC |
| Feature definitions | Anchors, features, typed keys — schema for ML features |
| Materialization | Spark jobs that compute features and push to online store |
| Online store | Redis, Azure Table, etc. |
| Online serving | `get_online_features`, `multi_get_online_features` |
| Feature registry | Purview / API catalog (metadata, lineage) |
| Point-in-time joins | Join user + item features for training (ML use case) |
| Spark backends | Local, Databricks, Azure Synapse |

### What Olympus actually uses

| Used | Not used |
|---|---|
| `FeathrClient` init | Feature registry / Purview |
| `multi_get_online_features` | Offline store in serving path |
| `RedisSink` + `materialize_features` | Databricks / Synapse (you use local Spark) |
| `HdfsSource`, `FeatureAnchor`, `build_features` | Feathr point-in-time joins (you do joins in pandas) |
| `_construct_redis_key`, `redis_client` (one endpoint) | Most of the “full platform” features |

So in Olympus, Feathr is basically:

1. **Spark → Redis writer** for batch `user` and `query`
2. **Redis reader with a fixed key layout** for serving `user` (offline) and `query`

---

## Part 5: Why use Feathr if everything is in Redis?

Good question. Honest answer: **for most of what you do today, you do not strictly need it.**

Feathr gives you:

1. **A standard key layout** — you do not hand-write Redis keys for `user`/`query`; Feathr knows the pattern
2. **Spark materialization** — CSV → Redis without writing your own Spark job
3. **Typed features** — BOOLEAN, INT64, STRING, JSON_STRING, etc. on write

What it does **not** give you in your setup:

- Compression (you built that yourself for `user_product`)
- JSON blob per user (same — your code)
- Realtime clickstream (Kafka job writes directly)
- Merging offline + realtime (your pandas logic)

So Feathr is a **convenience layer** for one style of data. Half your system already ignores it.

---

## Part 6: Can you move out of Feathr and use compression + JSON encoding?

**Yes.** You already did for `user_product`.

### What you have today without Feathr

**Ingestion** (`ingestion.py`):

- `store_features_json_compressed()` — zlib-compress JSON, `redis.set(key, compressed_data)`
- `store_features()` — Redis hash per user

**Serving** (`user_product_condensed_feature.py`):

- If `json_encoded` in config → read one key, decompress, `json.loads`
- Else → `hgetall` on hash

Config drives it via Albus (`json_encoded`, `compression`, `retrieval_order`).

### What you would need for `user` and `query`

Same idea:

1. **Pick your key format** — e.g. `shop_user_monthly:u123` → one JSON blob (optionally zlib)
2. **Change ingestion** — stop `RedisSink`; write like `user_product` (pandas or a small script)
3. **Change serving** — stop `multi_get_online_features`; read/decompress/parse in Python (or Go later)
4. **Migration** — either re-ingest old data into the new keys, or support both formats during transition

Compression and JSON encoding are **not Feathr features** in your stack. You own them. Feathr stores mostly **flat typed fields per entity** in its Redis layout.

### Tradeoffs

**Leaving Feathr — pros:**

- API pods no longer need Spark + Java JAR at startup
- One Redis access pattern everywhere (like `user_product`)
- Full control: compression, JSON shape, TTL, key naming
- Simpler dependencies (no forked `unbxd/feathr`)

**Leaving Feathr — cons:**

- You must document/replicate Feathr’s Redis key format **or** re-ingest `user`/`query` data
- You lose Spark materialization for free — replace with pandas scripts (you already do for `user_product`) or keep a separate batch job
- One-time migration work for data already in Feathr-shaped keys

---

## Part 7: Put it all together (one story)

1. **Redis** = the notebook where everything lives.

2. **Feathr** = a helper that:
   - Writes some pages (batch `user`/`query`) in a specific format via Spark
   - Reads those pages back via `multi_get_online_features`

3. **FeathrStore** = your code that turns the helper on when the API starts.

4. **FeathrClient** = the helper itself.

5. **Other parts of Olympus** write/read the notebook **directly**:
   - Kafka job → `shop.u123.click` lists
   - `user_product` → compressed JSON or hashes
   - User realtime features → merged in Python, not by Feathr

6. **Moving out of Feathr** = stop using the helper; read/write Redis yourself. You already know how — `user_product` is the template. Add compression/JSON for `user` and `query` the same way.

---

## Quick reference: who reads/writes what

| Data | Written by | Read by | Format |
|---|---|---|---|
| User offline (`recent_queries`, etc.) | Feathr Spark → RedisSink | FeathrClient | Feathr Redis keys |
| User realtime (clicks, carts) | Kafka consumer | Direct redis-py | `site.uid.click` lists |
| Query features | Feathr Spark → RedisSink | FeathrClient | Feathr Redis keys |
| User × product features | pandas → direct Redis | Direct redis-py | Hash or zlib JSON |

If you want to go deeper on one piece next, the most useful is **exactly how Feathr lays out Redis keys** for `user`/`query` (so you know what to replace when you drop Feathr). I can trace that from your `get_user_products` endpoint and ingestion table names if you want.