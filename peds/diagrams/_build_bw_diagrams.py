#!/usr/bin/env python3
"""Regenerate PEDS KT Excalidraw diagrams — black & white, high detail."""
import json
import random
import string
from pathlib import Path

OUT = Path(__file__).parent
STROKE = "#000000"
FILL = "#ffffff"
LANE_FILL = "transparent"
FONT = 14
FONT_SM = 12
FONT_LG = 18


def rid():
    return "".join(random.choices(string.ascii_letters + string.digits, k=12))


class Doc:
    def __init__(self):
        self.elements = []
        self._seed = 1000

    def _next_seed(self):
        self._seed += 1
        return self._seed

    def text_block(self, x, y, w, text, font_size=FONT_SM, align="left"):
        tid = rid()
        lines = text.split("\n")
        h = max(20, int(font_size * 1.35 * len(lines)) + 4)
        el = {
            "id": tid,
            "type": "text",
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "angle": 0,
            "strokeColor": STROKE,
            "backgroundColor": "transparent",
            "fillStyle": "solid",
            "strokeWidth": 1,
            "roughness": 0,
            "opacity": 100,
            "groupIds": [],
            "frameId": None,
            "roundness": None,
            "seed": self._next_seed(),
            "version": 1,
            "versionNonce": self._next_seed(),
            "isDeleted": False,
            "boundElements": None,
            "updated": 1,
            "link": None,
            "locked": False,
            "text": text,
            "fontSize": font_size,
            "fontFamily": 3,
            "textAlign": align,
            "verticalAlign": "top",
            "containerId": None,
            "originalText": text,
            "lineHeight": 1.25,
        }
        self.elements.append(el)
        return h

    def box(self, x, y, w, h, label, stroke_width=2, roughness=0, dashed=False):
        rid_ = rid()
        tid = rid()
        el = {
            "id": rid_,
            "type": "rectangle",
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "angle": 0,
            "strokeColor": STROKE,
            "backgroundColor": FILL,
            "fillStyle": "solid",
            "strokeWidth": stroke_width,
            "roughness": roughness,
            "opacity": 100,
            "groupIds": [],
            "frameId": None,
            "roundness": {"type": 3},
            "seed": self._next_seed(),
            "version": 1,
            "versionNonce": self._next_seed(),
            "isDeleted": False,
            "boundElements": [{"type": "text", "id": tid}],
            "updated": 1,
            "link": None,
            "locked": False,
        }
        if dashed:
            el["strokeStyle"] = "dashed"
        lines = label.split("\n")
        fs = FONT_SM if len(lines) > 4 else FONT
        th = max(20, int(fs * 1.35 * len(lines)))
        ty = y + max(4, (h - th) / 2)
        txt = {
            "id": tid,
            "type": "text",
            "x": x + 6,
            "y": ty,
            "width": w - 12,
            "height": th,
            "angle": 0,
            "strokeColor": STROKE,
            "backgroundColor": "transparent",
            "fillStyle": "solid",
            "strokeWidth": 1,
            "roughness": 0,
            "opacity": 100,
            "groupIds": [],
            "frameId": None,
            "roundness": None,
            "seed": self._next_seed(),
            "version": 1,
            "versionNonce": self._next_seed(),
            "isDeleted": False,
            "boundElements": None,
            "updated": 1,
            "link": None,
            "locked": False,
            "text": label,
            "fontSize": fs,
            "fontFamily": 3,
            "textAlign": "center",
            "verticalAlign": "middle",
            "containerId": rid_,
            "originalText": label,
            "lineHeight": 1.25,
        }
        self.elements.extend([el, txt])
        return rid_

    def lane(self, x, y, w, h, title):
        rid_ = rid()
        self.elements.append(
            {
                "id": rid_,
                "type": "rectangle",
                "x": x,
                "y": y,
                "width": w,
                "height": h,
                "angle": 0,
                "strokeColor": STROKE,
                "backgroundColor": LANE_FILL,
                "fillStyle": "solid",
                "strokeWidth": 1,
                "roughness": 0,
                "opacity": 100,
                "groupIds": [],
                "frameId": None,
                "roundness": {"type": 3},
                "seed": self._next_seed(),
                "version": 1,
                "versionNonce": self._next_seed(),
                "isDeleted": False,
                "boundElements": None,
                "updated": 1,
                "link": None,
                "locked": False,
                "strokeStyle": "solid",
            }
        )
        self.text_block(x + 8, y + 6, w - 16, title, FONT_LG, "left")

    def arrow(self, x1, y1, x2, y2, label=None):
        dx, dy = x2 - x1, y2 - y1
        self.elements.append(
            {
                "id": rid(),
                "type": "arrow",
                "x": x1,
                "y": y1,
                "width": dx,
                "height": dy,
                "angle": 0,
                "strokeColor": STROKE,
                "backgroundColor": "transparent",
                "fillStyle": "solid",
                "strokeWidth": 1,
                "roughness": 0,
                "opacity": 100,
                "groupIds": [],
                "frameId": None,
                "roundness": {"type": 2},
                "seed": self._next_seed(),
                "version": 1,
                "versionNonce": self._next_seed(),
                "isDeleted": False,
                "boundElements": None,
                "updated": 1,
                "link": None,
                "locked": False,
                "points": [[0, 0], [dx, dy]],
                "lastCommittedPoint": None,
                "startBinding": None,
                "endBinding": None,
                "startArrowhead": None,
                "endArrowhead": "arrow",
            }
        )
        if label:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            self.text_block(mx - 80, my - 28, 160, label, 11, "center")

    def save(self, name):
        path = OUT / name
        doc = {
            "type": "excalidraw",
            "version": 2,
            "source": "https://excalidraw.com",
            "elements": self.elements,
            "appState": {
                "viewBackgroundColor": "#ffffff",
                "gridSize": 20,
                "scrollX": 0,
                "scrollY": 0,
            },
            "files": {},
        }
        path.write_text(json.dumps(doc, indent=2))
        print(f"{name}: {len(self.elements)} elements -> {path}")


def build_vector():
    d = Doc()
    W = 1520
    d.text_block(40, 10, W - 80, "VECTOR SEARCH & HYBRID SEARCH — END-TO-END ARCHITECTURE (KT)", FONT_LG, "center")

    # Lane 1: Edge
    y = 50
    d.lane(40, y, W, 130, "① EDGE / INGRESS")
    d.box(60, y + 40, 140, 70, "Shopper / API\nclient")
    d.box(230, y + 40, 180, 70, "Wingman\nTLS reverse proxy\n→ brewer.downstream")
    d.box(440, y + 40, 200, 70, "Mimir (search core)\nConfigMW ← Albus")
    d.box(680, y + 40, 180, 70, "Solr (overpass)\nkeyword recall")
    d.arrow(200, y + 75, 230, y + 75, "HTTPS")
    d.arrow(410, y + 75, 440, y + 75)
    d.arrow(640, y + 75, 680, y + 75, "patched BQ/BF")

  # Lane 2: Mode A Hybrid
    y = 190
    d.lane(40, y, W, 280, "② MODE A — HYBRID SEARCH (PRIMARY NETWORK, BEFORE SOLR)")
    d.text_block(
        60,
        y + 38,
        W - 120,
        "Runs on default qiss graph when reranker recommend is ON and SkipRecReranker is false.\n"
        "Purpose: inject semantic product IDs into keyword query (metadata tag: hybrid_search). Default patch count ≈ 10 products.",
        FONT_SM,
    )
    flow_y = y + 100
    nodes = [
        (60, "PreProcessor"),
        (200, "QueryModifier"),
        (360, "rec_reranker\nnode"),
        (520, "HTTP POST\nReranker"),
        (680, "BuildRecReranker\n(patch Solr)"),
        (860, "SEARCH"),
        (1040, "PostProcessor"),
    ]
    bw = 120
    for i, (x, lab) in enumerate(nodes):
        d.box(x, flow_y, bw, 80, lab)
        if i < len(nodes) - 1:
            d.arrow(x + bw, flow_y + 40, nodes[i + 1][0], flow_y + 40)
    d.text_block(
        60,
        flow_y + 100,
        700,
        "Gates & params:\n"
        "• reranker.recommend=true\n"
        "• reranker.recommend.platform=semantic_search\n"
        "• reranker.recommend.patchProducts=true\n"
        "• reranker.recommend.patchProducts.count (default 10)\n"
        "• reranker.recommend.patchProducts.minTokenCount\n"
        "• SkipRecReranker helper (mimir/network/node/helper.go)",
        FONT_SM,
    )
    d.text_block(
        800,
        flow_y + 100,
        680,
        "Reranker request (shared API):\n"
        "POST {MIMIR_RERANKER_URL}/v2.0/sites/{sitekey}/recommend\n"
        "?version=…&platform=semantic_search&campaignId=…\n"
        "count default 100 (DEFAULT_RECOMMENDATION_COUNT)\n"
        "Client: mimir/internal/downstream/reranker/encode_decode.go",
        FONT_SM,
    )

    # Lane 3: Mode B SS fallback
    y = 490
    d.lane(40, y, W, 300, "③ MODE B — SEMANTIC SEARCH FALLBACK (NETWORK SUFFIX _ss)")
    d.text_block(
        60,
        y + 38,
        W - 120,
        "Triggered when fallback=true, ss in fallback.method, and ssThres passes "
        "(fallback.ss.maxResultsForTrigger vs keyword hit count; fallback.ss.minTokenCount).",
        FONT_SM,
    )
    steps = [
        (60, "Primary search\nreturns low recall"),
        (240, "ApplySSFb\n(resp_resolver)"),
        (420, "Fallback chain\nsearcher_with_fb.go\nsuffix _ss"),
        (640, "PostProcessor:\nSemanticSearchN\n(lastAppliedFb==_ss)"),
        (880, "Reranker recommend\n(platform=semantic_search)"),
        (1120, "Docstore hydrate\n+ res_rerank"),
    ]
    sy = y + 100
    for i, (x, lab) in enumerate(steps):
        d.box(x, sy, 150, 90, lab)
        if i < len(steps) - 1:
            d.arrow(x + 150, sy + 45, steps[i + 1][0], sy + 45)
    d.text_block(
        60,
        sy + 110,
        720,
        "Optional SS behaviors:\n"
        "• fallback.ss.hybridSearch=true → re-query Solr with OR-boosted SS product IDs\n"
        "• fallback.ss.includeOriginalResults → merge keyword + vector ordering\n"
        "• fallback.ss.enableNetwork — wire fallback DAG leg\n"
        "Disable SS: remove ss from fallback.method OR fallback.ss.maxResultsForTrigger=0",
        FONT_SM,
    )
    d.text_block(
        820,
        sy + 110,
        660,
        "Important: Mimir does NOT call Aragorn directly.\n"
        "Both Mode A and Mode B always go: Mimir → Reranker → (Aragorn | ISVC).",
        FONT_SM,
    )

    # Lane 4: Reranker internals
    y = 810
    d.lane(40, y, W, 320, "④ RERANKER SERVICE (GORERANKER v2 + PYRERANKER + ENVOY)")
    d.box(60, y + 50, 150, 90, "Envoy sidecar\nroutes by path /\nplatform / campaign")
    d.box(240, y + 50, 160, 90, "goreranker\n(Go, branch v2)\nrecommend.Service")
    d.box(430, y + 50, 160, 90, "pyreranker\n(Python, master)\ndefault some SS\nroutes")
    d.arrow(210, y + 95, 240, y + 95, "goRoutesSemantic")
    d.arrow(210, y + 115, 430, y + 115, "catch-all semantic")
    d.text_block(
        620,
        y + 50,
        880,
        "Deployment load quirk (Go):\n"
        "For platform=semantic_search, config handler is aragorn#recommend\n"
        "(NOT semantic_search#recommend) — reranker/internal/recommend/service.go\n\n"
        "Decision (semantic_search.go):\n"
        "IF model_metadata AragornEnabled (enabled=true)\n"
        "  → aragorn.VectorSearch\n"
        "  → POST {ARAGORN_HOST}/v1/{store}/collections/{id}/search\n"
        "ELSE\n"
        "  → optional query embedding\n"
        "  → aipipeline.GetInference → KServe ISVC predict\n"
        "  → http://{algo}-{inferenceId}-{suffix}.{ns}/v1/models/{deployment}/versions/{ver}:predict",
        FONT_SM,
    )
    d.box(60, y + 170, 220, 110, "Aragorn\nvector / keyword /\nhybrid + RRF\n(default merge cap 100)")
    d.box(310, y + 170, 240, 110, "KServe ISVC\nHNSW or Scann serving\nnamespace e.g. search\nprovisioned by Cerebro")
    d.text_block(
        580,
        y + 170,
        920,
        "Online filters:\n"
        "• search_score_threshold in deployment metadata\n"
        "• Query suggestions may return skip_semantic_search\n"
        "• Token / QS thresholds (SEMANTIC_SEARCH_* in reranker)\n"
        "• Blacklisted queries skip semantic path",
        FONT_SM,
    )
    d.arrow(280, y + 225, 310, y + 225, "else")
    d.arrow(170, y + 160, 170, y + 170, "if Aragorn")

    # Lane 5: Offline
    y = 1150
    d.lane(40, y, W, 340, "⑤ OFFLINE — CEREBRO KUBEFLOW PIPELINE ranking (platform=semantic_search)")
    train = [
        (60, "Inputs:\nclickstream S3\nfeed / QPS / QCP\nsite_key, deployment_id\nmodel, field_boosts\nvector_search_index_type"),
        (280, "train_semantic_search\ncomponent\n→ model artifacts"),
        (500, "S3\nmodel_output_s3_path\nmetrics sidecars"),
        (700, "Branch on aragorn flag"),
    ]
    ty = y + 55
    for i, (x, lab) in enumerate(train):
        d.box(x, ty, 180, 110, lab)
        if i < len(train) - 1:
            d.arrow(x + 180, ty + 55, train[i + 1][0], ty + 55)
    d.box(60, ty + 140, 200, 100, "aragorn=false\ndeploy/inference_service\nKServe custom ISVC\nkfserving_helper.py")
    d.box(290, ty + 140, 200, 100, "aragorn=true\ndeploy/aragorn\ncollections +\nembeddings +\nfeed upsert")
    d.box(520, ty + 140, 200, 100, "deploy/configure\n→ Albus configstore\nreranker deployments\nsibling sites metadata")
    d.box(760, ty + 140, 200, 100, "Reranker + GitOps\nread deployment\nat runtime\nAIPIPELINE_*, ARAGORN_HOST")
    d.arrow(790, ty + 110, 790, ty + 140)
    d.text_block(
        1000,
        ty + 40,
        480,
        "Cron / GitOps:\n"
        "• cerebro.yaml cluster workflows\n"
        "• reranker.yaml env per region\n"
        "• Flux team-ai services\n\n"
        "Spin new ISVC:\n"
        "Name hash from site+model+deployment_id\n"
        "STORAGE_URI from train S3\n"
        "Namespace PIPELINES_IFSVC_NAMESPACE",
        FONT_SM,
    )

    # Lane 6: Ops
    y = 1510
    d.lane(40, y, W, 200, "⑥ OPS — COMMON FAILURES (FOR TRIAGE)")
    d.text_block(
        60,
        y + 40,
        720,
        "• Hybrid never patches → patchProducts false / SkipRecReranker / minTokenCount\n"
        "• SS never fires → fallback.method missing ss / maxResultsForTrigger / minTokenCount\n"
        "• Empty semantic → thresholds, QS skip, short query\n"
        "• Deployment not found → check aragorn#recommend in Albus\n"
        "• ISVC errors → inference id, namespace, auth cookie, stale version\n"
        "• Wrong reranker binary → Envoy configmap goRoutesSemantic vs pyreranker",
        FONT_SM,
    )
    d.text_block(
        800,
        y + 40,
        680,
        "Defaults cheat sheet:\n"
        "Reranker fetch count = 100\n"
        "Hybrid Solr patch = 10 products\n"
        "Cerebro max_predictions = 100\n"
        "Aragorn RRF default limit = 100\n\n"
        "Handlers: image_search, pdp force semantic off (enablement.md §8)",
        FONT_SM,
    )
    y = 1730
    d.lane(40, y, W, 150, "⑦ CODE / CONFIG INDEX (OPEN IN IDE)")
    d.text_block(
        60,
        y + 38,
        700,
        "mimir: searcher_with_fb.go, rec_reranker_builder.go, semantic_search.go, encode_decode.go\n"
        "reranker: recommend/service.go, semantic/semantic_search.go, aipipeline/inference_svc.go, aragorn_svc.go\n"
        "cerebro: app/algos/ranking.py, train/semantic_search, deploy/inference_service, deploy/aragorn, deploy/configure",
        FONT_SM,
    )
    d.text_block(
        780,
        y + 38,
        700,
        "gitops: clusters/ai*/env/team-ai/service/{mimir,reranker,cerebro,aragorn}.yaml\n"
        "enablement: documentation/enablement.md §8\n"
        "Regenerate this file: python3 documentation/peds/diagrams/_build_bw_diagrams.py",
        FONT_SM,
    )
    d.save("vector_search.excalidraw")


def build_ub():
    d = Doc()
    W = 1520
    d.text_block(40, 10, W - 80, "USER BEHAVIOUR (UB) — BATCH + ONLINE ARCHITECTURE (KT)", FONT_LG, "center")

    y = 50
    d.lane(40, y, W, 120, "① WHAT IT IS / WHAT IT IS NOT")
    d.text_block(
        60,
        y + 35,
        W - 100,
        "User Behaviour = aggregated query×product engagement scores from clickstream (CLICK, CART, ORDER).\n"
        "Batch Spark aggregation → Redis → Mimir lookup at search time.\n"
        "NOT an online reranker ML model. NOT the same as Olympus personalization / IQ Recs (different data path).",
        FONT_SM,
    )

    y = 185
    d.lane(40, y, W, 380, "② OFFLINE — CEREBRO user-behaviour PIPELINE")
    pipe = [
        (60, "Gimli / Albus\nsite list +\ndeployments"),
        (220, "Cerebro CronJob\nrunner\nALGORITHM=\nuser-behaviour"),
        (400, "Kubeflow\nuser-behaviour\n(ub.py)"),
        (580, "transform/\nquery_product_score\nSpark"),
        (780, "optional\nproduct_diversity"),
        (980, "deploy/\nuser_behavior"),
    ]
    py = y + 50
    for i, (x, lab) in enumerate(pipe):
        d.box(x, py, 140, 100, lab)
        if i < len(pipe) - 1:
            d.arrow(x + 140, py + 50, pipe[i + 1][0], py + 50)
    d.box(60, py + 130, 300, 200, "Clickstream input (S3):\n"
         "• Paths: CLICKSTREAM_SITE_BASE_PATH / regional bucket\n"
         "• Events: CLICK, CART, ORDER; SEARCH/BROWSE for weights\n"
         "• Decay: exp(-age/duration_days)\n"
         "• Default weights e.g. click:1 cart:10 order:30 (pipeline args)\n"
         "• Ruleset search → group by query\n"
         "• Ruleset browse → group by category/node")
    d.box(390, py + 130, 300, 200, "Scoring output shape:\n"
         "• Top products per query with sum_score\n"
         "• ub_map: productRanking [{uniqueId, score}]\n"
         "• Optional stats array\n"
         "• Failure: no clickstream files → exit -1")
    d.box(720, py + 130, 340, 200, "Redis persist (cerebro-base UserBehavior):\n"
         "• Boost: HSET {siteKey}.{ruleset} field=query value=JSON map pid→score\n"
         "• Stats: ub.stats.{siteKey}.{ruleset}\n"
         "• browse ruleset stored as category in Redis\n"
         "• Experiment: {siteKey}_{expId}.{ruleset}\n"
         "• Host: ub-ec.*.infra:6379 DB 0 (UB_REDIS)")
    d.text_block(
        1100,
        py + 130,
        380,
        "Schedule example (gitops cerebro):\n"
        "30 6 * * 0,2,4,6\n(+ site workflows user-behaviour-daily)\n\n"
        "External UB:\n"
        "source=external + external_s3_path\n"
        "skips QPS step",
        FONT_SM,
    )

    y = 580
    d.lane(40, y, W, 280, "③ ONLINE — MIMIR SEARCH GRAPH")
    on = [
        (60, "Request +\nConfigMW"),
        (200, "Analysis\n(Asterix)\nanalyzed query"),
        (380, "UB node\nmimir.ub.enable\nuser.behaviour"),
        (560, "Redis HGET\n{site}.{ruleset}\nfield=analyzed query"),
        (760, "product_scores\nBuildProductScores"),
        (980, "Merge Popularity\n(score_merger)"),
        (1180, "Solr\nfieldcompare\nub.map boosts"),
    ]
    oy = y + 55
    for i, (x, lab) in enumerate(on):
        d.box(x, oy, 150, 95, lab)
        if i < len(on) - 1:
            d.arrow(x + 150, oy + 47, on[i + 1][0], oy + 47)
    d.text_block(
        60,
        oy + 115,
        720,
        "Enablement (two-level gate):\n"
        "• mimir.ub.enable=true (schema default false)\n"
        "• user.behaviour=true\n"
        "• Disable: user.behaviour=false OR mimir.ub.enable=false\n"
        "• mimir.ub.multiplier, user.behaviour.expId\n"
        "• mimir.scores.multiplicative → merge mode with Popularity",
        FONT_SM,
    )
    d.text_block(
        820,
        oy + 115,
        660,
        "Critical ops note:\n"
        "UB lookup key uses ANALYZED query string.\n"
        "If Analysis normalizes differently than training tokenization,\n"
        "Redis miss → empty boost with no hard error.",
        FONT_SM,
    )

    y = 875
    d.lane(40, y, W, 200, "④ MONITORING & FERNIR")
    d.text_block(
        60,
        y + 40,
        700,
        "Morpheus algo score monitor:\n"
        "• stale_pipeline / stale_redis_scores\n"
        "• Celery ~ 09:30 UTC Sun/Tue/Thu/Sat (3h after batch)\n"
        "• Fernir checker: Ranking - UB (user.behaviour must not be false)",
        FONT_SM,
    )
    d.text_block(
        780,
        y + 40,
        700,
        "IQ Recs interaction:\n"
        "boost_type=negative → forces user.behaviour=false\n"
        "See documentation/iqrecs.md",
        FONT_SM,
    )

    y = 1090
    d.lane(40, y, W, 260, "⑤ LEGACY / RELATED (DASHED = NOT PRIMARY PATH)")
    d.box(60, y + 45, 400, 90, "Aggregator UBScoresGenerator\nRedis: {site}.ub.pids.{query}\nREST UserBehaviorServiceManager", dashed=True)
    d.box(490, y + 45, 400, 90, "search-brewer PopularityServicePipe\nRedisUBDataSource same key schema", dashed=True)
    d.box(920, y + 45, 400, 90, "Reranker Olympus personalization\nuses ub.map name in IQ docs only\nNOT UB Redis", dashed=True)
    d.text_block(
        60,
        y + 155,
        1400,
        "Triage checklist: (1) Fernir enabled (2) recent Cerebro run (3) HGET on ub redis (4) debug=true analyzed query vs stored query (5) experiment expId suffix",
        FONT_SM,
    )
    y = 1370
    d.lane(40, y, W, 120, "⑦ CODE INDEX")
    d.text_block(
        60,
        y + 38,
        1400,
        "cerebro/app/algos/ub.py • query_product_score/component_spark.py • cerebro-base/libs/cerebro/user_behavior.py • "
        "mimir/internal/network/node/ub.go • downstream/ub/redis.go • qb/product_scores/ • documentation/enablement.md §5",
        FONT_SM,
    )
    d.save("user_behaviour.excalidraw")


def build_popularity():
    d = Doc()
    W = 1520
    d.text_block(40, 10, W - 80, "POPULARITY — v2 / v1 / LEGACY + ONLINE ARCHITECTURE (KT)", FONT_LG, "center")

    y = 50
    d.lane(40, y, W, 130, "① PURPOSE")
    d.text_block(
        60,
        y + 35,
        W - 100,
        "Popularity boosts products using site-wide demand signals (clickstream [+ optional feed attributes]).\n"
        "Complements query-specific User Behaviour and semantic retrieve. Reranker has no popularity ranking path in repo.",
        FONT_SM,
    )

    y = 195
    d.lane(40, y, W, 420, "② MODERN — CEREBRO popularity PIPELINE (PRIMARY)")
    cer = [
        (60, "CronJob +\nKFP popularity.py"),
        (240, "source/feed\n(optional)"),
        (420, "transform/\nproduct_score\nSpark/Python"),
        (620, "S3 scored\nJSON artifact"),
        (800, "deploy/\npopularity"),
    ]
    cy = y + 45
    for i, (x, lab) in enumerate(cer):
        d.box(x, cy, 150, 85, lab)
        if i < len(cer) - 1:
            d.arrow(x + 150, cy + 42, cer[i + 1][0], cy + 42)
    d.box(60, cy + 110, 350, 220, "product_score logic:\n"
         "• Events CLICK/CART/ORDER weighted\n"
         "• Time decay over clickstream_duration_in_days\n"
         "• Optional feed blend (clickstream_feed_weight)\n"
         "• group_by_store → per-store maps\n"
         "• Normalize min-max; max_products default 5000\n"
         "• No clickstream → sys.exit(-1)")
    d.box(440, cy + 110, 320, 220, "deploy version=v2:\n"
         "• cerebro-base Popularity.persist_scores\n"
         "• Redis HASH pop.{siteKey} or pop.{siteKey}_{expId}\n"
         "• uniqueId → float score, TTL hmset_with_ttl\n"
         "• POPULARITY_REDIS_HOST (often ub-ec DB 1)")
    d.box(790, cy + 110, 320, 220, "Optional deploy/hodor:\n"
         "• Solr field unx_popularity_{expId}\n"
         "• Odin default: unx_popularity_default\n"
         "• Online boost via exists(field) in QB factory")
    d.text_block(
        1140,
        cy + 110,
        340,
        "POP_V2_ENABLED env\nmay default pipeline\nto version v2\n\n"
        "Autosuggest deploy:\nsite suffix _auto",
        FONT_SM,
    )

    y = 630
    d.lane(40, y, W, 200, "③ v1 popularity-service PATH (HTTP)")
    d.box(60, y + 45, 350, 120, "deploy version=v1:\nPOST /internal-services/popularity/clear/{site}\nPOST /internal-services/popularity/{site}\nBody: PopularityValues [{uniqueId, value}]")
    d.box(440, y + 45, 350, 120, "popularity-service :6004\n(gitops team-platform)\nSOURCE REPO NOT IN WORKSPACE")
    d.box(820, y + 45, 350, 120, "Solr boost fn:\npopularity(uniqueId)\nrefresh_scores=true → Solr RELOAD")
    d.arrow(410, y + 105, 440, y + 105)
    d.arrow(790, y + 105, 820, y + 105)
    d.text_block(
        1200,
        y + 55,
        280,
        "Morpheus may SKIP\nRedis freshness for v1\n(_popularity_expects_redis_scores)",
        FONT_SM,
    )

    y = 850
    d.lane(40, y, W, 180, "④ LEGACY AGGREGATOR (BATCH)")
    d.box(60, y + 45, 450, 110, "PopularityScoreGenerator: CB | SB | CLICKS\nBQ day stats + ES product stats\nTop 10k products → Redis {site}:{formula}:PS", dashed=True)
    d.box(540, y + 45, 450, 110, "FeedClient → PopularityScoreClient\nindex field popularity_score", dashed=True)

    y = 1050
    d.lane(40, y, W, 320, "⑤ ONLINE — MIMIR")
    mim = [
        (60, "PopularityNode\nmimir.popularity.enable\nHGETALL Redis"),
        (260, "BuildPopularity\nmimir.defaultPopularity.enable\nenablePopularity"),
        (480, "Boost templates\nv1 popularity()\nv2 unx_popularity_*\ntrend() if trending"),
        (700, "BuildProductScores\nmerge with UB"),
        (920, "Solr bf/boost\nfieldcompare"),
    ]
    my = y + 50
    for i, (x, lab) in enumerate(mim):
        d.box(x, my, 170, 100, lab)
        if i < len(mim) - 1:
            d.arrow(x + 170, my + 50, mim[i + 1][0], my + 50)
    d.text_block(
        60,
        my + 120,
        720,
        "Enablement (two gates):\n"
        "• mimir.popularity.enable=true\n"
        "• mimir.defaultPopularity.enable=true (often off by default)\n"
        "• enablePopularity, enableTrendingProducts\n"
        "• popularity.version v1|v2, popularity.expId\n"
        "• popularity.segments.* for header/param segments\n"
        "• mimir.popularity.buckets → Redis key suffixes",
        FONT_SM,
    )
    d.text_block(
        820,
        my + 120,
        660,
        "Dual mode at runtime:\n"
        "Indexed v2: Solr has unx_popularity_* → exists() boosts\n"
        "Redis-backed: PopularityNode + product_scores path\n\n"
        "POP_REDIS_DB default 1 (same host as UB, diff DB)",
        FONT_SM,
    )

    y = 1390
    d.lane(40, y, W, 170, "⑥ OPS / FAQ")
    d.text_block(
        60,
        y + 40,
        720,
        "• No boost → defaultPopularity.enable false\n"
        "• Empty Redis → wrong expId / bucket / store suffix (defaultBoostKeyGen)\n"
        "• v1 feed score 0 → HTTP popularity-service failure\n"
        "• trend() data source not verified in workspace",
        FONT_SM,
    )
    d.text_block(
        800,
        y + 40,
        680,
        "Merge with UB:\n"
        "mimir.scores.multiplicative\n"
        "mimir.scores.disableDefaultPopularity when using merged scores",
        FONT_SM,
    )
    y = 1580
    d.lane(40, y, W, 120, "⑦ CODE INDEX")
    d.text_block(
        60,
        y + 38,
        1400,
        "cerebro/app/algos/popularity.py • transform/product_score • deploy/popularity • cerebro-base/libs/cerebro/popularity.py • "
        "mimir popularity.go • qb/popularity_builder.go • aggregator BaseSiteScoreGenerator • enablement.md §4",
        FONT_SM,
    )
    d.save("popularity.excalidraw")


if __name__ == "__main__":
    build_vector()
    build_ub()
    build_popularity()
