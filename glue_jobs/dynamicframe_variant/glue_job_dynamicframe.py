"""
DynamicFrame variant of the ETL Glue job (REFERENCE IMPLEMENTATION).

This is a standalone, runnable reference that implements the design in
`docs/pipeline/glue_schema_evolution_and_dynamicframes.md` (Section 8) using AWS Glue
DynamicFrames instead of plain Spark DataFrames.

It is NOT a replacement for the production scripts and does not modify them:
  - glue_jobs/without_data_model/glue_job.py        (production, untouched)
  - glue_jobs/with_data_model/glue_job_with_data_model.py (production, untouched)

What differs from the production DataFrame jobs
-----------------------------------------------
  1. Source is read with `create_dynamic_frame.from_options(... transformation_ctx=...)`,
     so Glue JOB BOOKMARKS actually track processed data (the production jobs use
     `spark.read.csv`, which does not participate in bookmarks — see doc Section 9).
  2. Type ambiguity is resolved with `resolveChoice`; precise typing reuses the same
     `cast_like` rule as production (non-key double -> DECIMAL(38,18)).
  3. The load path collapses `coalesce(1).write` + create-staging + COPY + merge into a
     SINGLE `write_dynamic_frame` call to the Redshift connector, carrying:
        - preactions : ALTER/CREATE staging+target (schema evolution lives here)
        - postactions: CALL public.sp_merge_from_staging(...)  (reuses the existing SP)

Prerequisites (vs the Data API used by production)
--------------------------------------------------
  * A Glue Connection to Redshift (JDBC) named via --connection_name, with the job
    placed in a VPC/subnet that can reach the Redshift endpoint. (The production jobs
    use the Redshift Data API, which needs no VPC — this is the trade-off documented
    in Section 8.5 / 10.14.)
  * A writable S3 temp location passed via --redshift_tmp_dir (the connector stages
    data there before COPY).
  * Job parameter `--job-bookmark-option job-bookmark-enable` to activate bookmarks.

Schema evolution scope (matches production semantics, Section 4):
  * additive only — new source columns -> ADD COLUMN; target-only columns -> back-filled;
    VARCHAR columns widened to fit. Integer promotion and view drop/recreate can be
    ported from the production `alter_varchar_columns` / `drop_views` the same way
    (emitted as additional preactions) — see the inline note in
    reconcile_and_build_preactions().
"""

import sys
import json
import re
import time
import uuid
import traceback
import functools
from datetime import datetime, timezone
from textwrap import dedent

import boto3
from awsglue.context import GlueContext
from awsglue.utils import getResolvedOptions
from awsglue.job import Job
from awsglue.dynamicframe import DynamicFrame
from pyspark.context import SparkContext
from pyspark.sql import functions as F
from pyspark.sql.functions import lit
from pyspark.sql.types import (
    StructType, StructField, StringType,
    IntegerType, FloatType, DoubleType, LongType, DecimalType,
    BooleanType, TimestampType, DateType, BinaryType
)

# ===============================================================
# LOGGING (structured + S3 export)  — identical to production
# ===============================================================


class LogBuffer:
    def __init__(self, run_id: str):
        self.lines = []
        self.run_id = run_id

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _emit(self, level: str, msg: str, **kv):
        payload = {"level": level, "ts": self._ts(), "run_id": self.run_id, "msg": msg}
        if kv:
            payload.update(kv)
        self.lines.append(json.dumps(payload, ensure_ascii=False))
        print(json.dumps(payload))  # also emit to CloudWatch

    def info(self, msg: str, **kv):
        self._emit("INFO", msg, **kv)

    def warning(self, msg: str, **kv):
        self._emit("WARNING", msg, **kv)

    def error(self, msg: str, **kv):
        self._emit("ERROR", msg, **kv)

    def export_to_s3(self, bucket: str, key_prefix: str, base_filename: str) -> str:
        key = f"{key_prefix.rstrip('/')}/{base_filename}"
        s3 = boto3.client('s3')
        body = "\n".join(self.lines).encode('utf-8')
        s3.put_object(Bucket=bucket, Key=key, Body=body)
        print(f"Logs exported to s3://{bucket}/{key}")
        return f"s3://{bucket}/{key}"

# ===============================================================
# RETRY LOGIC (exponential backoff)  — identical to production
# ===============================================================


def retry_on_exception(max_attempts=3, base_delay=5, max_delay=120, exceptions=(Exception,)):
    """Retry with exponential backoff: base_delay * 2^(attempt-1), capped at max_delay."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            _log = kwargs.get('log') or kwargs.get('logger')
            if _log is None:
                for obj in reversed(args):
                    if hasattr(obj, 'warning') and hasattr(obj, 'error'):
                        _log = obj
                        break
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        (_log.error if _log else print)(
                            f"{func.__name__} failed after {attempt} attempts: {e}")
                        raise
                    wait = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    (_log.warning if _log else print)(
                        f"{func.__name__} failed with {type(e).__name__}: {e}. "
                        f"Retrying in {wait}s (attempt {attempt}/{max_attempts})...")
                    time.sleep(wait)
        return wrapper
    return decorator

# ===============================================================
# GLUE INIT
# ===============================================================


def initialize_glue(job_name: str):
    sc = SparkContext.getOrCreate()
    glue_context = GlueContext(sc)
    spark = glue_context.spark_session
    job = Job(glue_context)
    # job.init + job.commit + transformation_ctx are what make bookmarks work.
    job.init(job_name, {"job_name": job_name})
    return spark, glue_context, job

# ===============================================================
# SMALL HELPERS  — ported from production for consistency
# ===============================================================


def _clean_colname(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_")


def _valid_ident(name: str) -> bool:
    """Identifier hardening (doc Section 10.7) before interpolating into DDL."""
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""))


def cast_like(df, config: dict):
    """Same typing rule as production: non-key double -> DECIMAL(38,18)."""
    out_cols = []
    ref_fields = {f.name: f.dataType for f in df.schema.fields}
    upsert_keys = [i.lower() for i in config['upsert_keys']]
    for name, dtype in ref_fields.items():
        if isinstance(dtype, DoubleType):
            if name in df.columns and name in upsert_keys:
                out_cols.append(F.col(name))
            else:
                out_cols.append(F.col(name).cast(DecimalType(38, 18)).alias(name))
        else:
            if name in df.columns:
                out_cols.append(F.col(name).cast(dtype).alias(name))
            else:
                out_cols.append(F.lit(None).cast(dtype).alias(name))
    return df.select(*out_cols)


def get_default_value(dtype):
    if isinstance(dtype, StringType):
        return ""
    if isinstance(dtype, (IntegerType, FloatType, DoubleType, LongType)):
        return 0
    if isinstance(dtype, DecimalType):
        return 0.0
    if isinstance(dtype, BooleanType):
        return False
    if isinstance(dtype, (DateType, TimestampType)):
        return None
    if isinstance(dtype, BinaryType):
        return b""
    return None


def _spark_to_redshift_type(data_type) -> str:
    if isinstance(data_type, StringType):
        return "VARCHAR(256)"
    if isinstance(data_type, IntegerType):
        return "INTEGER"
    if isinstance(data_type, LongType):
        return "BIGINT"
    if isinstance(data_type, FloatType):
        return "REAL"
    if isinstance(data_type, DoubleType):
        return "DOUBLE PRECISION"
    if isinstance(data_type, BooleanType):
        return "BOOLEAN"
    if isinstance(data_type, DateType):
        return "DATE"
    if isinstance(data_type, TimestampType):
        return "TIMESTAMP"
    if isinstance(data_type, DecimalType):
        return "DECIMAL(38,18)"
    if isinstance(data_type, BinaryType):
        return "VARBYTE"
    return "VARCHAR(256)"

# ===============================================================
# REDSHIFT DATA API  — still used for schema discovery + audit
# (DDL/SP introspection that the connector does not perform)
# ===============================================================


def _poll_statement(client, stmt_id: str, ctx: str, sleep_s: float = 0.5):
    while True:
        desc = client.describe_statement(Id=stmt_id)
        status = desc.get("Status")
        if status in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(sleep_s)
    if status != "FINISHED":
        raise RuntimeError(f"{ctx} failed. Status={status}, Error={desc.get('Error')}")
    return desc


def execute_sql(sql: str, redshift_conn: dict, client):
    resp = client.execute_statement(
        WorkgroupName=redshift_conn['workgroup_name'],
        Database=redshift_conn['database'],
        Sql=sql,
        SecretArn=redshift_conn['secret_arn'])
    return _poll_statement(client, resp["Id"], ctx="SQL")


def read_redshift_table_schema(config: dict, redshift_conn: dict, spark, client):
    sql = f"SELECT * FROM {redshift_conn['schema_name']}.{config['target_table']} WHERE 1=0;"
    resp = client.execute_statement(
        WorkgroupName=redshift_conn["workgroup_name"],
        Database=redshift_conn["database"],
        Sql=sql,
        SecretArn=redshift_conn["secret_arn"])
    _poll_statement(client, resp["Id"], ctx="Read target schema")
    metadata = client.get_statement_result(Id=resp["Id"])["ColumnMetadata"]

    def map_dtype(dtype: str):
        d = dtype.lower()
        if d in ("varchar", "char", "character varying"):
            return StringType()
        if d in ("int", "integer", "int4", "smallint", "int2"):
            return IntegerType()
        if d in ("bigint", "int8"):
            return LongType()
        if d in ("float", "float8", "double precision"):
            return DoubleType()
        if d in ("decimal", "numeric"):
            return DecimalType(38, 18)
        if d in ("boolean", "bool"):
            return BooleanType()
        if d in ("timestamp", "timestamp without time zone"):
            return TimestampType()
        if d == "date":
            return DateType()
        return StringType()

    schema = StructType([
        StructField(c["name"], map_dtype(c["typeName"]), True) for c in metadata])
    return spark.createDataFrame([], schema)


def check_table_exists(redshift_conn: dict, config: dict, client) -> bool:
    sql = dedent(f"""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = '{redshift_conn['schema_name']}'
          AND table_name   = '{config['target_table']}' LIMIT 1;
    """)
    resp = client.execute_statement(
        WorkgroupName=redshift_conn["workgroup_name"],
        Database=redshift_conn["database"],
        Sql=sql,
        SecretArn=redshift_conn["secret_arn"])
    _poll_statement(client, resp["Id"], ctx="Check table exists")
    return bool(client.get_statement_result(Id=resp["Id"]).get("Records"))


def get_metadata(config: dict, redshift_conn: dict, client) -> dict:
    sql = dedent(f"""
        SELECT column_name, data_type, character_maximum_length
        FROM SVV_COLUMNS
        WHERE table_schema = '{redshift_conn['schema_name']}'
          AND table_name = '{config['target_table']}';
    """)
    desc = execute_sql(sql, redshift_conn, client)
    rows = client.get_statement_result(Id=desc["Id"]).get("Records", [])
    meta = {}
    for r in rows:
        meta[r[0]['stringValue']] = {
            "dtype": r[1]['stringValue'].lower(),
            "length": (r[2].get('longValue') if r[2] else None),
        }
    return meta


def create_new_redshift_table(config: dict, redshift_conn: dict, df, client, log):
    log.info("Target table does not exist; creating")
    upsert_keys = set(config.get('upsert_keys', []))
    cols_ddls = []
    for field in df.schema.fields:
        not_null = " NOT NULL" if field.name in upsert_keys else ""
        cols_ddls.append(f"{field.name} {_spark_to_redshift_type(field.dataType)}{not_null}")
    ddl = dedent(f"""
        CREATE TABLE IF NOT EXISTS {redshift_conn['schema_name']}.{config['target_table']} (
            {', '.join(cols_ddls)}
        );
    """)
    if execute_sql(ddl, redshift_conn, client).get("Status") == "FINISHED":
        log.info(f"'{config['target_table']}' table created")


def get_row_count(config: dict, redshift_conn: dict, client) -> int:
    sql = f"SELECT COUNT(*) FROM {redshift_conn['schema_name']}.{config['target_table']};"
    resp = client.execute_statement(
        WorkgroupName=redshift_conn['workgroup_name'],
        Database=redshift_conn['database'],
        Sql=sql,
        SecretArn=redshift_conn['secret_arn'])
    _poll_statement(client, resp["Id"], ctx="Row count")
    records = client.get_statement_result(Id=resp["Id"]).get("Records", [])
    if not records:
        return 0
    cell = records[0][0]
    for k in ("longValue", "doubleValue", "stringValue"):
        if k in cell:
            return int(cell[k])
    raise ValueError(f"Unexpected count cell format: {cell}")


def update_job_sts_table(config, redshift_conn, run_start_ts, run_end_ts, source_filename,
                         records_read, records_updated, records_inserted,
                         status, error_message, client):
    run_id = str(int(time.time()))
    schema = redshift_conn['schema_name']
    safe_error = str(error_message).replace("'", "''")[:4096] if error_message else ''
    safe_filename = source_filename.replace("'", "''")
    sql = (
        f"CALL public.sp_log_job_status("
        f"'{schema}', '{run_id}', '{config['job_id']}', "
        f"'{run_start_ts}', '{run_end_ts}', "
        f"'{safe_filename}', '{config['target_table']}', "
        f"{records_read}, {records_updated}, {records_inserted}, "
        f"'{status}', '{safe_error}')")
    execute_sql(sql, redshift_conn, client)

# ===============================================================
# S3 ARCHIVE / CLEANUP  — ported from production
# ===============================================================


def _move_s3_file(config, target_file_path, log):
    s3 = boto3.client('s3')
    source = f"s3://{config['src_bucket']}/data/in/{config['source_file_name']}"
    sbkt, skey = source.replace("s3://", "").split("/", 1)
    tbkt, tkey = target_file_path.replace("s3://", "").split("/", 1)
    try:
        s3.copy_object(Bucket=tbkt, CopySource={'Bucket': sbkt, 'Key': skey}, Key=tkey)
        s3.delete_object(Bucket=sbkt, Key=skey)
        log.info(f"Moved {source} -> {target_file_path}")
    except Exception as e:
        log.error("Error while moving file", error=str(e))

# ===============================================================
# DYNAMICFRAME MODULES  (the variant-specific logic — doc Section 8)
# ===============================================================


def read_source(config: dict, glue_context, log):
    """Section 7.2 / 8: ingest via the Glue reader so BOOKMARKS track this source."""
    source = f"s3://{config['src_bucket']}/data/in/{config['source_file_name']}"
    log.info("Reading source as DynamicFrame", path=source)
    return glue_context.create_dynamic_frame.from_options(
        connection_type="s3",
        connection_options={"paths": [source]},
        format="csv",
        # Section 10.4 — real-world CSV robustness
        format_options={"withHeader": True, "quoteChar": '"', "escaper": '"'},
        transformation_ctx=f"read_{config['target_table']}",  # stable + unique => bookmark key
    )


def conform(dyf, config: dict, glue_context, log):
    """Section 8.1: resolve type ambiguity, normalise names, retype, add audit cols.

    `resolveChoice(choice="cast:string")` only touches ambiguous (ChoiceType) columns;
    cleanly-inferred columns keep their type. `cast_like` then applies the production
    DECIMAL-for-money rule. (Pure-DynamicFrame alternative: `dyf.apply_mapping([...])`.)
    """
    dyf = dyf.resolveChoice(choice="cast:string")
    df = dyf.toDF()
    for old in df.columns:
        new = _clean_colname(old)
        if old != new:
            df = df.withColumnRenamed(old, new)
    df = cast_like(df, config)
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    df = (df.withColumn("run_date", lit(run_ts).cast(TimestampType()))
            .withColumn("file_name", lit(config['source_file_name'])))
    return DynamicFrame.fromDF(df, glue_context, "conformed")


def quality_gate(dyf, config: dict, glue_context, log):
    """Section 7.6: capture malformed records instead of failing the whole job."""
    err_count = dyf.errorsCount()
    if err_count:
        log.warning("DynamicFrame contained malformed records", errors=err_count)
        bad = dyf.errorsAsDynamicFrame()
        glue_context.write_dynamic_frame.from_options(
            frame=bad, connection_type="s3",
            connection_options={
                "path": f"s3://{config['src_bucket']}/data/quarantine/{config['target_table']}/"},
            format="json", transformation_ctx="quarantine")
        threshold = config.get("error_threshold", 0)
        if err_count > threshold:
            raise RuntimeError(f"{err_count} malformed records exceeded threshold {threshold}")
    return dyf


@retry_on_exception(max_attempts=3, base_delay=5, max_delay=60, exceptions=(Exception,))
def reconcile_and_build_preactions(dyf, config, redshift_conn, spark, glue_context, client, log):
    """Section 8.2: additive schema evolution expressed as connector preactions.

    Returns (preactions_sql, aligned_dynamicframe, staging_table_name).
      * new source columns      -> ALTER TABLE target ADD COLUMN  (preaction)
      * VARCHAR too short        -> ALTER TABLE target ALTER COLUMN TYPE  (preaction)
      * target-only columns      -> back-filled in the frame
      * final frame aligned to the (now reconciled) target column order

    NOTE: integer promotion (SMALLINT->INT->BIGINT) and view drop/recreate are omitted
    here for clarity; port them from the production `alter_varchar_columns` / `drop_views`
    as additional preactions if your tables need them.
    """
    schema = redshift_conn['schema_name']
    target = config['target_table']
    staging = f"{target}_stg"

    df = dyf.toDF()
    redshift_df = read_redshift_table_schema(config, redshift_conn, spark, client)
    tgt_types = {f.name: f.dataType for f in redshift_df.schema.fields}
    src_types = {f.name: f.dataType for f in df.schema.fields}

    pre = []

    # 1) New columns in the source -> ADD COLUMN on the target
    new_cols = [n for n in src_types if n not in tgt_types]
    for n in new_cols:
        if not _valid_ident(n):
            raise ValueError(f"Unsafe column identifier rejected: {n!r}")
        pre.append(f"ALTER TABLE {schema}.{target} ADD COLUMN {n} {_spark_to_redshift_type(src_types[n])};")
    if new_cols:
        log.info("Schema evolution: adding columns", columns=new_cols)

    # 2) VARCHAR widening (single-pass max length aggregate -> one driver row)
    str_cols = [n for n, t in src_types.items() if isinstance(t, StringType) and n in tgt_types]
    if str_cols:
        meta = get_metadata(config, redshift_conn, client)
        row = df.agg(*[F.max(F.length(F.col(c))).alias(c) for c in str_cols]).collect()[0]
        widened = []
        for c in str_cols:
            src_len = int(row[c] or 0)
            cur_len = int((meta.get(c) or {}).get("length") or 0)
            if src_len > cur_len:
                new_len = min(src_len + 10, 65535)
                pre.append(f"ALTER TABLE {schema}.{target} ALTER COLUMN {c} TYPE VARCHAR({new_len});")
                widened.append({"column": c, "from": cur_len, "to": new_len})
        if widened:
            log.info("Schema evolution: widening VARCHAR", columns=widened)

    # 3) Back-fill target-only columns in the frame (keeps COPY column count aligned)
    backfilled = []
    for n, t in tgt_types.items():
        if n not in src_types:
            df = df.withColumn(n, F.lit(get_default_value(t)))
            backfilled.append(n)
    if backfilled:
        log.info("Back-filled target-only columns", columns=backfilled)

    # 4) Align to target column order (Redshift COPY is positional) — Section 4.5
    df = df.select(*[f.name for f in redshift_df.schema.fields])

    # 5) Staging lifecycle — created AFTER the target ALTERs so it inherits new columns
    pre.append(f"DROP TABLE IF EXISTS {schema}.{staging};")
    pre.append(f"CREATE TABLE {schema}.{staging} (LIKE {schema}.{target});")

    aligned = DynamicFrame.fromDF(df, glue_context, "aligned")
    return ("\n".join(pre), aligned, staging)


def load_and_merge(aligned, preactions, staging, config, redshift_conn, glue_context, log):
    """Section 8.2: one connector write replaces write+COPY+stage+merge.

    The connector stages `aligned` into `redshiftTmpDir`, runs `preactions`, COPYs into
    the staging table, then runs `postactions` (which reuse the existing merge SP and
    drop the staging table).
    """
    schema = redshift_conn['schema_name']
    target = config['target_table']
    keys_csv = ",".join(config['upsert_keys'])
    postactions = (
        f"CALL public.sp_merge_from_staging("
        f"'{schema}', '{target}', '{staging}', '{keys_csv}')")

    log.info("Loading via Redshift connector", staging=staging, postaction="sp_merge_from_staging")
    glue_context.write_dynamic_frame.from_options(
        frame=aligned,
        connection_type="redshift",
        connection_options={
            "redshiftTmpDir": redshift_conn["tmp_dir"],
            "useConnectionProperties": "true",
            "connectionName": redshift_conn["connection_name"],
            "dbtable": f"{schema}.{staging}",
            "preactions": preactions,
            "postactions": postactions,
            "aws_iam_role": redshift_conn["iam_role"],  # used by the COPY under the hood
        },
        transformation_ctx=f"load_{target}",
    )

# ===============================================================
# MAIN
# ===============================================================


def main():
    args = getResolvedOptions(
        sys.argv,
        [
            'job_id', 'job_name', 'source_file_name', 'target_table', 'upsert_keys',
            'workgroup_name', 'database', 'region', 'secret_arn', 'iam_role',
            'schema_name', 'src_bucket',
            # variant-specific args (vs the production Data API jobs):
            'connection_name',     # Glue Connection (JDBC) to Redshift
            'redshift_tmp_dir',    # S3 temp dir used by the connector for COPY
        ])

    config = {
        'job_id': args['job_id'],
        'job_name': args['job_name'],
        'source_file_name': args['source_file_name'],
        'target_table': args['target_table'],
        'upsert_keys': json.loads(args['upsert_keys']),
        'src_bucket': args['src_bucket'],
    }
    redshift_conn = {
        'workgroup_name': args['workgroup_name'],
        'database': args['database'],
        'region': args['region'],
        'secret_arn': args['secret_arn'],
        'iam_role': args['iam_role'],
        'schema_name': args['schema_name'],
        'connection_name': args['connection_name'],
        'tmp_dir': args['redshift_tmp_dir'],
    }

    run_id = uuid.uuid4().hex
    log = LogBuffer(run_id)
    spark, glue_context, job = initialize_glue(config['job_name'])
    client = boto3.client("redshift-data", region_name=redshift_conn['region'])

    run_start_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    now = datetime.now(timezone.utc)
    year, month = now.strftime('%Y'), now.strftime('%m')
    source_filename = config['source_file_name']
    stem = source_filename.split('.')[0]
    log_base_name = f"{stem}_log_{now.strftime('%Y%m%d%H%M%S')}.txt"
    s3_archive_path = f"s3://{config['src_bucket']}/data/archive/{year}/{month}/{stem}_{now.strftime('%Y%m%d%H%M%S')}.csv"
    s3_unprocessed_path = f"s3://{config['src_bucket']}/data/unprocessed/{year}/{month}/{source_filename}"

    records_read = 0
    try:
        # --- Ingest (bookmarked) -> conform -> quality gate ---
        dyf = read_source(config, glue_context, log)
        records_read = dyf.count()
        log.info("Records read", count=records_read)

        dyf = conform(dyf, config, glue_context, log)
        dyf = quality_gate(dyf, config, glue_context, log)

        # --- Create table if missing; capture row count for audit deltas ---
        if check_table_exists(redshift_conn, config, client):
            rows_before = get_row_count(config, redshift_conn, client)
        else:
            create_new_redshift_table(config, redshift_conn, dyf.toDF(), client, log)
            rows_before = 0

        # --- Schema evolution (preactions) + single connector load + merge ---
        preactions, aligned, staging = reconcile_and_build_preactions(
            dyf, config, redshift_conn, spark, glue_context, client, log)
        load_and_merge(aligned, preactions, staging, config, redshift_conn, glue_context, log)

        job.commit()  # persist bookmark state (this is meaningful here, unlike production)

        # --- Audit + archive + logs ---
        rows_after = get_row_count(config, redshift_conn, client)
        records_inserted = max(rows_after - rows_before, 0)
        records_updated = max(records_read - records_inserted, 0)
        run_end_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        update_job_sts_table(
            config, redshift_conn, run_start_ts, run_end_ts, source_filename,
            records_read, records_updated, records_inserted, "SUCCESS", "NULL", client)
        log.info("ETL Job Completed Successfully")

        _move_s3_file(config, s3_archive_path, log)
        log.export_to_s3(config['src_bucket'], f"logs/{now.strftime('%Y/%m/%d')}", log_base_name)

    except Exception as e:
        log.error("ETL FAILED", error=str(e))
        traceback.print_exc()
        try:
            run_end_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            update_job_sts_table(
                config, redshift_conn, run_start_ts, run_end_ts, source_filename,
                records_read, 0, 0, "FAILED", str(e), client)
        except Exception as audit_ex:
            log.error("Failed to record job status", error=str(audit_ex))
        finally:
            try:
                _move_s3_file(config, s3_unprocessed_path, log)
                log.export_to_s3(config['src_bucket'], f"logs/{now.strftime('%Y/%m/%d')}", log_base_name)
            except Exception:
                pass
        raise


if __name__ == '__main__':
    main()
