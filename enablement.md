# Algorithm Enablement / Disablement Config Reference

This document lists, per algorithm, the exact query-param / config keys used to **enable** it and the exact param to pass on a request to **disable** it (e.g. `reranker.recommend.patchProducts=false` disables hybrid-search patching).

General precedence for most boolean flags (`r.GetBoolVal(key)`), see `internal/search/request/request.go`:

```
URL query param  >  experiment override params  >  config.EnableSvc[key] (from Albus "mimir" config)
```

So **any** of these params can always be force-disabled per-request by passing `key=false` in query params, regardless of what site config / experiment says — this is the standard way to disable an algorithm for a single request.

Site-level defaults come from Albus config service `mimir`, under a **handler** (roughly one per algorithm) with a boolean property named `enable`, deserialized in `Deserialize()` / `DesBrewerProp()` in `internal/configmw/configmw.go`.

---

## 1. NER (Named Entity Recognition)

**Query param:** `ner`
**Enable:** `ner=true`
**Disable:** `ner=false`

- Autosuggest also accepts an alias `enableNer=true/false`, which is translated internally to `ner=true/false` (`internal/configmw/configmw.go:98-107`).
- Dimension-NER is a related but separate flag — see below.

**Defaults per handler:**
| Handler | Default |
|---|---|
| search | driven by site config / albus `ner` handler `enable` |
| category | forced `ner=false` |
| autosuggest | forced `ner=false` unless `enableNer=true` passed |
| pdp | forced `ner=false` |
| image_search | forced `ner=false` |

**Site config (Albus `mimir` svc):** handler `ner`, property `enable` (bool) → `config.EnableSvc["ner"]`.

**Related tunables (Albus `ner` svc handler `default`, property `entity_field_mapping`, and `mimir` handler `ner`):**
- `ner_min_confidence` (float, default `0.5`)
- `reformulation_min_confidence` (float)
- `ner.synonyms.boost_mode` (string: `max` / `sum` / `product`)

### Dimension NER

**Query param:** `dimension`
**Enable:** `dimension=true`
**Disable:** `dimension=false`

- Autosuggest alias: `enableDimNer=true/false` → mapped to `dimension=true/false`.
- Forced `dimension=false` for category, autosuggest (unless override), pdp, image_search — same as NER above.
- Site config: handler `dimension`, property `enable`.
- Tunables: `default_dimension.dimension`, `default_unit.dimension`, `allowed_measurements.dimension`, `normalization_factor.dimension`, `query_mode.dimension`, `dimension.remainingQueryBoost`.

---

## 2. QCS (Query Category Statistics / Query Concept Search)

**Query param:** `qcs`
**Enable:** `qcs=true`
**Disable:** `qcs=false`

**Defaults per handler:**
| Handler | Default |
|---|---|
| search | driven by site config `qcs` handler `enable` |
| category | forced `qcs=false` |
| autosuggest | forced `qcs=false` |
| pdp | forced `qcs=false` |
| image_search | forced `qcs=false` |

**Site config:** Albus `mimir` handler `qcs`, property `enable`; Albus `qcs` svc handler `statistical`, property `entity_field_mapping` / `field_boost`.

**Related tunables:**
- `query_mode` (under `qcs`) → exposed as `query_mode.qcs`
- `category_level` → `qcs.category_level` (default `ALL`)
- `boost_min_confidence` → `qcs.boost_min_confidence`
- `filter_min_confidence` → `qcs.filter_min_confidence`
- `boost_aggregation` → `qcs.boost_aggregation` (`max` / `sum`)
- `use_dim_query_qcs` — bool, uses dimension-stripped query for QCS

---

## 3. Groot / Promotion

**Query param:** `promotion`
**Enable:** `promotion=true`
**Disable:** `promotion=false`

There's also a lower-level "remove manager" gate that fully strips the promotion engine call:

**Query param:** `req.rm.promotionEngine`
**Disable (harder kill):** `req.rm.promotionEngine=false` — set to `true` to allow promotion engine call to happen at all; combined with `promotion=false` to fully disable.

**Defaults per handler:**
| Handler | promotion | req.rm.promotionEngine |
|---|---|---|
| search / category | `true` | `true` |
| autosuggest | `false` | — |
| image_search | `true` | `true` |
| pdp | forced `false` | — |

**Site config:** Albus `mimir` handler for promotion (bool `promotion` in the generic brewer bool map); handler `promotion` `enable` also honored via generic default case in `Deserialize()`.

**Related tunables:**
- `promotion.version` (default `v1`, also `v3`/`v4`)
- `promotion.classifier.strategy`
- `promotion.enableNestedFacet`
- `promotion.ignoreGlobalRules`

---

## 4. Popularity (additive / multiplicative, segments, trending)

Popularity is **off by default** — it needs an explicit enable at two levels: the algorithm-level enable, plus a hard "master" gate.

**Query params:**
- `mimir.popularity.enable` — **hard master gate**. Must be `true` for the popularity node to run at all.
- `mimir.defaultPopularity.enable` — must be `true` for the base popularity boost to be applied in the query builder.
- `enablePopularity` — soft toggle inside the builder (defaults to `true` if the above two are on and this is absent).

**Enable (full):**
```
mimir.popularity.enable=true
mimir.defaultPopularity.enable=true
enablePopularity=true
```

**Disable:**
```
mimir.popularity.enable=false
```
(or `mimir.defaultPopularity.enable=false`, or `enablePopularity=false` for a softer disable that keeps the node running but skips the base boost)

**Additive vs Multiplicative:**
- `mimir.scores.multiplicative` — `true` = use multiplicative boost function (`boost`), `false`/absent = additive (`bf`).
- `mimir.scores.disableDefaultPopularity=true` forces default popularity off downstream regardless of the above.

**Popularity segments (custom per-segment boosts):**
- `popularity.segments.enable=true/false` — enable/disable segment-based custom popularity boosts.
- `popularity.segments.multiplicative=true/false` — additive vs multiplicative for segment boosts.
- `popularity.segments.name`, `popularity.segments` — JSON segment definitions (site config only).

**Trending products:**
- `enableTrendingProducts=true/false` (default `false`).

**Related tunables:**
- `popularity.version` (default `v1`)
- `popularity.expId`
- `mimir.popularity.multiplier` (float)
- `mimir.popularity.buckets` (site config, array of field mappings)

---

## 5. User Behaviour (UB)

Also requires a two-level gate similar to popularity.

**Query params:**
- `mimir.ub.enable` — **hard master gate**, must be `true`.
- `user.behaviour` — algorithm-level toggle.

**Enable:**
```
mimir.ub.enable=true
user.behaviour=true
```

**Disable:**
```
user.behaviour=false
```
(or `mimir.ub.enable=false` to hard-kill it regardless of `user.behaviour`)

**Related tunables:**
- `mimir.ub.multiplier` (float) — multiplier applied to UB score contribution.
- `user.behaviour.expId` — UB experiment id, changes the sitekey suffix used to fetch UB data.

---

## 6. Spellcheck (AI + Solr)

There are two independent spellcheck implementations that share the top-level `spellcheck` gate.

**Query param (master gate):** `spellcheck`
**Enable:** `spellcheck=true` (this is the default for search/category/autosuggest/pdp)
**Disable:** `spellcheck=false`

### Spellcheck AI
**Query param:** `spellcheck_ai`
**Enable:** `spellcheck_ai=true` (only takes effect for `search` / `autosuggest` rulesets)
**Disable:** `spellcheck_ai=false`

Related tunables: `spellcheck_ai.language`, `spellcheck_ai.max_distance` (float).

### Spellcheck Solr
There's no separate positive-enable flag for Solr spellcheck — it runs whenever `spellcheck=true` **and** it isn't explicitly disabled:

**Query param:** `spellcheck.solr.disabled`
**Disable Solr spellcheck (keep AI spellcheck):** `spellcheck.solr.disabled=true`
**Enable Solr spellcheck:** `spellcheck.solr.disabled=false` (default)

Note: Solr spellcheck is also auto-suppressed whenever `spellcheck_ai=true` (AI takes precedence).

Related tunables:
- `spellcheck.score.th` (float, default `0.6`) — score threshold for using NER-corrected spelling.
- `spellcheck.version`
- `spellcheck.dictionary`

---

## 7. Personalization (reranker recommend / rerank)

Personalization piggybacks on the reranker recommend/rerank nodes.

**Query params:**
- `personalization` — master personalization toggle.
- `personalization.version` — must be `v2` for the personalization path to be used in recommend/rerank gating (v1/absent = legacy path).
- `reranker.recommend` — must be `true` for the recommend node to run at all (default `true` for search/category/pdp/image_search).
- `reranker.rerank` — must be `true` for the rerank node's personalization path; note recommend-personalization requires `reranker.rerank=false`.

**Enable personalized recommendations:**
```
reranker.recommend=true
personalization=true
personalization.version=v2
```
(plus a resolvable uid/customerId on the request)

**Disable personalization (keep default recommend/rerank behavior):**
```
personalization=false
```

**Disable recommend/rerank entirely:**
```
reranker.recommend=false
reranker.rerank=false
```

**Related tunables:**
- `personalization.enableNetcoreID` / `personalization.enableCustomerID` — which ID is sent to the reranker service.
- `reranker.recommend.platform`, `reranker.recommend.algo`, `reranker.recommend.campaignId`, `reranker.recommend.filterId`, `reranker.recommend.count`
- `reranker.rerank.platform`, `reranker.rerank.campaignId`, `reranker.rerank.count`, `reranker.rerank.boost`, `reranker.rerank.ignoreSort`, `reranker.rerank.disablePinSlot`

---

## 8. Vector / Semantic Search (Hybrid Search)

Semantic search doesn't have a single top-level "on/off" query param — it only runs as a **fallback leg** (`_ss` suffix) once the SS fallback strategy is triggered from `fallback.method`. What you *can* toggle directly are the hybrid-search patching behaviors layered on top of the recommend results.

**To disable hybrid-search patching (as noted in the ask):**
```
reranker.recommend.patchProducts=false
```

**Enable hybrid-search patching:**
```
reranker.recommend.patchProducts=true
```

**To disable the SS fallback leg itself (prevents semantic search from ever triggering):**
```
fallback.ss.maxResultsForTrigger=0
```
or remove `ss` from `fallback.method` (see Fallback section below).

**Related tunables:**
- `reranker.recommend.patchProducts.count` — number of products to patch in.
- `reranker.recommend.patchProducts.minTokenCount` — min token count in remaining query before patching kicks in.
- `fallback.ss.hybridSearch=true/false` — controls SS/docstore fallback pagination behavior in the response reranker.
- `fallback.ss.minTokenCount`, `fallback.ss.includeOriginalResults`
- `reranker.recommend.platform` — semantic_search platform value maps internally to `hybrid_search` metadata.

**Forced off:** `image_search` and `pdp` handlers force `semantic_search=false` by default.

---

## 9. EMF (Exact Matched Fields)

**Query param:** `emf.enable`
**Enable:** `emf.enable=true` (this is also the default when the param is absent, as long as `emf` field config exists)
**Disable:** `emf.enable=false`

Additional requirements for EMF to actually apply: ruleset must be `search`, and query must not be a wildcard (`*`).

**Related tunables:**
- `emf` (string, site config) — the exact-match field name/expression.
- `emf.mode` — `bq` (boost query, default) or `bf` (boost function).
- `emf.slop` (int, default `0`)
- `emf.score` (int, default `100`)

Note: once EMF is applied to a request, the builder sets `emf.enable=false` downstream (to avoid double-application on the fallback leg).

---

## 10. Fallback

Fallback has a top-level gate plus per-strategy gates/thresholds. Individual strategies are threshold-triggered rather than boolean-only, but each can effectively be disabled by zeroing/removing its trigger threshold or excluding it from `fallback.method`.

**Query param (master gate):** `fallback`
**Enable:** `fallback=true`
**Disable (kill all fallback behavior):** `fallback=false`

**Fallback strategy selection:**
`fallback.method` — comma-separated list of strategies to try, in order. Default: `spellcheck,qis`.
- To disable a specific strategy, remove it from the list, e.g. `fallback.method=qis` disables spellcheck fallback.
- To disable fallback response metadata block: `fallback.response=false`.

**Per-strategy trigger thresholds (effectively disable a strategy by setting its trigger threshold so it never fires):**
| Strategy | Suffix | Enable trigger param | Disable (never trigger) |
|---|---|---|---|
| QIS (reformulation) | `_qis` | `fallback.qis.minResultsForTrigger` / `fallback.qis.maxResultsForTrigger` | set `maxResultsForTrigger=-1` / remove `qis` from `fallback.method` |
| Spellcheck | `_spellcheck` | `fallback.spellcheck.maxResultsForTrigger`, requires `spellcheck=true` | `spellcheck=false` or `fallback.spellcheck.maxResultsForTrigger=0` |
| QF (alternate qf) | `_qf` | `fallback.qf.maxResultsForTrigger`, requires `alternate.qf` set | `fallback.qf.maxResultsForTrigger=0` |
| SS (semantic search) | `_ss` | `fallback.ss.maxResultsForTrigger`, `fallback.ss.minTokenCount` | `fallback.ss.maxResultsForTrigger=0` |

**Per-fallback-leg network enable/disable (site config / advanced):**
`fallback.<suffix>.enableNetwork` (e.g. `fallback.qis.enableNetwork`, `fallback.ss.enableNetwork`) — bool, gates whether the DAG network for that fallback leg is even wired up.

**Related tunables:**
- `fallback.maxAllowedFilters` (int, default `0`) — max filters allowed for a cached fallback response to still apply.
- `fallback.version` (string, default `v1`; also `v0` for brewer-only legacy)

---

## Quick Reference Table

| # | Algorithm | Enable param(s) | Disable param |
|---|---|---|---|
| 1 | NER | `ner=true` | `ner=false` |
| 1b | Dimension NER | `dimension=true` | `dimension=false` |
| 2 | QCS | `qcs=true` | `qcs=false` |
| 3 | Groot/Promotion | `promotion=true` | `promotion=false` |
| 4 | Popularity | `mimir.popularity.enable=true` + `mimir.defaultPopularity.enable=true` | `mimir.popularity.enable=false` |
| 4b | Popularity multiplicative | `mimir.scores.multiplicative=true` | `mimir.scores.multiplicative=false` (additive) |
| 4c | Popularity segments | `popularity.segments.enable=true` | `popularity.segments.enable=false` |
| 5 | User Behaviour | `mimir.ub.enable=true` + `user.behaviour=true` | `user.behaviour=false` |
| 6 | Spellcheck (master) | `spellcheck=true` | `spellcheck=false` |
| 6b | Spellcheck AI | `spellcheck_ai=true` | `spellcheck_ai=false` |
| 6c | Spellcheck Solr | `spellcheck.solr.disabled=false` | `spellcheck.solr.disabled=true` |
| 7 | Personalization | `personalization=true` + `personalization.version=v2` | `personalization=false` |
| 8 | Vector/Hybrid search (patching) | `reranker.recommend.patchProducts=true` | `reranker.recommend.patchProducts=false` |
| 9 | EMF | `emf.enable=true` | `emf.enable=false` |
| 10 | Fallback (master) | `fallback=true` | `fallback=false` |

---

### Sources
- `internal/consts/const.go`
- `internal/configmw/configmw.go`
- `internal/search/request/request.go`
- `internal/network/node/helper.go`, `internal/network/node/qb/*_builder.go`, `internal/network/node/rb/*.go`
- `internal/search/resolver/resp_resolver.go`
