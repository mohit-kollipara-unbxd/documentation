Here’s a short end-to-end picture of **Top Sellers with variants** (`variants=True`).

## Pipeline flow

1. **Feed** (`app/algos/top_sellers.py`) — fetches feed with `variantId` when `variants=True`
2. **Transform** (`components/transform/top_sellers/`) — joins clickstream on `variant_id = feed.variantId`, rolls up to product level, attaches top variants
3. **Deploy** (`components/deploy/top_sellers/`) — parses S3 output and pushes to Aerospike via `cerebro.topsellers.TopSellers.push()`

---

## Transform: how variants are built

When `variants=True`:

| Step | What happens |
|------|----------------|
| **Join** | Clickstream `variant_id` ↔ feed `variantId` (not `product_ids` ↔ `uniqueId`) |
| **Score** | Each variant gets a `pdscore` (time-decayed quantity/orders/revenue) |
| **Roll up** | Variants grouped by product (`uniqueId` as `pid`), top variants sorted by score |
| **Enrich** | Up to **5 variants** per product: clickstream-sold variants first, then feed variants to fill gaps |
| **Encode** | `pid` becomes `uniqueId\|var1,var2,var3` |

```504:505:components/transform/top_sellers/src/component.py
            enriched_df = enriched_df.withColumn("pid", F.concat(F.col("pid"), F.lit("|"),
                                                                 F.concat_ws(",", F.col("final_variants"))))
```

---

## S3 output format (before deploy)

Spark writes tuples like:

```text
('mysite', 'uniqueID1|varA,varB,varC###45.2@@@uniqueID2|varX,varY###38.1')
('mysite.IND-DEL-##', 'uniqueID1|varA,varB###40.0@@@uniqueID3|varZ###35.5')
```

- Key: `site` or `site.location`
- Value: `pid###score` entries joined by `@@@`
- With variants, `pid` = `uniqueId|comma-separated-variantIds`

---

## Deploy: TS vs TSV in Aerospike

`TopSellers.push()` (in `cerebro-base`) does **two writes** when `variants=True`:

```64:88:/Users/netcore/Projects/cerebro-base/libs/cerebro/topsellers.py
        if variants:
            # Push uniqueIds and variantIDs
            variant_ids = [{"uniqueId": pid.split("|")[0], "variantIds": pid.split("|")[1].split(",")} for pid in pids]
            ...
                self.persist_to_aerospike(
                    key,
                    recipe_id,
                    box_type.abbreviation + "V",   # "TS" + "V" = "TSV"
                    variant_ids
                )
            ...
            pids = [pid.split("|")[0] for pid in pids]

        # Push just the uniqueIDs
        ...
            self.persist_to_aerospike(
                key,
                recipe_id,
                box_type.abbreviation,               # "TS"
                pids
            )
```

Both bins live on the **same Aerospike record** (same key).

---

## Sample Aerospike data

**Record key:** `mysite` (site-level) or `mysite.{recipe_id}` or `mysite.{loc_id}`

**Namespace / set:** `recommendations` / `topseller` (or `recipe_topseller` if recipe-scoped)

```json
{
  "TS": ["uniqueID1", "uniqueID2"],
  "TSV": [
    {"uniqueId": "uniqueID1", "variantIds": ["varA", "varB", "varC"]},
    {"uniqueId": "uniqueID2", "variantIds": ["varX", "varY"]}
  ]
}
```

That matches the unit test:

```102:108:/Users/netcore/Projects/cerebro-base/libs/tests/test_top_sellers.py
        variant_pids = ["VPID1|VID11,VID12", "VPID2|VID21"]
        self.topsellers.push("mysite", "31", variant_pids, RecommenderBoxType.TOP_SELLERS, platform='io', variants=True)
        expected_variant_ids = [{"uniqueId": "VPID1", "variantIds": ["VID11", "VID12"]},
                               {"uniqueId": "VPID2", "variantIds": ["VID21"]}]
        assert self.as_util.get(("recommendations", "recipe_topseller", "mysite.31")) == {"TSV": expected_variant_ids,
                                                                                          "TS": ["VPID1", "VPID2"]}
```

---

## Without variants (`variants=False`)

- Join: `product_ids = uniqueId`
- `pid` stays plain `uniqueID1`, `uniqueID2`
- Only **TS** bin is written — no TSV

```json
{
  "TS": ["uniqueID1", "uniqueID2"]
}
```

---

## Other algo bins (same pattern)

| Algo | uniqueId bin | variant bin |
|------|--------------|-------------|
| Top Sellers | `TS` | `TSV` |
| Category Top Sellers | `CTS` | `CTSV` |
| Brand Top Sellers | `BTS` | `BTSV` |
| Top Viewed | `TV` | `TVV` |

Abbreviation = first letter of each word in the enum name + `V` for variants.

---

## Variant selection logic (summary)

1. Sum scores per variant per product
2. Sort variants by score (desc)
3. Take top clickstream variants; if fewer than 5, backfill from feed `collect_list(variantId)` for that `uniqueId`
4. Cap at **5 variants** per product (`DEFAULT_MAX_VARIANTS = 5`)

So **TS** is the ranked product list; **TSV** is the per-product variant mapping the recommender uses when serving variant-level results.

Here's the sample Aerospike data for **Brand Top Sellers (BTS)** and **Category Top Sellers (CTS)** in the same format. The bin naming follows `box_type.abbreviation + "V"` for the variant bin.

## Brand Top Sellers (BTS / BTSV)

**Site-level** — key: `mysite.21`

```json
{
  "BTS": ["uniqueID1", "uniqueID2", "uniqueID3"],
  "BTSV": [
    {"uniqueId": "uniqueID1", "variantIds": ["varA", "varB", "varC"]},
    {"uniqueId": "uniqueID2", "variantIds": ["varX", "varY"]},
    {"uniqueId": "uniqueID3", "variantIds": ["varP"]}
  ]
}
```

**Location-level** — key: `mysite.21.IND-DEL-##`

```json
{
  "BTS": ["uniqueID1", "uniqueID5"],
  "BTSV": [
    {"uniqueId": "uniqueID1", "variantIds": ["varA", "varB"]},
    {"uniqueId": "uniqueID5", "variantIds": ["varZ"]}
  ]
}
```

**Brand-scoped** — key: `mysite.21.IND-DEL-##.NIKE`

```json
{
  "BTS": ["uniqueID1", "uniqueID7", "uniqueID9"],
  "BTSV": [
    {"uniqueId": "uniqueID1", "variantIds": ["varA", "varB", "varC"]},
    {"uniqueId": "uniqueID7", "variantIds": ["varM"]},
    {"uniqueId": "uniqueID9", "variantIds": ["varN", "varO"]}
  ]
}
```

---

## Category Top Sellers (CTS / CTSV)

**Site-level** — key: `mysite.21`

```json
{
  "CTS": ["uniqueID1", "uniqueID2", "uniqueID4"],
  "CTSV": [
    {"uniqueId": "uniqueID1", "variantIds": ["varA", "varB", "varC"]},
    {"uniqueId": "uniqueID2", "variantIds": ["varX", "varY"]},
    {"uniqueId": "uniqueID4", "variantIds": ["varR"]}
  ]
}
```

**Category-scoped** — key: `mysite.21.Footwear`

```json
{
  "CTS": ["uniqueID1", "uniqueID3", "uniqueID6"],
  "CTSV": [
    {"uniqueId": "uniqueID1", "variantIds": ["varA", "varB"]},
    {"uniqueId": "uniqueID3", "variantIds": ["varK"]},
    {"uniqueId": "uniqueID6", "variantIds": ["varL", "varM"]}
  ]
}
```

**Category + location-scoped** — key: `mysite.21.IND-DEL-##.Footwear`

```json
{
  "CTS": ["uniqueID1", "uniqueID8"],
  "CTSV": [
    {"uniqueId": "uniqueID1", "variantIds": ["varA", "varB", "varC"]},
    {"uniqueId": "uniqueID8", "variantIds": ["varQ"]}
  ]
}
```

---

## Key shape recap

The Aerospike record key is built by `TopSellers.create_record_key`:

```20:32:/Users/netcore/Projects/cerebro-base/libs/cerebro/topsellers.py
        key = site_key
        if recipe_id:
            key += "." + recipe_id
        if kwargs.get("loc_id"):
            key += "." + kwargs["loc_id"]
        # either one of category/brand
        if kwargs.get("category"):
            key += "." + kwargs["category"]
        elif kwargs.get("brand"):
            key += "." + kwargs["brand"]
        if kwargs.get("top_viewed"):
            key += ".tv"
        return key
```

So the key pattern is:

```
site_key[.recipe_id][.loc_id][.category|brand][.tv]
```

and the record always carries both the plain-uniqueId bin (`TS`/`BTS`/`CTS`/`TV`) and the variant bin (`TSV`/`BTSV`/`CTSV`/`TVV`) when `variants=True`.

---

## Without variants

For BTS / CTS when `variants=False`, only the plain bin is written (no `BTSV` / `CTSV`):

```json
{
  "BTS": ["uniqueID1", "uniqueID2", "uniqueID3"]
}
```

```json
{
  "CTS": ["uniqueID1", "uniqueID2", "uniqueID4"]
}
```


### ABOUT THE PD FORMULA

## PDSCORE formula

The base formula (registered as the Spark UDF `PDSCORE`) is a **time-decayed metric**:

```
PDSCORE = 60 * exp(-1 * |TO_DATE - date| / 15.0) * metric_value
```

Where:
- `60` — fixed amplitude constant
- `exp(-1 * |TO_DATE - date| / 15.0)` — exponential time decay (half-life ~10.4 days)
- `TO_DATE` — yesterday (`CURRENT_TIME.date() - 1 day`)
- `date` — the event date
- `15.0` — decay constant (days); older events contribute less
- `metric_value` — depends on `metric_type`

```199:211:components/transform/top_sellers/src/component.py
    if metric_type == 'quantity':
        def pdscore_udf(date_val, quantity_val, revenue_val):
            return 60 * math.exp(-1 * abs(TO_DATE - date_val).days / 15.0) * (quantity_val if quantity_val else 0)
    elif metric_type == 'orders':
        def pdscore_udf(date_val, quantity_val, revenue_val):
            # For orders: count each transaction as 1 regardless of quantity
            return 60 * math.exp(-1 * abs(TO_DATE - date_val).days / 15.0)
    elif metric_type == 'revenue':
        def pdscore_udf(date_val, quantity_val, revenue_val):
            return 60 * math.exp(-1 * abs(TO_DATE - date_val).days / 15.0) * (revenue_val if revenue_val else 0)
    else:
        # Default to quantity if invalid metric_type
        def pdscore_udf(date_val, quantity_val, revenue_val):
            return 60 * math.exp(-1 * abs(TO_DATE - date_val).days / 15.0) * (quantity_val if quantity_val else 0)
```

### Per `metric_type`

| `metric_type` | Formula |
|---------------|---------|
| `quantity` (default) | `60 * exp(-Δdays/15) * quantity` |
| `orders` | `60 * exp(-Δdays/15)` *(each order event = 1, ignores qty)* |
| `revenue` | `60 * exp(-Δdays/15) * revenue` |

where `Δdays = |TO_DATE - event_date|`

---

## Top Viewed variant — `PDSCORETV`

For top-viewed, there's no quantity/revenue, so it's just the time decay:

```
PDSCORETV = 60 * exp(-1 * |TO_DATE - date| / 15.0)
```

```239:243:components/transform/top_sellers/src/component.py
        sqlContext.udf.register(
            "PDSCORETV",
            lambda date_val: 60 * math.exp(-1 * abs(TO_DATE - date_val).days / 15.0),
            DoubleType()
        )
```

---

## Worked example (quantity)

Say `TO_DATE = 2026-07-06`, an order on `2026-06-26` (10 days ago) for qty = 2:

```
PDSCORE = 60 * exp(-10/15) * 2
        = 60 * exp(-0.667) * 2
        = 60 * 0.5134 * 2
        ≈ 61.6
```

An order yesterday (Δ=0): `60 * exp(0) * 2 = 120` — full weight.

---

## How it's used in the SQL

The UDF is called in the clickstream load query so each event row gets a `pdscore` column:

```70:80:components/transform/top_sellers/src/component.py
    query = f"""
    SELECT 
        ts, date, request_id, event_type, 
        array_join(
            transform(
                product_ids, 
                id -> IF(position('?' IN id) > 0, substring_index(id, '?', 1), id)
            ), 
            "|"
        ) AS product_ids,
        {"variant_id," if VARIANTS else ""}
        user_id, quantity, revenue,
        location_id, {pdscore} as pdscore
    FROM {tname}
    WHERE product_ids is not null
        AND (lower(event_type) = "{target_event}" OR lower(event_type) = "{target_event_external}")
        AND (terminated is null OR terminated = false)
        AND (source is null OR source = true)
    """
```

Then top-sellers aggregation is simply `SUM(pdscore)` grouped by `pid` (and `loc_id`/`brand`/`category` as needed), sorted desc — the score you see as `###45.2` in the S3 output is this summed PDSCORE.