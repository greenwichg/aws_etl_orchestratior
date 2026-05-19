# Data Pipeline Logic

This document explains what each component of the ETL pipeline does step-by-step — from file arrival in S3 through to the final Redshift load and audit log. Covers both Glue jobs: the simple ETL and the data model (star schema) variant.

---

## End-to-End Flow

```
1. File lands in S3 (data/in/)
2. EventBridge detects ObjectCreated event
3. SQS FIFO queue buffers the event
4. Lambda reads config.json → validates file → triggers Step Functions
5. Step Functions runs the appropriate Glue job
6. Glue job:
   a. Reads CSV from S3
   b. Infers / aligns schema against Redshift target table
   c. Writes single CSV to S3 staging path
   d. COPY staging CSV into Redshift staging table
   e. MERGE staging → target table (UPSERT)
   f. Updates JOB_STS audit table
   g. Archives source file, cleans up staging
   h. Exports structured JSON log to S3
```

---

## Stage 1: Lambda Orchestrator

**File:** `lambda_functions/lambda_function.py`

### What It Does

The Lambda is triggered by SQS (which receives EventBridge events from S3 `ObjectCreated`). It performs three tasks:

#### 1. Event Parsing

Supports two event shapes:
- **SQS-wrapped (production):** EventBridge event is JSON-encoded in `Records[0].body`
- **Direct EventBridge (testing):** Event is the raw EventBridge payload

Extracts: `bucket_name`, `object_key`

#### 2. Config Matching

Loads `config/config.json` from S3 — a JSON array of job definitions:

```json
[
  {
    "job_id": "JOB_001",
    "job_name": "customer_load",
    "source_file_name": "customers.csv",
    "target_table": "customers",
    "upsert_keys": ["customer_id"],
    "is_active": true,
    "glue_job": "simple"
  }
]
```

Matches the uploaded filename (case-insensitive, stripped) against `source_file_name`.

#### 3. Three Outcomes

| Outcome | Condition | Action |
|---------|-----------|--------|
| `not_configured` | File not found in config | Move file to `data/new_files/`, send SNS alert |
| `inactive` | `is_active: false` | Send SNS alert, do nothing else |
| `triggered` | Active config match | Start Step Functions execution |

#### 4. Glue Job Routing

The `glue_job` field in config selects the Glue script:
- `"glue_job": "simple"` → `etl-orchestrator-{env}-etl-simple` (no dimensional model)
- `"glue_job": "data_model"` → `etl-orchestrator-{env}-etl-data-model` (star schema)

#### 5. Step Functions Input Payload

```json
{
  "glue_jobs": [
    {
      "glue_job_name": "etl-orchestrator-dev-etl-simple",
      "job_id": "JOB_001",
      "job_name": "customer_load",
      "source_file_name": "customers.csv",
      "target_table": "customers",
      "upsert_keys": ["customer_id"]
    }
  ]
}
```

---

## Stage 2: Step Functions

**File:** `terraform/modules/step_functions/main.tf`

The state machine has two states:

### `ProcessJobs` (Map state)

- Iterates over the `glue_jobs` array from the Lambda payload
- `MaxConcurrency: 1` — runs jobs **serially**, one at a time
- Passes each item to the `RunGlueJob` state

### `RunGlueJob` (Task state)

- Uses the `.sync` integration pattern (`arn:aws:states:::glue:startJobRun.sync`)
- Starts the Glue job and **waits for it to complete** before moving to the next
- Passes `job_id`, `job_name`, `source_file_name`, `target_table`, `upsert_keys` as Glue arguments
- On any error → `LogError` (Pass state, continues — does not fail the state machine)

> **Why `.sync`?** Without it, Step Functions starts the Glue job and immediately moves to the next state, making it impossible to detect failures or enforce serial execution.

---

## Stage 3: Glue ETL — Simple Job

**File:** `glue_jobs/without_data_model/glue_job.py`

### 3.1 Initialisation

```python
spark, glue_context, job = initialize_glue(job_name)
client = boto3.client("redshift-data", region_name=region)
run_id = uuid.uuid4().hex
log = LogBuffer(run_id)
```

- Creates a Spark session via `GlueContext`
- Opens a Redshift Data API client
- Generates a unique `run_id` for log correlation
- Initialises a `LogBuffer` — structured JSON logs emitted to both CloudWatch and S3

### 3.2 Read Source CSV

```python
df = read_csv_file(config, spark)
```

- Reads from `s3://{bucket}/data/in/{source_file_name}`
- `inferSchema=true` — Spark infers column types automatically
- Column names are normalised: lowercased, spaces/special chars replaced with `_`, duplicates de-duped
- Optional: casts columns to types specified in config override

### 3.3 Schema Reconciliation

**Case A — Target table does NOT exist:**

```python
create_new_redshift_table(config, redshift_conn, df, client, log)
```

- Derives `CREATE TABLE` DDL from the Spark DataFrame schema
- Maps Spark types to Redshift types (e.g. `StringType → VARCHAR(65535)`, `LongType → BIGINT`)
- Executes via Redshift Data API

**Case B — Target table EXISTS:**

```python
redshift_df = read_redshift_table_schema(...)
if set(df.columns) != set(redshift_df.columns):
    df = fill_missing_columns(df, redshift_df, log)
    alter_redshift_table(...)
```

- Reads current Redshift schema via `information_schema.columns`
- If source has **new columns**: adds them with `ALTER TABLE ADD COLUMN`
- If source is **missing columns** that Redshift has: fills them with `NULL` in the DataFrame
- Also widens `VARCHAR` columns if source values exceed current column length (`alter_varchar_columns`)

### 3.4 Write to S3 Staging

```python
s3_staging_path = f"s3://{bucket}/data/staging/{target_table}_{staging}/{run_id}"
df.coalesce(1).write.mode("overwrite").option("header", True).csv(s3_staging_path)
```

- `coalesce(1)` — forces output to a **single CSV file** (required for Redshift COPY)
- Uses `run_id` in the path to prevent collisions between concurrent runs

### 3.5 Redshift COPY

```python
create_staging_table(...)   # CREATE TABLE {target}_staging LIKE {target}
copy_to_redshift(...)       # COPY {staging_table} FROM 's3://...' IAM_ROLE '...' CSV HEADER
```

- Creates a staging table with the same schema as the target
- Executes COPY from the single S3 CSV using the `redshift-s3-access` IAM role
- Polls `DescribeStatement` until COPY completes (async API)

### 3.6 MERGE / UPSERT

```python
run_merge(config, redshift_conn, staging_table_name, client, log)
```

Executes a two-step merge pattern:

```sql
-- Step 1: Delete rows from target that exist in staging (matched on upsert_keys)
DELETE FROM {target_table}
USING {staging_table}
WHERE {target}.{key} = {staging}.{key} [AND ...];

-- Step 2: Insert all rows from staging into target
INSERT INTO {target_table}
SELECT * FROM {staging_table};

-- Step 3: Drop staging table
DROP TABLE {staging_table};
```

### 3.7 Audit — JOB_STS Table

```python
update_job_sts_table(config, redshift_conn, run_start_ts, run_end_ts,
                     source_filename, records_read, records_updated,
                     records_inserted, "SUCCESS", "NULL", client)
```

Records every run in the `job_sts` table:

| Column | Value |
|--------|-------|
| `job_id` | From config |
| `job_name` | From config |
| `source_file_name` | Uploaded filename |
| `run_start_ts` | Job start timestamp |
| `run_end_ts` | Job end timestamp |
| `records_read` | Row count from source CSV |
| `records_inserted` | `rows_after - rows_before` |
| `records_updated` | `records_read - records_inserted` |
| `status` | `SUCCESS` or `FAILED` |
| `error_message` | `NULL` or exception message |

### 3.8 Cleanup & Archive

**On success:**
```python
delete_staging_s3_files(s3_staging_path, log)       # remove temp staging CSV
move_s3_file_to_archive(config, s3_archive_path, log)  # move to data/archive/YYYY/MM/
log.export_to_s3(bucket, f"logs/YYYY/MM/DD", log_name)  # export structured log
```

**On failure:**
```python
update_job_sts_table(..., "FAILED", str(e), ...)
move_s3_file_to_unprocessed(config, s3_unprocessed_path, log)  # data/unprocessed/YYYY/MM/
log.export_to_s3(...)
raise  # re-raises to let Step Functions see the failure
```

---

## Stage 4: Glue ETL — Data Model Job (Star Schema)

**File:** `glue_jobs/with_data_model/glue_job_with_data_model.py`

This job extends the simple ETL with dimensional modelling. Steps 3.1–3.6 are identical; it adds:

### 4.1 Load Dimensional Config

```python
dim_config = load_dimensional_config(bucket, "config/dimensional_mappings.json")
```

Reads a separate JSON config that defines how to split the source file into dimensions and fact tables:

```json
{
  "dimensions": [
    {
      "table": "dim_customer",
      "source_columns": ["customer_id", "customer_name", "country"],
      "surrogate_key": "customer_sk",
      "natural_key": "customer_id",
      "scd_type": 1
    }
  ],
  "facts": [
    {
      "table": "fact_transactions",
      "measures": ["amount", "quantity"],
      "dimension_links": [
        {"dim_table": "dim_customer", "natural_key": "customer_id", "sk_column": "customer_sk"}
      ]
    }
  ]
}
```

### 4.2 Process Dimensions (SCD Type 1)

```python
process_dimension_from_config(df, dim_config, redshift_conn, client, log)
```

For each dimension:
1. Selects the dimension columns from the source DataFrame
2. Deduplicates on the natural key
3. Checks if the dimension table exists — creates it if not
4. Upserts records (DELETE + INSERT pattern, same as simple job)
5. Assigns surrogate keys if `surrogate_key` is defined

**SCD Type 1** — overwrites existing records. No history is kept; only the latest version of each dimension member is stored.

### 4.3 Process Facts

```python
process_fact_from_config(df, fact_config, redshift_conn, client, log)
```

For each fact table:
1. Joins source DataFrame to each dimension to resolve surrogate keys
2. Selects measure columns + resolved surrogate keys
3. Loads to Redshift fact table via staging + COPY + MERGE pattern

### 4.4 Create / Refresh Views

```python
create_views(config, redshift_conn, client, log)
```

- Loads view definitions from `config/views.json`
- Creates or replaces reporting views in Redshift (e.g. joining fact and dimension tables)
- Views are dropped and recreated on each run to reflect latest schema

---

## Retry & Error Handling

The `@retry_on_exception` decorator wraps all Redshift Data API calls:

```python
@retry_on_exception(max_attempts=3, base_delay=5, max_delay=120)
def execute_sql(sql, redshift_conn, client):
    ...
```

- **Max attempts:** 3
- **Backoff:** exponential — 5s, 10s, 20s (capped at 120s)
- **On final failure:** logs error, raises exception → Glue job fails → Step Functions catches it

---

## Logging Architecture

Every Glue run uses `LogBuffer` — a structured logging class that:

1. Emits JSON lines to **CloudWatch** (`print()` goes to `/aws-glue/jobs/output`)
2. Buffers all log lines in memory
3. Exports the complete buffer to **S3** at the end of the run (`logs/YYYY/MM/DD/`)

Log entry format:
```json
{"level": "INFO", "ts": "2024-01-15T10:30:00Z", "run_id": "abc123", "msg": "Records read", "count": 5000}
```

---

## S3 Path Reference

| Path | Content | Lifecycle |
|------|---------|-----------|
| `data/in/{file}` | Source file waiting to be processed | Deleted after successful load (moved to archive) |
| `data/staging/{table}/{run_id}/` | Single-file CSV for Redshift COPY | Deleted on success; kept for debugging on failure |
| `data/archive/YYYY/MM/{file}_{ts}.csv` | Successfully processed source file | Permanent |
| `data/unprocessed/YYYY/MM/{file}` | Failed source file | Manual review required |
| `data/new_files/{file}` | File with no matching config entry | Manual review required |
| `logs/YYYY/MM/DD/{file}_log_{ts}.txt` | Structured JSON run log | Permanent |
| `config/config.json` | Job routing configuration | Managed manually |
| `config/dimensional_mappings.json` | Star schema mapping (data model job) | Managed manually |
| `config/views.json` | Redshift view definitions | Managed manually |
| `scripts/glue_job.py` | Simple ETL Glue script | Deployed by Terraform |
| `scripts/glue_job_with_data_model.py` | Data model Glue script | Deployed by Terraform |

---

## Interview Tips

| Topic | What Interviewers Test |
|-------|----------------------|
| **Why `coalesce(1)` before COPY** | Redshift COPY expects one file (or a consistent set). Multiple part files from Spark's default partitioning can cause header row duplication. `coalesce(1)` forces a single output file. |
| **COPY vs INSERT** | COPY is massively parallel — AWS recommends it for any bulk load into Redshift. Individual INSERTs are row-by-row and orders of magnitude slower. |
| **Staging table pattern** | Load into a staging table first, then MERGE into the target. This avoids partial updates if the load fails and makes UPSERT atomic. |
| **SCD Type 1 vs Type 2** | Type 1: overwrite old values (no history). Type 2: add a new row with effective dates (full history). This pipeline implements SCD Type 1. |
| **Why serial execution in Step Functions** | `MaxConcurrency: 1` prevents two Glue jobs from running against the same Redshift table simultaneously, avoiding lock contention and data corruption. |
| **Schema evolution** | The pipeline handles new columns automatically via `ALTER TABLE ADD COLUMN`. It also widens VARCHAR columns if values exceed current length. No manual DDL changes needed for additive schema changes. |
| **`inferSchema` trade-off** | Convenient but adds a full DataFrame scan before processing (Spark reads the data twice). For very large files, providing an explicit schema is faster. |
| **Redshift Data API vs JDBC** | Data API is HTTP-based — no VPC, no connection pool, no driver. JDBC is faster for high-throughput streaming inserts but requires network connectivity to Redshift. For batch ETL the Data API is simpler and sufficient. |
