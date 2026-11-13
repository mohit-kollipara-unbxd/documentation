# Popularity — KT Guide

**Audience:** Engineers triaging ranking boosts, Cerebro popularity runs, Redis/index issues, or v1/v2 migrations.  
**Architecture diagram (for presentation):** [diagrams/popularity.excalidraw](./diagrams/popularity.excalidraw) — black-and-white swimlanes; regenerate with `python3 documentation/peds/diagrams/_build_bw_diagrams.py`  
**Related enablement:** [Enablement reference §4 — Popularity](../enablement.md#4-popularity-additive--multiplicative-segments-trending)

---

## 1. Introduction

**Popularity** boosts products using **aggregate engagement signals** (clicks, carts, orders) optionally blended with **feed-derived attributes**. Scores are computed offline and applied at search time as Solr **boost functions** and/or **per-request score maps** merged with User Behaviour.

The platform has **three historical layers** that often coexist during migration:

| Generation | Compute | Serving | Typical Solr expression |
|------------|---------|---------|-------------------------|
| **Cerebro v2** | KFP `product_score` + `deploy/popularity` | Redis `pop.{site}` + optional indexed `unx_popularity_*` | `exists(unx_popularity_default)` boosts |
| **Cerebro v1** | Same scoring | HTTP **popularity-service** (:6004) | `popularity(uniqueId)` |
| **Aggregator legacy** | BQ/ES batch `PopularityScoreGenerator` | Redis formula lists + popularity-service | Feed field `popularity_score` |

**Reranker does not implement a popularity ranking path** in the local `reranker` repo (only shared Redis host references for unrelated features).

---

## 2. Problem statement

High-traffic products should rank competitively even when the query match is weak. Popularity provides a **site-wide or segment-aware demand signal** complementary to query-specific UB and semantic retrieve.

---

## 3. How the algorithm works (Cerebro modern path)

### 3.1 Transform: `product_score`

**File:** `cerebro/components/transform/product_score/src/component.py`

| Input | Processing |
|-------|------------|
| Clickstream parquet | Weighted CLICK/CART/ORDER + optional revenue |
| `clickstream_duration_in_days` | Exponential time decay |
| Optional feed | Attribute-weighted blend via `clickstream_feed_weight` |
| `group_by_store` | Per-store score maps when enabled |

**Normalization:** Min–max style scaling; `max_products` default **5000** (or dynamic threshold when `max_products=-1`).

**Failure:** No clickstream in window → `sys.exit(-1)`.

### 3.2 Deploy: v1 vs v2

**File:** `cerebro/components/deploy/popularity/src/component.py`

| `version` | Destination |
|-----------|-------------|
| **v2** | `cerebro-base/libs/cerebro/popularity.py` → Redis hash **`pop.{siteKey}`** or `pop.{siteKey}_{expId}` |
| **v1** | HTTP clear + POST to `POPULARITY_ENDPOINT` (`helper.py` — `/internal-services/popularity/{site}`) |
| **Optional** | `deploy/hodor` writes Solr fields `unx_popularity_{expId}` |
| **Solr reload** | When `refresh_scores=true` after v1 deploy |

**Default version bias:** `POP_V2_ENABLED` env in Cerebro config can default pipelines to v2.

**Pipeline:** `cerebro/app/algos/popularity.py`.

### 3.3 Legacy aggregator formulas

**File:** `aggregator/.../BaseSiteScoreGenerator.java`

| Formula | Idea |
|---------|------|
| **CLICKS** | Click share only |
| **SB** | Normalized clicks + carts + orders vs site totals |
| **CB** | Conversion-style weighting using site/product click ratios |

Top **10,000** products persisted; job gated by site `SEARCHHITS` tracking in `PopularityScoreGenerator`.

---

## 4. Online architecture (Mimir)

### 4.1 Network nodes

Registered in `mimir/internal/factory/fconf/conf_factory.go`:

1. **`PopularityNode`** — skipped unless `mimir.popularity.enable` (`popularity.go`); `HGETALL` on Redis via `mimir/internal/downstream/popularity/redis.go`.
2. **Query builders** — `BuildPopularity` / `BuildCustomPopularityBoosts` (`popularity_builder.go`, `popularity_factory.go`).
3. **`BuildProductScores`** — merges Redis popularity with UB for `fieldcompare` (`product_scores_builder.go`).

**Redis DB:** `POP_REDIS_DB` default **1** (same host as UB, different DB — `mimir/cmd/flags/flag.go`).

**Key bucketing:** `pop.{site}_{expId}_{fieldKey}_{apiParam}` when buckets/segments configured (`redis.go` `defaultBoostKeyGen`).

### 4.2 Dual serving modes

| Mode | Mechanism |
|------|-----------|
| **Indexed v2** | Solr field `unx_popularity_default` (or experiment id) — boost via `exists(...)` templates in `popularity_factory.go` |
| **Redis-backed** | Per-product boosts in request when popularity node runs and merges with UB |

**Trending:** `enableTrendingProducts` adds Solr `trend()` in boost strings — **trend data pipeline not verified in this workspace**.

### 4.3 Legacy Brewer path

**search-brewer** `PopularityServicePipe.java` injects same boost function templates without Mimir Redis node (shared pipe with UB — noted FIXME in source).

---

## 5. Training / batch inputs & outputs

| Stage | Input | Output |
|-------|--------|--------|
| `product_score` | Clickstream S3, optional feed, weights, duration | Scored product list JSON on S3 (Kubeflow artifact) |
| `deploy/popularity` v2 | Score JSON | Redis `pop.{site}` TTL via `hmset_with_ttl` |
| `deploy/popularity` v1 | Score JSON | popularity-service HTTP body `PopularityValues` |
| Hodor (optional) | Scores | Solr index fields |
| GitOps cron | `workflow: popularity` | Scheduled KFP runs (`gitops/.../cerebro.yaml`) |

**External scores:** `source=external` in deploy component.

**Autosuggest:** ruleset suffix `_auto` in deploy component.

---

## 6. popularity-service (v1) — provisioning

**GitOps:** `gitops/unbxd/team-platform/service/popularity-service/` — image `popularity-service:1.0.0`, port **6004**.

**Uncertainty:** Service **source repo is not in this workspace** (`unbxd/popularity-service` per gitops). Internal storage and Solr plugin wiring should be documented after cloning that repo.

**Aggregator client:** `PopularityScoreClient.java` — HTTP fetch for feed indexing `popularity_score`.

---

## 7. Enablement

Two-level gate (same pattern as UB):

```
mimir.popularity.enable=true
mimir.defaultPopularity.enable=true
enablePopularity=true   # soft toggle inside builder
```

**Disable:** `mimir.popularity.enable=false` or `mimir.defaultPopularity.enable=false` or `enablePopularity=false`.

**Segments:** `popularity.segments.enable`, `popularity.segments.segments` (JSON), `popularity.segments.multiplicative`.

**Related:** `mimir.scores.multiplicative`, `mimir.scores.disableDefaultPopularity`, `popularity.version`, `popularity.expId`, `mimir.popularity.multiplier`, `mimir.popularity.buckets`.

See [enablement.md §4](../enablement.md#4-popularity-additive--multiplicative-segments-trending).

---

## 8. Dependencies

| System | Role |
|--------|------|
| Clickstream S3 | Primary signal |
| Cerebro / Kubeflow | Modern batch path |
| Redis `ub-ec` (DB1 for pop) | v2 serving |
| popularity-service | v1 serving |
| Solr / Odin | Boost fns + `unx_popularity_*` (`odin/config/conf/default.json`) |
| Feed / Hodor | Optional index materialization |
| Mimir | Online application |
| Morpheus | Freshness — **v1 may skip Redis checks** (`algo_score_monitor_tasks.py` `_popularity_expects_redis_scores`) |
| Aggregator (legacy) | CB/SB jobs + `/pop-scores` API |

**UB interaction:** Additive or multiplicative merge in `score_merger.go`; disabling default Solr popularity when using merged scores: `mimir.scores.disableDefaultPopularity`.

---

## 9. Operational FAQs & failure modes

| Symptom | Likely cause | Action |
|---------|----------------|--------|
| No boost (Mimir) | `mimir.defaultPopularity.enable` false (common default) | Albus + request params |
| Empty HGETALL, no error | Wrong Redis key (expId, bucket, store suffix) | `defaultBoostKeyGen` vs site config |
| Popularity node metric 500 | Empty Redis response | `popularity.go` |
| Cerebro pipeline fails | No clickstream | `product_score` exit |
| Feed `popularity_score=0` | popularity-service non-200 | Aggregator client logs |
| v1 deploy OK, no Solr change | `refresh_scores=false` | Re-run with reload or manual Solr reload |
| v2 monitor quiet | Morpheus expects Redis for v2 only | Confirm `popularity.version` |
| Aggregator silent Redis fail | Caught `JedisDataException` | DAO TODO in aggregator |

**FAQ — v1 vs v2?** v1 → HTTP + `popularity()` Solr fn; v2 → `pop.*` Redis and/or indexed `unx_popularity_{expId}`.

---

## 10. Triage checklist

1. Confirm `popularity.version` and experiment id on request.
2. Check Fernir / Albus popularity handler enablement and Cerebro deployment metadata.
3. For v2: `HGETALL pop.{site}` on popularity Redis DB.
4. For v1: HTTP health on popularity-service; aggregator feed client logs.
5. Mimir `debug=true` — boost `bf`/`boost` params, product_scores merge.
6. Compare with UB: both use `product_scores` — verify `mimir.scores.multiplicative`.

---

## 11. Code index

| Topic | Path |
|-------|------|
| KFP pipeline | `cerebro/app/algos/popularity.py` |
| Scoring | `cerebro/components/transform/product_score/src/component.py` |
| Deploy v1/v2 | `cerebro/components/deploy/popularity/src/component.py` |
| Redis library | `cerebro-base/libs/cerebro/popularity.py` |
| Mimir node | `mimir/internal/network/node/popularity.go` |
| Redis client | `mimir/internal/downstream/popularity/redis.go` |
| QB / factory | `mimir/internal/network/node/qb/popularity_builder.go`, `popularity_factory.go` |
| UB merge | `mimir/internal/network/node/qb/product_scores/` |
| Aggregator | `aggregator/.../BaseSiteScoreGenerator.java`, `PopularityScoreGenerator` |
| Brewer legacy | `search-brewer/.../PopularityServicePipe.java` |
| GitOps service | `gitops/unbxd/team-platform/service/popularity-service/` |
| Enablement | `documentation/enablement.md` |
