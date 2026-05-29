# Glue Schema Evolution, DynamicFrames & Job Best Practices

This document explains how schema evolution is handled in the Glue ETL jobs of this
orchestrator, how AWS Glue `DynamicFrame`s are created and managed (and how they
relate to the `DataFrame`-based approach this repo actually uses), and the broader
set of scenarios and best practices to keep in mind when developing Glue jobs.

It complements [`data_pipeline_logic.md`](./data_pipeline_logic.md), which walks the
end-to-end pipeline. Here the focus is **schema handling** and **Glue engineering
practice**.

**Source files referenced throughout:**

| Job | File |
|-----|------|
| Simple (flat-table) ETL | `glue_jobs/without_data_model/glue_job.py` |
| Data-model (star schema) ETL | `glue_jobs/with_data_model/glue_job_with_data_model.py` |
| Prototypes / reference | `docs/scratch/*.py`, `glue_features/glue_features.md`, `scenario_based_questions/scenario_based_questions.md` |

---

## Table of Contents

1. [TL;DR — the most important thing to know](#1-tldr)
2. [Two paradigms: DataFrame vs DynamicFrame](#2-two-paradigms-dataframe-vs-dynamicframe)
3. [The approach used in this repository](#3-the-approach-used-in-this-repository)
4. [Schema evolution strategies (as implemented)](#4-schema-evolution-strategies-as-implemented)
5. [AWS Glue DynamicFrames — creation & management](#5-aws-glue-dynamicframes--creation--management)
6. [Schema evolution *with* DynamicFrames](#6-schema-evolution-with-dynamicframes)
7. [Building the entire ELT on DynamicFrames (module-by-module)](#7-building-the-entire-elt-on-dynamicframes-module-by-module)
8. [Other important scenarios & best practices](#8-other-important-scenarios--best-practices)
9. [Design considerations & trade-offs](#9-design-considerations--trade-offs)
10. [Quick reference](#10-quick-reference)

---

## 1. TL;DR

- **This repo does schema evolution with native Spark `DataFrame`s**, not Glue
  `DynamicFrame`s. The job reads the CSV with Spark, reads the **target Redshift
  table's schema** via the Redshift Data API, and **reconciles the two** —
  adding/altering Redshift columns and back-filling source columns as needed.
- The Redshift table is treated as the **schema authority**. Evolution is
  **additive and target-anchored**: new columns are added, missing columns are
  back-filled, and `VARCHAR`/integer columns are widened. Nothing is dropped or
  narrowed automatically.
- **`DynamicFrame`s are a Glue-specific abstraction** that defer type resolution
  (the "choice" type) and are excellent for *messy, self-describing, semi-structured*
  sources. They are **not used in the production jobs here** — but you should
  understand them because they are the idiomatic Glue answer to schema drift and
  appear in this repo's reference material.
- Sections 5–7 answer the "how are DynamicFrames created and managed" question in
  full and show how you would slot one into this pipeline.

---

## 2. Two paradigms: DataFrame vs DynamicFrame

AWS Glue gives you two in-memory abstractions. They interoperate freely
(`dyf.toDF()` / `DynamicFrame.fromDF(df, glueContext, "name")`), so most real jobs
mix them.

| Aspect | Spark `DataFrame` (used here) | Glue `DynamicFrame` |
|--------|-------------------------------|----------------------|
| Schema | **Fixed** — resolved at read time (`inferSchema` or explicit) | **Flexible / self-describing** — each record carries its own schema; ambiguity is preserved as a `choice` type |
| Type conflicts | Fail at read, or coerce to one type | Captured as a `choice`, resolved later with `resolveChoice` |
| Best for | Tabular, predictable, columnar (CSV/Parquet with stable schema) | Messy, nested, evolving, semi-structured (JSON, logs, "stringly typed" CSV) |
| Transforms | Full Spark SQL / functions API | Glue transforms (`ApplyMapping`, `ResolveChoice`, `Relationalize`, `Unbox`, …) + can drop to Spark via `toDF()` |
| Catalog integration | Manual | First-class (`from_catalog`, `enableUpdateCatalog`, bookmarks via `transformation_ctx`) |
| Error handling | Exceptions | Per-record **error capture** (`errorsAsDynamicFrame`, `stageThreshold`) |
| Performance | Generally faster (no per-record schema overhead) | Slight overhead from self-describing records; great for avoiding full re-reads on drift |

> **Why this repo chose `DataFrame`s:** the sources are well-defined business CSVs
> (AUM, revenue, expense, headcount…) flowing into **typed Redshift tables**. The
> target schema — not the file — is the contract. A `DataFrame` + explicit
> reconciliation against `information_schema` gives precise control over Redshift
> DDL (column order, `DECIMAL(38,18)` precision, `NOT NULL` keys, `VARCHAR`
> widths). A `DynamicFrame`'s strength (tolerating unknown shape) is less valuable
> when the destination is a strict relational warehouse.

---

## 3. The approach used in this repository

Both jobs follow the same schema lifecycle inside `main()`. The Redshift target is
the source of truth; the incoming CSV is conformed to it.

```
read_csv_file()                      # Spark reads CSV, normalises columns, casts types
        │
        ▼
check_table_exists()                 # information_schema.tables lookup
        │
   ┌────┴─────────────────────────┐
   │ table missing                │ table exists
   ▼                              ▼
create_new_redshift_table()    read_redshift_table_schema()   # SELECT ... WHERE 1=0 → ColumnMetadata
   │ (DDL from Spark schema)      │
   │                             if column sets differ:
   │                                fill_missing_columns()     # back-fill target cols absent in source
   │                                alter_redshift_table()     # ADD COLUMN for source cols absent in target
   └──────────────┬───────────────┘
                  ▼
alter_varchar_columns()              # widen VARCHAR(n) / promote SMALLINT→INT→BIGINT to fit data
                  ▼
df.select(<target column order>)     # align DataFrame to the (now reconciled) Redshift schema
                  ▼
coalesce(1).write.csv(staging)  →  COPY → staging table  →  MERGE (delete+insert) → target
```

### 3.1 Reading & normalising the source (`read_csv_file`)

`glue_jobs/without_data_model/glue_job.py:138`

```python
df = spark.read.option("header", "true").option("inferSchema", "true").csv(source_file)

# normalise column names: lowercase, non-alphanumerics → "_", collapse repeats
for old in df.columns:
    new = _clean_colname(old)
    if old != new:
        df = df.withColumnRenamed(old, new)
```

Two deliberate choices happen here:

- **`inferSchema=true`** lets Spark pick types from the data (convenient, but it
  reads the file twice — see §8.5).
- **`cast_like`** normalises numerics: any `DoubleType` that is **not** an upsert
  key is cast to `DECIMAL(38,18)`. This protects monetary/financial precision
  (floating point drift is unacceptable for AUM, fees, revenue) while leaving join
  keys untouched.
- Two audit columns are appended: `run_date` (timestamp) and `file_name`
  (provenance).

### 3.2 Discovering the target schema (`read_redshift_table_schema`)

`glue_jobs/without_data_model/glue_job.py:215`

The target schema is read **without scanning data** using a zero-row probe:

```python
sql = f"SELECT * FROM {schema_name}.{table_name} WHERE 1=0;"
...
metadata = result["ColumnMetadata"]   # Redshift Data API returns column types even for 0 rows
```

`map_dtype()` translates Redshift type names back into Spark types, producing an
**empty typed DataFrame** that acts as a portable schema descriptor. This is the
linchpin of the whole strategy: the comparison `set(df.columns) != set(redshift_df.columns)`
drives every evolution decision.

> Note: `read_redshift_table_schema` is called **again** after any DDL change
> (`glue_job.py:885`) so the column-ordering `select` at the end always reflects
> the *post-evolution* schema.

---

## 4. Schema evolution strategies (as implemented)

This is "Solution 2 — Explicit Schema Reconciliation" from
[`scenario_based_questions.md`](../../scenario_based_questions/scenario_based_questions.md)
(Scenario 5), realised across several functions.

### 4.1 New table — derive DDL from the source (`create_new_redshift_table`)

When the target does not exist, DDL is generated from the Spark schema via
`_spark_to_redshift_type()`:

| Spark type | Redshift type |
|------------|---------------|
| `StringType` | `VARCHAR(256)` |
| `IntegerType` | `INTEGER` |
| `LongType` | `BIGINT` |
| `DoubleType` | `DOUBLE PRECISION` |
| `DecimalType` | `DECIMAL(38,18)` |
| `BooleanType` | `BOOLEAN` |
| `DateType` / `TimestampType` | `DATE` / `TIMESTAMP` |
| `BinaryType` | `VARBYTE` |

Upsert-key columns get `NOT NULL`. A reporting view is created afterward
(`create_views`).

### 4.2 New columns in the source → `ALTER TABLE ADD COLUMN` (`alter_redshift_table`)

`glue_jobs/without_data_model/glue_job.py:331`

For any column present in the source but missing in the target, an
`ALTER TABLE … ADD COLUMN` is issued. Because Redshift views are bound to their
underlying table, the dependent view is **dropped first and recreated after**:

```python
drop_views(...)
for colf in source_df.schema.fields:
    if colf.name not in target_cols:
        execute_sql(f"ALTER TABLE … ADD COLUMN {colf.name} {rtype};", ...)
create_views(...)   # refresh
```

This is the additive, backward-compatible case — existing rows get `NULL` for the
new column.

### 4.3 Columns missing from the source → back-fill (`fill_missing_columns`)

`glue_jobs/without_data_model/glue_job.py:494`

If the target has columns the source lacks (e.g. an older file format), they are
materialised in the DataFrame with type-appropriate defaults (`get_default_value`):
empty string for text, `0` for numerics, `None` for dates/timestamps. This keeps
the CSV→COPY column count aligned with the table.

### 4.4 Widening to fit the data (`alter_varchar_columns`)

`glue_jobs/without_data_model/glue_job.py:373`

This handles **value-driven** evolution — the column exists with the right *name*
but the data no longer fits:

- **`VARCHAR` widening:** computes `max(length(col))` across the DataFrame in one
  aggregation; if it exceeds the current `character_maximum_length`, alters to
  `VARCHAR(min(maxlen + 10, 65535))`.
- **Integer promotion:** computes `max(abs(col))`; if it overflows the current
  integer width, promotes `SMALLINT → INTEGER → BIGINT`. Redshift cannot directly
  change an integer column's type, so it uses an **add-column → update → drop →
  rename** dance.

Both paths drop/recreate the view and log the altered columns.

> **Why one `.agg().collect()` is safe here:** it returns a *single* row of
> aggregates (max lengths/magnitudes), not the dataset — so it does not pull data
> to the driver. Contrast with `df.collect()` on a full DataFrame, which you should
> avoid (see §8.10).

### 4.5 Aligning column order before COPY

Redshift `COPY` from CSV is **positional**. After all DDL, the job re-reads the
target schema and reorders the DataFrame to match exactly:

```python
redshift_df = read_redshift_table_schema(config, redshift_conn, spark, client)
df = df.select(*[c.name for c in redshift_df.schema.fields])
```

Skipping this would silently load values into the wrong columns — a classic,
hard-to-spot schema-evolution bug.

### 4.6 The unsafe-type guard (`check_datatype_matching`)

Only in the simple job (`glue_job.py:698`, currently commented out at the call
site) is a guard that **refuses** to load when a non-numeric source column maps to
a numeric target column *and* has non-null values — i.e. it blocks a lossy/garbage
load rather than letting `COPY` insert nulls or fail opaquely. Worth enabling (or
porting to the data-model job) as a data-quality gate.

### 4.7 What is **not** handled automatically (by design)

| Change | Behaviour | Recommended handling |
|--------|-----------|----------------------|
| Column **renamed** in source | Treated as *new* column added + old column back-filled | Maintain a rename map in config; apply before reconciliation |
| Column **dropped** from source | Back-filled with default (kept in target) | Acceptable; explicit `DROP COLUMN` only via reviewed migration |
| Type **narrowing** (e.g. `BIGINT`→`INT`) | Never auto-applied | Requires a deliberate, reviewed migration |
| Incompatible type change (text↔number) | `check_datatype_matching` can reject it | Enable the guard; quarantine the file |
| Precision/scale change on `DECIMAL` | Standardised to `(38,18)` | Override per-column if business needs differ |

The deliberate stance: **additive, non-destructive evolution is automatic;
destructive or lossy changes require a human.**

---

## 5. AWS Glue DynamicFrames — creation & management

This section answers the explicit question — *how DynamicFrames are created and
managed* — even though the production jobs use DataFrames. It is the idiomatic Glue
toolkit for schema drift and semi-structured data.

### 5.1 What a DynamicFrame is

A `DynamicFrame` is a distributed collection of **self-describing `DynamicRecord`s**.
Unlike a DataFrame (one schema for all rows), each record can differ, and when Glue
sees the *same field with different types across records*, it does **not** fail —
it records a **`choice` type** (e.g. `int|string`) and lets you resolve it
explicitly later. This is precisely what makes them resilient to schema evolution.

### 5.2 Creating DynamicFrames

```python
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from pyspark.context import SparkContext

glueContext = GlueContext(SparkContext.getOrCreate())

# (a) From the Glue Data Catalog (schema + location come from the catalog table).
#     transformation_ctx is REQUIRED for job bookmarks to track progress.
dyf = glueContext.create_dynamic_frame.from_catalog(
    database="ingestion_db",
    table_name="aum_revenue_raw",
    transformation_ctx="dyf_aum",
)

# (b) From S3/JDBC directly, no catalog needed (connection + format options).
dyf = glueContext.create_dynamic_frame.from_options(
    connection_type="s3",
    connection_options={"paths": ["s3://bucket/data/in/"], "recurse": True},
    format="csv",
    format_options={"withHeader": True},
    transformation_ctx="dyf_s3",
)

# (c) From an existing Spark DataFrame (the bridge into Glue transforms).
dyf = DynamicFrame.fromDF(df, glueContext, "dyf_from_df")

# Inspect inferred (possibly ambiguous) schema:
dyf.printSchema()          # may show e.g.  amount: choice (double | string)
dyf.toDF().show(5)
```

> The job-bookmarks example in
> [`scenario_based_questions.md`](../../scenario_based_questions/scenario_based_questions.md)
> (Scenario 4) uses pattern **(b)** with `transformation_ctx` and then `toDF()`.

### 5.3 Managing DynamicFrames — the core transforms

| Transform | Purpose |
|-----------|---------|
| `resolveChoice` | Resolve `choice` (ambiguous) columns — the key schema-evolution tool (see §6) |
| `apply_mapping` / `ApplyMapping` | Rename, reorder, **cast** columns in one declarative step: `[("src","string","tgt","bigint"), …]` |
| `relationalize` | Flatten nested JSON/arrays into related flat frames (great for loading into relational targets) |
| `unbox` | Parse a string column containing JSON/CSV into structured fields |
| `drop_fields` / `select_fields` | Project columns |
| `drop_nulls` / `DropNullFields` | Remove all-null columns |
| `rename_field` | Rename a single (possibly nested) field |
| `map` / `filter` | Per-record Python UDF transform / predicate |
| `join` / `union` / `mergeDynamicFrame` | Combine frames; `mergeDynamicFrame` merges on keys |
| `split_fields` / `split_rows` | Partition a frame into multiple |

```python
# Declarative cast + rename + reorder — the DynamicFrame equivalent of cast_like()
mapped = dyf.apply_mapping([
    ("transaction_id", "string", "transaction_id", "string"),
    ("revenue",        "string", "revenue",        "decimal(38,18)"),
    ("report_date",    "string", "report_date",    "date"),
])
```

### 5.4 Per-record error handling

A major DynamicFrame advantage: bad records do not abort the job. Failures are
captured and inspectable.

```python
resolved = dyf.resolveChoice(choice="make_struct")
errors_dyf = resolved.errorsAsDynamicFrame()        # rows that failed resolution
print("error count:", resolved.stageErrorsCount())
# Glue also supports a stageThreshold / totalThreshold to fail only past a tolerance.
```

### 5.5 Writing DynamicFrames

```python
glueContext.write_dynamic_frame.from_options(
    frame=resolved,
    connection_type="s3",
    connection_options={"path": "s3://bucket/curated/aum/"},
    format="parquet",
)

# Or via a sink, which can also evolve the Data Catalog (see §6.3):
sink = glueContext.getSink(connection_type="s3", path="s3://bucket/curated/aum/",
                           enableUpdateCatalog=True, updateBehavior="UPDATE_IN_DATABASE")
sink.setCatalogInfo(catalogDatabase="curated_db", catalogTableName="aum")
sink.setFormat("glueparquet")
sink.writeFrame(resolved)
```

---

## 6. Schema evolution *with* DynamicFrames

If this pipeline were rebuilt around DynamicFrames, schema evolution would be
expressed declaratively instead of via `information_schema` reconciliation.

### 6.1 Resolving type conflicts (`resolveChoice`)

When a column arrives as mixed types across files/records, Glue keeps a `choice`.
You decide how to collapse it:

```python
# Cast every choice to the widest safe type (e.g. choice<int,string> → string)
resolved = dyf.resolveChoice(choice="cast:string")

# Keep BOTH representations as a struct (no data loss; resolve downstream)
resolved = dyf.resolveChoice(choice="make_struct")

# Per-column policy — mirrors this repo's DECIMAL-for-money intent
resolved = dyf.resolveChoice(specs=[
    ("revenue", "cast:decimal"),
    ("flags",   "make_cols"),     # explode choice into revenue_int, revenue_string, …
])
```

| `choice` option | Effect |
|-----------------|--------|
| `make_cols` | Splits the ambiguous column into one column per observed type |
| `make_struct` | Wraps both types in a struct — lossless |
| `cast:<type>` | Forces a single type |
| `project:<type>` | Keeps only values already of that type |

### 6.2 Merging evolving datasets (`mergeDynamicFrame`)

```python
# Upsert-style merge of an incremental frame into a base frame on natural keys
merged = base_dyf.mergeDynamicFrame(incremental_dyf, paths=["transaction_id", "report_date"])
```

### 6.3 Evolving the Glue Data Catalog automatically

For S3/lakehouse targets, the **sink** can grow the catalog schema as new columns
appear — the catalog analogue of this repo's `ALTER TABLE ADD COLUMN`:

```python
sink = glueContext.getSink(
    connection_type="s3", path="s3://bucket/curated/aum/",
    enableUpdateCatalog=True,
    updateBehavior="UPDATE_IN_DATABASE",          # add new columns to the catalog table
    partitionKeys=["report_date"],
)
sink.setCatalogInfo(catalogDatabase="curated_db", catalogTableName="aum")
sink.setFormat("glueparquet")
sink.writeFrame(resolved)
```

| Mechanism | Where evolution lands |
|-----------|-----------------------|
| `resolveChoice` | In-memory type unification |
| `enableUpdateCatalog` + `updateBehavior=UPDATE_IN_DATABASE` | Glue Data Catalog table definition |
| Crawler with an update policy | Catalog (scheduled, out-of-band) |
| Parquet/Delta/Iceberg `mergeSchema` | The physical table format (see `glue_features.md`) |

### 6.4 Open table formats (the modern alternative)

Per [`glue_features.md`](../../glue_features/glue_features.md), Glue 5.0 ships
Iceberg 1.7.1 / Delta 3.3.0 / Hudi 0.15.0. For a data-lake target these handle
schema evolution natively (`mergeSchema`, `ALTER TABLE ADD COLUMNS`, column
mapping) and add time-travel/branching — often a better fit than hand-rolled
reconciliation **when the target is the lake** rather than Redshift.

---

## 7. Building the entire ELT on DynamicFrames (module-by-module)

This section answers: *if we wanted to build **every module** of the ELT script on
DynamicFrames — schema evolution included — how would we approach it?* It is a
**design alternative**, not a description of the current code. Read it as the
blueprint you'd follow if you re-platformed the jobs onto the idiomatic Glue stack.

The single biggest architectural shift is the **load path**: Glue's native Redshift
connector (`write_dynamic_frame`) replaces the manual
`coalesce(1) → S3 CSV → COPY → staging table → MERGE` plumbing with one call that
takes `preactions` / `postactions`. Type handling moves into `apply_mapping` +
`resolveChoice`. Schema evolution splits by target: **automatic** for an
S3/Data-Catalog target (`enableUpdateCatalog`), but still **explicit** for Redshift
(the connector does *not* `ALTER` Redshift tables for you — see §7.4).

### 7.1 Module map — what changes, what stays

| Current module (function) | Today (DataFrame) | Rebuilt on DynamicFrames |
|---------------------------|-------------------|--------------------------|
| Init (`initialize_glue`) | `GlueContext` + `Job` | **Unchanged** |
| Ingest (`read_csv_file`) | `spark.read.csv` | `create_dynamic_frame.from_options/from_catalog` + `transformation_ctx` (§7.2) |
| Normalise + cast (`_clean_colname`, `cast_like`) | `withColumnRenamed`, `F.col().cast()` | `apply_mapping` + `resolveChoice` (§7.3) |
| Schema discovery (`read_redshift_table_schema`) | `SELECT … WHERE 1=0` probe | Same probe; compare against **resolved DynamicFrame schema** (§7.4) |
| Evolution (`alter_redshift_table`, `fill_missing_columns`, `alter_varchar_columns`) | `ALTER TABLE` via Data API + back-fill | Drift → **`preactions`** on the sink; back-fill via `apply_mapping` (§7.4) |
| Load + upsert (`write` + `create_staging_table` + `copy_to_redshift` + `run_merge`) | Manual S3 + COPY + DELETE/INSERT | **`write_dynamic_frame` with `preactions`/`postactions`** (§7.5) |
| Dimensions/facts (`process_*`) | Spark select/join + SP calls | `SelectFields`/`apply_mapping`/`Join` + SP calls as `postactions` (§7.6) |
| Data-quality guard (`check_datatype_matching`) | Type check, raise | `resolveChoice("make_struct")` + `errorsAsDynamicFrame` + Glue DQ (§7.7) |
| Audit / logging / retries / archive | boto3 + `LogBuffer` | **Unchanged** — orthogonal to the frame type (§7.8) |

> Two modules carry most of the benefit (ingest + load); the warehouse-side SQL
> (merge, SCD, fact loads) is best left in the existing **stored procedures**,
> invoked as connector `postactions`.

### 7.2 Module: Ingest

Create the DynamicFrame at the S3/catalog boundary. `transformation_ctx` is what
wires in **job bookmarks** (incremental file tracking).

```python
def read_source(config, glueContext):
    source = f"s3://{config['src_bucket']}/data/in/{config['source_file_name']}"
    dyf = glueContext.create_dynamic_frame.from_options(
        connection_type="s3",
        connection_options={"paths": [source]},
        format="csv",
        format_options={"withHeader": True},
        transformation_ctx=f"read_{config['target_table']}",   # ← bookmarks key
    )
    return dyf
```

### 7.3 Module: Type conform & column normalisation

The DataFrame job renames columns then casts (`cast_like`: non-key `double →
decimal(38,18)`). On DynamicFrames this is one declarative `apply_mapping`, with
`resolveChoice` to settle any mixed-type columns first.

```python
def conform(dyf, config):
    # 1) Settle ambiguity. Keep both representations rather than guessing:
    dyf = dyf.resolveChoice(specs=[(c, "make_struct") for c in dyf.toDF().columns])
    # (or, simply: dyf.resolveChoice(choice="cast:string") and retype below)

    # 2) Rename + cast + reorder in one step. Mirrors cast_like()'s DECIMAL rule.
    upsert = set(k.lower() for k in config["upsert_keys"])
    mappings = []
    for f in dyf.toDF().schema.fields:
        src = f.name
        tgt = _clean_colname(src)
        tgt_type = "decimal(38,18)" if (_is_double(f.dataType) and tgt not in upsert) \
                   else _glue_type(f.dataType)
        mappings.append((src, _glue_type(f.dataType), tgt, tgt_type))
    return dyf.apply_mapping(mappings)
```

> `apply_mapping` is also the clean home for a **column-rename map** (§4.7): list the
> old name as source and the new name as target, and renames stop looking like
> "add + drop".

### 7.4 Module: Schema discovery & evolution (against Redshift)

**Be clear-eyed here:** the Glue Redshift connector loads data but does **not**
alter the Redshift table to match the frame. `enableUpdateCatalog` evolves the
**Glue Data Catalog**, not a Redshift table reached over JDBC. So for a Redshift
target you keep an explicit reconciliation step — but now you drive it from the
**resolved DynamicFrame schema** and emit the DDL as connector **`preactions`** so
schema change + load happen atomically in one sink call.

```python
def build_preactions(dyf, config, redshift_conn, client):
    """Compute Redshift schema drift and return (preactions_sql, aligned_dyf)."""
    target = read_redshift_table_schema(config, redshift_conn, spark, client)  # reuse probe
    tgt_cols = {f.name: f.dataType for f in target.schema.fields}
    src_cols = {f.name: f.dataType for f in dyf.toDF().schema.fields}
    schema, table = redshift_conn["schema_name"], config["target_table"]

    pre = [f"CREATE TABLE IF NOT EXISTS {schema}.{table}_stg (LIKE {schema}.{table});",
           f"TRUNCATE TABLE {schema}.{table}_stg;"]

    # New source columns → ADD COLUMN to BOTH target and staging (additive evolution)
    for name, dt in src_cols.items():
        if name not in tgt_cols and _valid_ident(name):           # §8.7 hardening
            rtype = _spark_to_redshift_type(dt)
            pre.append(f"ALTER TABLE {schema}.{table} ADD COLUMN {name} {rtype};")
            pre.append(f"ALTER TABLE {schema}.{table}_stg ADD COLUMN {name} {rtype};")

    # Target columns missing from source → back-fill in the frame (not in SQL)
    df = dyf.toDF()
    for name, dt in tgt_cols.items():
        if name not in src_cols:
            df = df.withColumn(name, F.lit(get_default_value(dt)))
    aligned = DynamicFrame.fromDF(df.select(*tgt_cols.keys()), glueContext, "aligned")
    return ";\n".join(pre), aligned
```

- **New columns** become `ALTER TABLE … ADD COLUMN` preactions (the DynamicFrame
  equivalent of `alter_redshift_table`).
- **Missing columns** are back-filled in the frame with `withColumn` (equivalent of
  `fill_missing_columns`).
- **VARCHAR/INT widening** (`alter_varchar_columns`) is unchanged in spirit: compute
  `max(length)` / `max(abs)` (one aggregate row — cheap), and append the widening
  `ALTER`s to `preactions`.
- **Type ambiguity** that the DataFrame path could not see is now first-classed by
  `resolveChoice` in §7.3 — a genuine *gain*.

### 7.5 Module: Load + upsert (the big win)

This replaces five functions (`df.write`, `_find_single_csv_in_prefix`,
`create_staging_table`, `copy_to_redshift`, `run_merge`) with **one** connector
call. The connector writes the frame to `redshift_tmp_dir` and runs the `COPY`
internally; `preactions` create/evolve staging, `postactions` run the
delete/insert merge and drop staging.

```python
def load_and_merge(dyf, config, redshift_conn, client):
    pre, aligned = build_preactions(dyf, config, redshift_conn, client)
    schema, table = redshift_conn["schema_name"], config["target_table"]
    keys = config["upsert_keys"]
    on = " AND ".join(f"{table}.{k} = {table}_stg.{k}" for k in keys)

    post = (
        f"BEGIN;"
        f"DELETE FROM {schema}.{table} USING {schema}.{table}_stg WHERE {on};"
        f"INSERT INTO {schema}.{table} SELECT * FROM {schema}.{table}_stg;"
        f"DROP TABLE {schema}.{table}_stg;"
        f"END;"
    )
    # You can equally call the existing SP: postactions=f"CALL public.sp_merge_from_staging(...)"

    glueContext.write_dynamic_frame.from_options(
        frame=aligned,
        connection_type="redshift",
        connection_options={
            "redshiftTmpDir": redshift_conn["tmp_dir"],
            "useConnectionProperties": "true",
            "dbtable": f"{schema}.{table}_stg",   # load into staging…
            "connectionName": "redshift-glue-connection",
            "preactions":  pre,                    # …create/evolve it first
            "postactions": post,                   # …then MERGE into target
        },
        transformation_ctx=f"load_{table}",
    )
```

> **What you no longer maintain:** the single-file `coalesce(1)` write, the
> "find the one CSV under the prefix" helper, manual `COPY` SQL, and staging-file
> cleanup. The connector owns the S3 temp round-trip.

### 7.6 Module: Dimensional model (SCD 1/2 + facts)

The dimensional functions are mostly *projection + dedupe + SQL*. DynamicFrame
transforms cover the projection cleanly; the relational merge logic stays in the
stored procedures, called as `postactions`.

```python
def process_dimension_dyf(dyf, dim_config, redshift_conn):
    # column_mappings (target ← source) via apply_mapping
    maps = [(src, "string", tgt, "string") for tgt, src in dim_config["column_mappings"].items()]
    dim = dyf.apply_mapping(maps).select_fields(list(dim_config["column_mappings"].keys()))
    dim = DynamicFrame.fromDF(dim.toDF().dropDuplicates(), glueContext, "dim")  # distinct

    glueContext.write_dynamic_frame.from_options(
        frame=dim, connection_type="redshift",
        connection_options={
            "connectionName": "redshift-glue-connection",
            "redshiftTmpDir": redshift_conn["tmp_dir"],
            "dbtable": f"{redshift_conn['schema_name']}.stg_{dim_config['dimension_table']}",
            "preactions":  "CREATE TABLE IF NOT EXISTS …; TRUNCATE …;",
            "postactions": f"CALL public.sp_process_scd_type2(…);",   # reuse existing SP
        },
        transformation_ctx=f"dim_{dim_config['dimension_table']}",
    )
```

- **Facts** need surrogate-key lookups — i.e. joins. Use `Join.apply(factDyf, dimDyf,
  …)` for the DynamicFrame-native path, or drop to Spark (`toDF()`) for complex
  multi-dimension joins, then load. The repo's `sp_load_fact_table` postaction
  approach (join in Redshift after staging the natural keys) remains the simplest.
- **`relationalize`** would matter if a source were nested JSON (flatten arrays into
  child frames before loading); the current CSV sources are flat, so it is not
  needed — but it is the tool you'd reach for if inputs gained nesting.

> **Honest call:** DynamicFrames add little to the star-schema *SQL*. Keep the
> stored procedures (`03_scd_type2.sql`, `04_scd_type1.sql`, `05_fact_loader.sql`)
> and invoke them as `postactions`. Use DynamicFrames for ingest/typing/loading.

### 7.7 Module: Data quality & error capture

`check_datatype_matching` becomes richer and non-fatal-by-default:

```python
resolved = dyf.resolveChoice(choice="make_struct")   # don't lose ambiguous values
bad = resolved.errorsAsDynamicFrame()                 # quarantine instead of crashing
if resolved.stageErrorsCount() > config.get("error_threshold", 0):
    glueContext.write_dynamic_frame.from_options(
        frame=bad, connection_type="s3",
        connection_options={"path": f"s3://{config['src_bucket']}/data/quarantine/"},
        format="json")
    raise RuntimeError(f"{resolved.stageErrorsCount()} bad records exceeded threshold")
```

Pair this with a **Glue Data Quality** ruleset (`EvaluateDataQuality`, DQDL) as a
declarative gate — the modern replacement for hand-written type checks, and a
natural companion to `stored_procedures/07_data_quality.sql`.

### 7.8 Modules that stay the same

Audit (`update_job_sts_table`), structured logging (`LogBuffer`), retry/backoff
(`retry_on_exception`), and S3 archive/cleanup are **boto3 utilities independent of
the frame type** — port them unchanged. `job.commit()` (already present) is what
persists bookmark state from §7.2.

### 7.9 The "pure-Glue" variant — S3 / Lakehouse target = *automatic* schema evolution

If the warehouse contract allowed an **S3 / Data-Catalog** target (Parquet/Iceberg/
Delta) instead of a Redshift table, schema evolution becomes fully automatic and
the §7.4 reconciliation module disappears:

```python
sink = glueContext.getSink(
    connection_type="s3", path="s3://bucket/curated/aum/",
    enableUpdateCatalog=True,
    updateBehavior="UPDATE_IN_DATABASE",      # new columns auto-added to the catalog
    partitionKeys=["report_date"],
)
sink.setCatalogInfo(catalogDatabase="curated_db", catalogTableName="aum")
sink.setFormat("glueparquet")
sink.writeFrame(resolved)
# Redshift then reads via Spectrum / Lakehouse (see glue_features.md §S, §L)
```

This is the only configuration where DynamicFrames give you *hands-off* schema
evolution end-to-end. Against a strict Redshift table you always keep an explicit
DDL step (§7.4) — which is exactly why the current design uses DataFrames.

### 7.10 End-to-end skeleton

```python
def main_dynamic():
    spark, glueContext, job = initialize_glue(config["job_name"])
    dyf = read_source(config, glueContext)                 # §7.2 ingest + bookmarks
    dyf = conform(dyf, config)                             # §7.3 resolveChoice + apply_mapping
    dyf = add_audit_fields(dyf)                            # run_date / file_name (Map or toDF)
    dyf = quality_gate(dyf, config)                        # §7.7 errors → quarantine
    if check_table_exists(redshift_conn, config, client):
        load_and_merge(dyf, config, redshift_conn, client)  # §7.4 preactions + §7.5 connector
    else:
        create_new_redshift_table(config, redshift_conn, dyf.toDF(), client, log)
        load_and_merge(dyf, config, redshift_conn, client)
    process_dimensional_model(dyf, config, redshift_conn)  # §7.6 (data-model job only)
    update_job_sts_table(...)                              # §7.8 unchanged
    job.commit()                                           # persist bookmark
```

### 7.11 Trade-offs of the DynamicFrame rebuild

| You gain | You pay |
|----------|---------|
| `resolveChoice` first-classes mixed-type columns the DataFrame path can't see | `choice` types can mask real data problems if blindly `cast` |
| Connector-managed `COPY` — delete ~5 functions of S3/COPY plumbing | The Redshift connector needs a **Glue JDBC Connection + VPC route**, losing the **Data API's no-VPC simplicity** the repo deliberately chose (`data_pipeline_logic.md`) |
| Per-record error capture + quarantine (`errorsAsDynamicFrame`) | Manage `redshiftTmpDir` lifecycle/permissions |
| Job bookmarks via `transformation_ctx` | Redshift DDL evolution **still not automatic** — you keep §7.4 (now schema-driven, run as `preactions`) |
| **Automatic** catalog schema evolution *iff* the target is S3/Catalog (§7.9) | Slight per-record overhead vs DataFrames |

**Bottom line:** a DynamicFrame rebuild is most compelling if (a) inputs become
messy/semi-structured, (b) you want connector-managed loads and bookmarks, or
(c) the target moves to an S3/Lakehouse table where schema evolution is automatic.
For a strict Redshift target with clean CSVs, the current DataFrame design stays
competitive — and you can adopt DynamicFrames **surgically at the ingest edge**
(§7.2–7.3) without rewriting the load path.

---

## 8. Other important scenarios & best practices

These are grounded in what the jobs already do well, plus gaps worth closing.

### 8.1 Idempotency & exactly-once (implemented)

The MERGE is a **delete-then-insert on upsert keys** (`sp_merge_from_staging`,
`stored_procedures/02_merge_upsert.sql`), wrapped so re-running the same file
yields the same result. Combined with **SQS FIFO + Lambda reserved concurrency 1 +
Step Functions `MaxConcurrency: 1`**, the pipeline avoids concurrent writers to the
same table. Keep upsert keys genuinely unique, or duplicates collapse silently.

### 8.2 Staging table + COPY + MERGE (implemented)

Never `INSERT` row-by-row into Redshift. The job writes one CSV, `COPY`s into a
staging table, then MERGEs. `COPY` is massively parallel; the staging hop makes the
upsert atomic and lets the load fail without partially mutating the target.

### 8.3 `coalesce(1)` before COPY (implemented — know the trade-off)

`df.coalesce(1)` forces a single output file so `COPY` reads a consistent object and
header handling is unambiguous. **Cost:** it funnels the final write through one
task — fine for these report-sized files, but for multi-GB outputs prefer multiple
files (Redshift `COPY` parallelises across them) or write a manifest.

### 8.4 Run-isolated staging paths (implemented)

Staging S3 prefixes are keyed by `run_id` (`…/{target_table}_{staging}/{run_id}`),
so concurrent or retried runs never clobber each other's files. Staging tables are
likewise suffixed with a UUID in the data-model job.

### 8.5 `inferSchema` reads the file twice (gap)

`inferSchema=true` triggers an extra full pass before processing. For large inputs,
pass an **explicit schema** (you already build typed schemas from Redshift — reuse
that), or read everything as string and cast deliberately. Saves a whole scan.

### 8.6 Job bookmarks for incremental ingest (recommended)

Today the pipeline is one-file-per-trigger. For folder-level or re-driven loads,
enable bookmarks (`--job-bookmark-option=job-bookmark-enable`) and set a stable,
unique `transformation_ctx` per source, then `job.commit()` (already called). See
`scenario_based_questions.md` Scenario 4.

### 8.7 SQL string-building / injection hardening (gap — important)

Most DDL/DML is assembled with f-strings from config and column names, e.g.
`f"ALTER TABLE {schema}.{table} ADD COLUMN {col} {rtype};"`. The audit writer
escapes quotes (`update_job_sts_table`), but identifiers are not validated. Because
`target_table`/`upsert_keys`/column names flow from `config.json` and file headers,
treat those as **trusted-but-verify**:

- Validate identifiers against `^[A-Za-z_][A-Za-z0-9_]*$` before interpolation.
- Prefer parameterised `execute_statement(..., Parameters=[...])` for **values**.
- Keep using stored procedures for the heavy DML (already the pattern).

### 8.8 Data quality gates (partially present)

`stored_procedures/07_data_quality.sql` and the commented `check_datatype_matching`
show intent. Recommended additions: row-count drift vs prior load, null-rate checks
on keys, duplicate-key detection **before** MERGE, and — if you adopt Glue
visual/Spark DQ — **Glue Data Quality (DQDL)** rulesets that fail the job on
violation and emit results to CloudWatch.

### 8.9 Structured logging & audit (implemented — strong)

`LogBuffer` emits JSON to CloudWatch **and** exports the full buffer to
`s3://…/logs/YYYY/MM/DD/`, correlated by `run_id`; `job_sts` records counts and
status per run. Keep `run_id` on every log line and in the audit row so a failure
is traceable across CloudWatch, S3 logs, and Redshift.

### 8.10 Spark hygiene

- Avoid `df.collect()` / `df.toPandas()` on full datasets (driver OOM). The single
  aggregation `collect()` in `alter_varchar_columns` is fine — it returns one row.
- Cache only when reused; the job mostly streams once, so caching is unneeded.
- Watch for **small-file** and **skew** problems on larger inputs; tune
  `spark.sql.shuffle.partitions` and partition keys rather than always `coalesce(1)`.

### 8.11 Resilience: retries, failure routing (implemented)

`@retry_on_exception` wraps Redshift Data API calls with exponential backoff (5→120s,
3 attempts). On failure the source file is moved to `data/unprocessed/YYYY/MM/`, the
audit row is marked `FAILED`, logs are flushed, and the exception re-raises so Step
Functions sees it. The data-model job additionally wraps star-schema processing in
try/except so **base ETL success is preserved** even if dimensional load fails — a
good blast-radius boundary.

### 8.12 Worker sizing, timeouts, cost

Pick the smallest worker type that fits (`G.1X`/`G.2X`), enable **auto-scaling**,
and set a **job timeout** so a hung run can't burn DPU-hours. These files are small,
so a low `NumberOfWorkers` is appropriate — see `docs/operations/cost_estimation.md`.

### 8.13 Secrets & connectivity (implemented)

Redshift credentials come from **Secrets Manager** via `secret_arn`; the **Redshift
Data API** is used (HTTP, no VPC/driver/pooling) — simpler than JDBC for batch and a
deliberate design choice (see the "Data API vs JDBC" note in
`data_pipeline_logic.md`).

### 8.14 Glue version selection

Target **Glue 5.0** (Spark 3.5.4 / Python 3.11) for the performance and cost wins
and `requirements.txt` dependency management documented in `glue_features.md`,
unless a library pins you to an older runtime.

---

## 9. Design considerations & trade-offs

| Decision in this repo | Benefit | Trade-off / when to revisit |
|-----------------------|---------|------------------------------|
| `DataFrame` + Redshift-anchored reconciliation | Precise DDL, typed warehouse, simple mental model | More bespoke code than DynamicFrame auto-resolution; less tolerant of truly chaotic input |
| Target table = schema authority | Stable downstream contracts, views safe | Source-driven additions need a write to Redshift each drift |
| Additive-only evolution | No accidental data loss | Renames/drops/narrowing need manual migration |
| `DECIMAL(38,18)` for non-key doubles | No float drift on money | Slightly larger storage; uniform precision may over-provision |
| `COPY` + staging + delete/insert MERGE | Fast, atomic, idempotent | `COPY` is positional → column alignment is mandatory |
| Redshift Data API | No VPC/driver, easy retries | Async polling; not ideal for high-frequency tiny writes |
| Serial execution (FIFO + concurrency 1) | No merge conflicts | Lower throughput; parallelise per-table if needed |
| DynamicFrames **not** used | Less abstraction, faster, predictable | Lose per-record error capture & `choice` resolution — adopt at the ingest edge (§7) if inputs get messy |

---

## 10. Quick reference

### Function → responsibility map (both jobs)

| Function | Schema-evolution role |
|----------|------------------------|
| `read_csv_file` | Read CSV, normalise names, cast (double→decimal), add audit cols |
| `read_redshift_table_schema` | Probe target schema via `SELECT … WHERE 1=0` → typed empty DF |
| `check_table_exists` | Decide create-vs-evolve path |
| `create_new_redshift_table` | DDL from Spark schema; `NOT NULL` on keys; create view |
| `alter_redshift_table` | `ADD COLUMN` for new source columns (drop/recreate view) |
| `fill_missing_columns` | Back-fill target columns absent from source |
| `alter_varchar_columns` | Widen `VARCHAR`; promote `SMALLINT→INT→BIGINT` |
| `check_datatype_matching` | (Optional) reject lossy text→number loads |
| `df.select(target order)` | Positional alignment before `COPY` |
| `create_staging_table` / `copy_to_redshift` / `run_merge` | Staging → COPY → idempotent MERGE |
| `process_*_dimension` / `process_fact_from_config` | SCD1/SCD2 + fact loads (data-model job) |

### Glossary

- **`choice` type** — a DynamicFrame column whose type varies across records;
  resolved with `resolveChoice`.
- **Target-anchored evolution** — the destination schema, not the file, defines the
  contract; the file is conformed to it.
- **Additive evolution** — only add columns / widen types; never drop or narrow
  automatically.
- **Positional COPY** — Redshift `COPY` maps CSV columns by order, so DataFrame
  column order must match the table.

### See also

- [`data_pipeline_logic.md`](./data_pipeline_logic.md) — end-to-end pipeline walkthrough
- [`scenario_based_questions.md`](../../scenario_based_questions/scenario_based_questions.md) — schema-evolution & bookmark scenarios
- [`glue_features.md`](../../glue_features/glue_features.md) — Glue 5.0 / table formats / FGAC
- [`docs/operations/cost_estimation.md`](../operations/cost_estimation.md) — worker sizing & cost
- AWS docs: *Glue DynamicFrame class*, *ResolveChoice transform*, *Updating the Data Catalog from a job*, *Glue job bookmarks*, *Redshift Data API*
