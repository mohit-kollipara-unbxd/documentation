# Top Sellers — store-level support (TSS)

Short summary of store top sellers work on branch `top_sellers_stores_v2`.  
Transform uses `RecommenderBoxType.TOP_SELLERS_STORE` (abbreviation `TSS`); deploy uses `persist_store_topsellers` (not `push()`).

## Data contract

| Item | Value |
|------|--------|
| Store id source | Order clickstream `misc.store_id` |
| Transform output | One JSON file per site: `[{"storeId": "...", "uniqueIds": ["pid", ...]}, ...]` |
| S3 path | `{output}/store-top-sellers/site/{site_key}.json` |
| Aerospike | Namespace/set same as site TS; **key** = site key only; **bin** = `TSS` |
| Coexistence | Same record as site `TS` bin; different bin name |

## Pipeline flags (cerebro)

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `is_store_topsellers_enabled` | `false` | Run TSS transform + deploy |
| `store_topsellers_top_stores` | `100` | Max stores in JSON |
| `store_topsellers_products_per_store` | `100` | Max products per store |

---

## cerebro-base

**Branch:** `top_sellers_stores_v2` (from v2.0.11)

- **`libs/cerebro/recommendations/types.py`** — `RecommenderBoxType.TOP_SELLERS_STORE` (abbreviation **`TSS`**).
- **`libs/cerebro/topsellers.py`**
  - `persist_store_topsellers(site_key, store_list)` — writes bin `TSS` on the `topseller` set (site key, no recipe).
  - `TopSellers.push` logging — one summary line per push (key, box, pid count); no full PID dumps.
- **`libs/tests/test_top_sellers.py`** — TSS abbreviation, persist bin/value, empty-list no-op.

Deploy image pins this branch in `components/deploy/top_sellers/requirements.txt` until a release tag is cut.

---

## cerebro

**Transform** (`components/transform/top_sellers/`)

- Component YAML + CLI: store flags, limits, `store_topsellers_output_file_path`.
- Clickstream SQL adds `store_id` when store TSS is enabled.
- Feed×clickstream join includes `store_id`.
- Extra algo step `RecommenderBoxType.TOP_SELLERS_STORE`: aggregate `pdscore` by store/product, rank/cap, write JSON; KFP output `N/A` when disabled/empty.
- `### TSS:` logs for store counts and write summary.

**Pipeline** (`app/algos/top_sellers.py`)

- Wires store params into transform and `store_topsellers_paths` into deploy.

**Deploy** (`components/deploy/top_sellers/`)

- Reads JSON file path(s) from transform (not `part-*` layout).
- `ts.persist_store_topsellers(site_key, parsed_json)` after normal TS/BTS/CTS/TV pushes.
- `### TSS deploy` logging.

Site/brand/category/top-viewed top sellers and threshold **fallback** behavior are unchanged. TSS has no fallback.
