# User Behaviour (UB) — KT Guide

**Audience:** Engineers triaging ranking, Cerebro batch, or Redis freshness issues.  
**Architecture diagram (for presentation):** [diagrams/user_behaviour.excalidraw](./diagrams/user_behaviour.excalidraw) — black-and-white swimlanes; regenerate with `python3 documentation/peds/diagrams/_build_bw_diagrams.py`  
**Related enablement:** [Enablement reference §5 — User Behaviour](../enablement.md#5-user-behaviour-ub)

---

## 1. Introduction

**User Behaviour (UB)** boosts products at search time using **precomputed query→product engagement scores** derived from clickstream (clicks, carts, orders). It is a **batch aggregation pipeline** plus **Redis lookup** — not an online ML model and **not served by Reranker** for the classic UB path.

Cerebro’s Kubeflow pipeline is named *“train and deploy a UB model”* (`cerebro/app/algos/ub.py`), but operationally the flow is **Spark scoring → Redis deploy**.

---

## 2. Problem statement

Shoppers repeatedly click and buy certain products for specific queries. Keyword relevance alone may not surface those products high enough. UB encodes **historical query–product affinity** so repeat intent gets a ranking nudge.

**Constraints**

- Scores are **query-level** (search ruleset) or **browse/category ruleset** — not per-user in this path (contrast with **personalization / Olympus**, which is separate).
- Lookup uses the **analyzed query** from Analysis — tokenizer/normalization mismatch causes cache misses.

---

## 3. How the algorithm works

### 3.1 Offline scoring (Spark QPS)

**Component:** `cerebro/components/transform/query_product_score` (`component_spark.py`).

| Step | Behavior |
|------|----------|
| Ingest | Parquet clickstream from regional S3 (`CLICKSTREAM_SITE_BASE_PATH` in `cerebro/app/constants.py`) |
| Events | `CLICK`, `CART`, `ORDER` (defaults weighted e.g. click:1, cart:10, order:30 in pipeline args) |
| Decay | Time decay by `clickstream_duration_in_days` |
| Rulesets | **search** — key on query column; **browse** — key on category/node column |
| Output | Top products per query with scores + optional stats |

Pipeline definition: `cerebro/app/algos/ub.py` — optional `transform/product_diversity`, then `deploy/user_behavior`.

### 3.2 Deploy to Redis

**Library:** `cerebro-base/libs/cerebro/user_behavior.py` — class `UserBehavior`.

| Item | Value |
|------|--------|
| Boost hash key | `{siteKey}.{ruleset}` — field = query string, value = JSON `map[uniqueId]score` |
| Stats key | `ub.stats.{siteKey}.{ruleset}` |
| Browse ruleset | Persisted Redis ruleset name **`category`** when ruleset is `browse` (deploy alias) |
| Experiment | `{siteKey}_{expId}` when `user.behaviour.expId` is set |

**Deploy component:** `cerebro/components/deploy/user_behavior/main.py` reads pipeline JSON and calls `persist_data`.

### 3.3 Online application (Mimir)

**Graph:** Analysis runs before UB on search (`mimir/internal/network/query_modifier.go` — `ANALYSIS → UB`).

**UB node** (`mimir/internal/network/node/ub.go`):

1. Uses **analyzed query** (`analysisRes.RawQuery`) as Redis hash field.
2. Fetches boosts from `mimir/internal/downstream/ub/redis.go`.
3. Passes scores into query builder merge.

**Solr application** (`mimir/internal/network/node/qb/product_scores/product_scores_builder.go`):

- Merges UB with **Popularity** into `ub.map` boost payload.
- Applies via Solr `fieldcompare` — **additive** or **multiplicative** when `mimir.scores.multiplicative=true` (`score_merger.go`).

**Multiplier:** `mimir.ub.multiplier` applied in `ub_helper.go` (schema default `0`; runtime may treat 0 as 1.0 — verify per site).

---

## 4. Architecture (batch + online)

### 4.1 Batch orchestration

```
Gimli/Albus site list
  → Cerebro CronJob runner (ALGORITHM=user-behaviour)
  → Kubeflow pipeline (user-behaviour)
  → Spark QPS → deploy/user_behavior → UB Redis (ub-ec.*:6379, DB 0)
```

**Example schedule (prod AS1 gitops):** `30 6 * * 0,2,4,6` in `gitops/clusters/ai01.gcp.prod.as1/env/team-ai/service/cerebro.yaml` (plus site-specific workflows).

**Morpheus** monitors pipeline/Redis freshness (`morpheus/workflow/algo_score_cerebro_client.py`, Celery ~9:30 UTC on Sun/Tue/Thu/Sat per `morpheus/cmd/settings.py`).

### 4.2 Online path

```
Request → Mimir ConfigMW → Analysis → UB node (Redis) ∥ Popularity (optional)
       → BuildProductScores → SEARCH → Solr
```

**Env:** `UB_REDIS_URL` on Mimir pods (`mimir/build/helm/mimir/values/*`, gitops overlays).

### 4.3 External / custom UB

- `external_s3_path` + `source=external` in `ub.py` skips QPS and loads external scores.
- Notebook: `cerebro/notebooks/external_ub.ipynb`.

---

## 5. Training inputs & outputs (summary)

| Phase | Input | Output |
|-------|--------|--------|
| QPS | Clickstream parquet, impression weights, duration, optional feed | JSON / S3 intermediate per Kubeflow run |
| Diversity (optional) | QPS output | Reranked product lists |
| Deploy | Scored JSON | Redis hashes + stats |
| Configure (Albus) | `deployments?service=cerebro&handler=user-behaviour` | `model_metadata`: weights, duration, max_queries, experiment_id, etc. (`cerebro/build/setup/config-setup.sh`) |

---

## 6. Enablement

| Goal | Params |
|------|--------|
| Hard enable UB node | `mimir.ub.enable=true` (schema default **false**) |
| Algorithm on | `user.behaviour=true` |
| Disable per request | `user.behaviour=false` |
| Experiment bucket | `user.behaviour.expId` |
| Merge mode with Popularity | `mimir.scores.multiplicative` |

**Morpheus Fernir:** “Ranking - UB” — disabled when `user.behaviour=false` (`morpheus/workflow/fernir_algorithm_checker.py`).

**IQ Recs:** `boost_type=negative` forces UB off (documented in `documentation/iqrecs.md`).

---

## 7. Dependencies

| Component | Role |
|-----------|------|
| Clickstream S3 | Primary signal |
| Cerebro + Kubeflow | Batch compute and deploy |
| UB Redis (`ub-ec`) | Serving store (DB 0) |
| Analysis (Asterix) | Query text for Redis key |
| Popularity (optional) | Merged into same `ub.map` |
| Morpheus | Freshness alerting |
| Albus | Site deployments and mimir enable flags |

**Reranker / Olympus:** Personalization is a **different** system; IQ recs may use `ub.map` param name but not UB Redis.

---

## 8. Legacy & related paths (not primary)

| Path | Notes |
|------|--------|
| **Aggregator** `UBScoresGenerator` | Java batch; Redis schema `{site}.ub.pids.{query}` — `aggregator/.../UBScoresGenerator.java` |
| **search-brewer** `PopularityServicePipe` | Reads Cerebro-style UB keys; legacy Brewer stack |
| **Aggregator REST** `/user-behavior` | `UserBehaviorServiceManager.java` |

**Uncertainty:** Whether aggregator jobs still write prod Redis for any tenants — Cerebro cron is the documented modern path.

---

## 9. Operational FAQs & failure modes

| Symptom | Likely cause | Action |
|---------|----------------|--------|
| UB never applies | `mimir.ub.enable` false (default) or `user.behaviour=false` | Check Albus + request params |
| Empty boost, no error | Redis miss — analyzed query ≠ stored query | Compare Analysis output vs Redis field |
| Browse UB missing | Ruleset `category` vs handler `browse` mismatch | Confirm deploy ruleset and Mimir `RuleSet` |
| Pipeline exit -1 | No clickstream files in window | `component_spark.py` |
| Stale scores alert | Failed/missed cron | Morpheus `stale_pipeline` / `stale_redis_scores` |
| UB metric “500” with empty data | Empty Redis response coded as 500 | `ub.go` — may be noisy |
| Multiplier looks wrong | Schema default 0 vs runtime | Validate `mimir.ub.multiplier` on site |

---

## 10. Triage checklist

1. Morpheus / Fernir: is “Ranking - UB” enabled for the site?
2. Recent successful Cerebro `user-behaviour` run for site + deployment id.
3. `HGET {site}.{search|category} "<analyzed query>"` on regional UB Redis.
4. Mimir search with `debug=true` — metadata `user.behaviour`, `ub.map` presence.
5. If experiment: confirm `user.behaviour.expId` and Redis key suffix.

---

## 11. Code index

| Topic | Path |
|-------|------|
| KFP pipeline | `cerebro/app/algos/ub.py` |
| Spark QPS | `cerebro/components/transform/query_product_score/src/component_spark.py` |
| Redis persist | `cerebro-base/libs/cerebro/user_behavior.py` |
| Deploy | `cerebro/components/deploy/user_behavior/main.py` |
| Mimir UB node | `mimir/internal/network/node/ub.go` |
| Redis client | `mimir/internal/downstream/ub/redis.go` |
| Score merge | `mimir/internal/network/node/qb/product_scores/` |
| Enablement doc | `documentation/enablement.md` |
| Legacy generator | `aggregator/.../UBScoresGenerator.java` |
