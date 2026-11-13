# Vector Search & Hybrid Search — KT Guide

**Audience:** Engineers who triage search/reranker/Cerebro issues without deep prior context.  
**Architecture diagram (for presentation):** [diagrams/vector_search.excalidraw](./diagrams/vector_search.excalidraw) — black-and-white swimlanes; regenerate with `python3 documentation/peds/diagrams/_build_bw_diagrams.py`  
**Related enablement:** [Enablement reference §8 — Vector / Hybrid Search](../enablement.md#8-vector--semantic-search-hybrid-search)

---

## 1. Introduction

**Vector search (semantic search)** matches shopper queries to products using embedding similarity instead of (or in addition to) keyword recall in Solr.

In this stack it is **not** a single switch. Mimir uses semantic retrieval in two different ways:

| Mode | When it runs | Primary purpose |
|------|----------------|-----------------|
| **Hybrid search (patch)** | Primary search network, before Solr runs | Boost or inject top semantic product IDs into the keyword query |
| **SS fallback (`_ss`)** | After weak keyword recall, via `fallback.method` | Replace or augment results using reranker semantic recommend |

Both paths call **Reranker** `POST /v2.0/sites/{sitekey}/recommend` with `platform=semantic_search`. Reranker then calls either **Aragorn** (vector/hybrid collection API) or a **KServe inference service (ISVC)** on the AI pipeline, depending on site deployment metadata.

**Mimir does not call Aragorn directly** — the hop is always Mimir → Reranker → (Aragorn \| ISVC).

---

## 2. Problem statement

- Keyword search misses intent when queries are vague, synonym-rich, or don’t match catalog text.
- Zero or very low recall should not dead-end the shopper experience.
- Merchandising still needs Solr filters, facets, and business rules — pure vector-only pages are insufficient for most sites.

**What we optimize for**

- Better recall and relevance on hard queries without abandoning Solr’s filter/facet model.
- Controlled cost: bounded candidate counts (defaults below), optional QS gating, score thresholds.

---

## 3. How the algorithm solves it (conceptual)

1. **Encode the query** (and optionally use query suggestions / blacklists) inside Reranker.
2. **Retrieve** nearest-neighbor products from a trained index (ISVC HNSW/Scann) or Aragorn collection (vector / hybrid / RRF).
3. **Return** ranked product IDs + scores to Mimir.
4. **Mimir applies** them either by **patching** the Solr query (hybrid) or by **reordering/hydrating** the response on the `_ss` fallback leg.

**Defaults worth remembering**

| Constant | Value | Where |
|----------|-------|--------|
| Reranker recommend `count` | **100** | `mimir/internal/consts/const.go` — `DEFAULT_RECOMMENDATION_COUNT` |
| Hybrid patch product count | **10** | Same file — `DEFAULT_RECOMMENDER_PATCH_PRODUCTS_COUNT` |
| Pipeline `max_predictions` | **100** | `cerebro/app/algos/ranking.py` |

Hybrid patching uses fewer products than the reranker fetch window by design.

---

## 4. Online architecture

### 4.1 Edge and core search

```
Shopper → Wingman (TLS / reverse proxy) → Mimir (search graph) → Solr (overpass)
                                      ↘ Reranker (recommend API)
```

- Wingman proxies to regional Mimir/Brewer downstream (`wingman/conf/gen/env/wing.yaml`).
- Mimir reranker client: `{MIMIR_RERANKER_URL}/v2.0/sites/{sitekey}/recommend?...` (`mimir/internal/downstream/reranker/encode_decode.go`).

### 4.2 Mimir graph (primary search)

Typical path: `PreProcessor → QueryModifier → SEARCH → PostProcessor`.

- **Query modifier** can invoke **`rec_reranker`** before Solr when recommend is enabled (`mimir/internal/network/query_modifier.go`).
- **`BuildRecReranker`** patches the Solr query; platform `semantic_search` is recorded as metadata type **`hybrid_search`** (`rec_reranker_builder.go`, `consts.HYBRID_SEARCH`).

**Gating hybrid patch:** `SkipRecReranker` in `internal/network/node/helper.go` — patch requires `reranker.recommend.patchProducts=true` (among other conditions).

### 4.3 SS fallback network (`_ss`)

When `fallback=true` and thresholds in `internal/search/resolver/resp_resolver.go` (`ssThres`) pass:

- Fallback suffix **`_ss`** is wired in `searcher_with_fb.go` (`FbConfig[c.SS]`).
- Post-processor **`SemanticSearchN`** runs on the `_ss` leg (`internal/network/node/rb/semantic_search.go`).
- **Docstore** hydrates products; optional `fallback.ss.hybridSearch` re-queries Solr with OR-boosted IDs (`docstore.go`).

**Trigger knobs:** `fallback.ss.maxResultsForTrigger`, `fallback.ss.minTokenCount`, `fallback.method` must include `ss`. See [enablement.md](../enablement.md).

### 4.4 Reranker pod (Envoy + Go + Python)

One pod typically runs:

- **Envoy** — routes by path/query (`reranker/helm/reranker/templates/configmap.yaml`).
- **goreranker** (Go, `v2` branch) — primary for many semantic + Aragorn paths.
- **pyreranker** (Python, `master`) — default for some `platform=semantic_search` routes unless `goRoutesSemantic` sends traffic to Go.

GitOps example env: `gitops/clusters/ai01.gcp.prod.as1/env/team-ai/service/reranker.yaml` (`AIPIPELINE_*`, `ARAGORN_HOST`, `MIMIR_HOST`).

### 4.5 Aragorn vs ISVC (Reranker branch)

`recommend.Service.Recommend` (`reranker/internal/recommend/service.go`):

- For `platform=semantic_search`, **deployment config is loaded from handler `aragorn#recommend`**, not `semantic_search#recommend` (lines 76–83).
- **Go semantic service** (`internal/recommend/semantic/semantic_search.go`):
  - If `model_metadata` indicates **Aragorn enabled** → `aragorn.VectorSearch` → `POST .../collections/{id}/search`.
  - Else → embed query → `aipipeline.GetInference` → **KServe ISVC** URL pattern in `internal/downstreams/aipipeline/inference_svc.go`.

**ISVC naming (provisioned by Cerebro):** `{algo}-{hash}[-suffix].{namespace}` with predict path `/v1/models/{deploymentId}/versions/{version}:predict` (see `cerebro/components/deploy/inference_service/src/kfserving_helper.py`).

---

## 5. Offline: training, deploy, configuration

Orchestrator: **Cerebro** Kubeflow pipeline **`ranking`** (`cerebro/app/algos/ranking.py`), default `platform=semantic_search`.

### 5.1 Inputs (typical)

| Input | Role |
|-------|------|
| `site_key`, `deployment_id`, `sibling_sites` | Identity and multi-site models |
| Clickstream (S3) | Optional event boosts in algo configs |
| Feed / QPS / QCP paths | Catalog and query-product signals for ranking training |
| `model`, `vector_search_index_type` (`hnsw` / `scann`) | Embedding + index type |
| `field_boosts`, thresholds | Online filtering and boosting |
| `aragorn=true` | Switches deploy from ISVC path to Aragorn component |

**Train component:** `cerebro/components/train/semantic_search/` → model artifacts to **S3** (`model_output_s3_path`).

### 5.2 Outputs and where they land

| Path | `aragorn=false` | `aragorn=true` |
|------|-----------------|----------------|
| Model artifacts | S3 | S3 + Aragorn collections / embeddings |
| Serving | KServe ISVC in `PIPELINES_IFSVC_NAMESPACE` (e.g. `search`) | Aragorn vector store |
| Site config | Albus via `deploy/configure` — `reranker.semantic_search#recommend` + metadata | **`aragorn#recommend`** deployments, collection IDs (`cerebro/components/deploy/configure/src/component.py`) |

### 5.3 Spinning up new ISVC instances

1. Cerebro `deploy/inference_service` reads trained model S3 URI.
2. `deploy_custom_inference_service()` creates KServe InferenceService with deterministic name from site, model, deployment id.
3. Namespace from `PIPELINES_IFSVC_NAMESPACE` / cluster values in Cerebro helm.
4. `deploy/configure` pushes deployment record to **configstore (Albus)** so Reranker resolves inference id and version at request time.

**Reranker flags:** `aipipeline.infsvc.namespace`, `aipipeline.infsvc.suffix`, `aipipeline.cluster.local` (`reranker/cmd/reranker/flags/flag.go`).

### 5.4 Aragorn deploy path

`cerebro/components/deploy/aragorn/` — collections, embedding upsert, hybrid retrieval config. Reranker uses `ARAGORN_HOST` and collection metadata from configstore.

---

## 6. Enablement (quick reference)

Full detail: [enablement.md §8](../enablement.md#8-vector--semantic-search-hybrid-search).

| Goal | Config / param |
|------|----------------|
| Enable recommend node | `reranker.recommend=true` (often default on search) |
| Semantic platform | `reranker.recommend.platform=semantic_search` |
| Hybrid patch on primary search | `reranker.recommend.patchProducts=true` |
| Disable hybrid patch | `reranker.recommend.patchProducts=false` |
| SS fallback leg | `fallback=true`, include `ss` in `fallback.method`, tune `fallback.ss.*` |
| Prevent SS from firing | Remove `ss` from `fallback.method` or `fallback.ss.maxResultsForTrigger=0` |

**Handlers:** `image_search` and `pdp` force semantic off by default (enablement doc).

---

## 7. Dependencies

| Service | Role |
|---------|------|
| **Mimir** | Graph, fallback, Solr query build, docstore |
| **Reranker** | Recommend API, Aragorn/ISVC routing, embedding |
| **Cerebro** | Train/deploy/configure pipelines |
| **Aragorn** | Vector/hybrid retrieval (optional) |
| **KServe / AI pipeline** | ISVC hosting (optional) |
| **Albus / configstore** | Per-site deployments, thresholds, Aragorn flags |
| **Wingman** | Edge proxy to Mimir |
| **Solr / overpass** | Keyword execution and filters |

---

## 8. Operational FAQs & known failure modes

| Symptom | Likely cause | What to check |
|---------|----------------|---------------|
| No hybrid patch | `SkipRecReranker`, patchProducts false, token below `patchProducts.minTokenCount` | Mimir debug metadata `hybrid_search`; `helper.SkipRecReranker` |
| SS fallback never runs | `fallback.ss.maxResultsForTrigger` vs result count; min tokens | `resp_resolver.ssThres`; site `fallback.method` |
| Empty semantic results | Query too short; QS returns skip; score threshold | Reranker semantic service logs; deployment thresholds |
| Deployment not found | Wrong Albus handler | Semantic path reads **`aragorn#recommend`** in Go service |
| ISVC 4xx/5xx | Stale deployment id, wrong namespace, auth | Inference URL in `inference_svc.go`; cluster `PIPELINES_*` env on reranker |
| Aragorn empty / 4xx | Collection id mismatch, feed not synced | Cerebro configure output; Aragorn `vector_search_svc.go` |
| Traffic on wrong reranker binary | Envoy routing | `helm/reranker/templates/configmap.yaml`, `goRoutesSemantic` |
| “100 products” confusion | Reranker fetch vs Mimir patch count | Defaults 100 vs 10 (section 3) |
| Py vs Go behavior drift | Route sends to pyreranker | Compare branch `master` vs `v2`; verify deployed image tags (`origin/v2` may differ from local `v2`) |

---

## 9. Triage checklist

1. Confirm request handler (search vs category vs image_search).
2. `debug=true` on search — check metadata for `hybrid_search`, fallback suffix `_ss`, `user.behaviour` unrelated but often co-enabled.
3. Verify Albus: `reranker.aragorn#recommend` / `semantic_search#recommend` deployments for site + campaign.
4. Curl Reranker recommend directly with same query/platform/version.
5. For ISVC sites: Kubeflow run + ISVC pod health in `search` namespace.
6. For Aragorn sites: collection search API with same query embedding path.

---

## 10. Code index

| Topic | Path |
|-------|------|
| Fallback registration | `mimir/internal/network/searcher_with_fb.go` |
| Hybrid patch QB | `mimir/internal/network/node/qb/rec_reranker_builder.go` |
| SS semantic node | `mimir/internal/network/node/rb/semantic_search.go` |
| Reranker HTTP client | `mimir/internal/downstream/reranker/encode_decode.go` |
| Recommend entry | `reranker/internal/recommend/service.go` |
| Semantic strategy | `reranker/internal/recommend/semantic/semantic_search.go` |
| ISVC client | `reranker/internal/downstreams/aipipeline/inference_svc.go` |
| Aragorn client | `reranker/internal/downstreams/aragorn/aragorn_svc.go` |
| Cerebro ranking pipeline | `cerebro/app/algos/ranking.py` |
| Train / deploy ISVC | `cerebro/components/train/semantic_search/`, `cerebro/components/deploy/inference_service/` |
| Aragorn deploy | `cerebro/components/deploy/aragorn/` |
| Albus configure | `cerebro/components/deploy/configure/src/component.py` |

---

## 11. Glossary

| Term | Meaning |
|------|---------|
| **Hybrid search** | Keyword Solr search with semantic products patched in (metadata label `hybrid_search`) |
| **SS fallback** | Network suffix `_ss` semantic leg after low keyword recall |
| **ISVC** | KServe InferenceService for embedding + ANN predict |
| **Aragorn** | Vector store service with hybrid/RRF retrieval |
| **Deployment id** | Per-site model/deployment identifier in Albus + ISVC version path |
