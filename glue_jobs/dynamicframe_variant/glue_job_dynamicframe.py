"""
DynamicFrame variant of the SIMPLE ETL Glue job (REFERENCE IMPLEMENTATION).

This is a standalone, runnable reference that re-implements the production simple job
  glue_jobs/without_data_model/glue_job.py
using AWS Glue DynamicFrames, per the design in
  docs/pipeline/glue_schema_evolution_and_dynamicframes.md  (Sections 4, 7, 8, 9).

It is FULL PARITY with the simple (without_data_model) job — same schema-evolution
behaviour, same view handling, same staging/merge via stored procedures, same audit /
archive / failure routing — but built on DynamicFrames. It does NOT implement the star
schema (that lives in glue_jobs/with_data_model/...). The production scripts are NOT
modified by this file.

What differs from the production DataFrame job
----------------------------------------------
  1. Source is read with `create_dynamic_frame.from_options(... transformation_ctx=...)`
     so Glue JOB BOOKMARKS actually track processed data (production uses
     `spark.read.csv`, which does NOT participate in bookmarks — see doc Section 9).
  2. Type ambiguity is resolved with `resolveChoice`; malformed records are captured via
     `errorsAsDynamicFrame()` and quarantined instead of failing the whole job.
  3. The S3-write + COPY are handled by ONE `write_dynamic_frame` call to the Redshift
     connector; staging creation and the upsert reuse the EXISTING stored procedures
     (`sp_create_staging_table` as a preaction, `sp_merge_from_staging` as a postaction).

What is the SAME as production (ported faithfully)
--------------------------------------------------
  * Schema discovery via the Redshift Data API (SELECT ... WHERE 1=0).
  * Additive schema evolution: ADD COLUMN for new source columns, back-fill target-only
    columns, widen VARCHAR, and promote SMALLINT -> INTEGER -> BIGINT — each wrapped in
    view drop/recreate because Redshift views bind to their table.
  * Reporting view create/drop driven by config/config_view.json.
  * Audit logging (sp_log_job_status), archive on success, move-to-unprocessed on
    failure, structured JSON logs to CloudWatch + S3.

NOTE on a production bug this variant fixes
-------------------------------------------
  Production `alter_redshift_table` compares the Redshift schema against itself (it reads
  `source_df` from Redshift rather than using the incoming CSV), so genuinely new source
  columns are never added. This variant compares the SOURCE frame against the target and
  adds the missing columns, which is the clearly-intended behaviour.

Prerequisites (vs the Data API used by production)
--------------------------------------------------
  * A Glue Connection (JDBC) to Redshift, named via --connection_name, with the job in a
    VPC/subnet that can reach Redshift. (Production uses only the Data API — no VPC. This
    is the documented trade-off, doc Section 8.5 / 10.14.)
  * A writable S3 temp dir via --redshift_tmp_dir (the connector stages data there).
  * Job parameter `--job-bookmark-option job-bookmark-enable` to activate bookmarks.
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
from pyspark.sql.functions import lit, count
from pyspark.sql.types import (
    StructType, StructField, StringType,
    IntegerType, FloatType, DoubleType, LongType, DecimalType,
    BooleanType, TimestampType, DateType, BinaryType,
    ShortType, ByteType, ArrayType, MapType
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
# DYNAMICFRAME INGEST + CONFORM + QUALITY  (variant-specific)
# ===============================================================


def read_source(config: dict, glue_context, log):
    """Section 7.2 / 8: ingest via the Glue reader so BOOKMARKS track this source.

    Replaces production `read_csv_file`'s `spark.read.csv` (which does not participate in
    job bookmarks). `transformation_ctx` is stable + unique => it is the bookmark key.
    """
    source = f"s3://{config['src_bucket']}/data/in/{config['source_file_name']}"
    log.info("Reading source as DynamicFrame", path=source)
    return glue_context.create_dynamic_frame.from_options(
        connection_type="s3",
        connection_options={"paths": [source]},
        format="csv",
        # Section 10.4 — real-world CSV robustness
        format_options={"withHeader": True, "quoteChar": '"', "escaper": '"'},
        transformation_ctx=f"read_{config['target_table']}",
    )


def quality_gate(dyf, config: dict, glue_context, log):
    """Section 7.6: capture malformed records instead of failing the whole job.

    Must run on the raw DynamicFrame (where ChoiceType / error records still exist),
    before `toDF()` in `conform_to_df`.
    """
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


def conform_to_df(dyf, config: dict, log):
    """Section 8.1: resolve type ambiguity, normalise names, retype, add audit cols.

    Returns a Spark DataFrame so the schema-reconciliation steps below can reuse the
    production logic verbatim. `resolveChoice(choice="cast:string")` only touches
    ambiguous (ChoiceType) columns; cleanly-inferred columns keep their type.
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
    return df

# ===============================================================
# REDSHIFT DATA API UTILITIES  — identical to production
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

# ===============================================================
# SCHEMA DISCOVERY & HARMONIZATION  — ported from production
# ===============================================================


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


def create_new_redshift_table(config: dict, redshift_conn: dict, df, client, log):
    log.info("Target table does not exist; creating")
    upsert_keys = set(config.get('upsert_keys', []))
    cols_ddls = []
    for field in df.schema.fields:
        if not _valid_ident(field.name):
            raise ValueError(f"Unsafe column identifier rejected: {field.name!r}")
        not_null = " NOT NULL" if field.name in upsert_keys else ""
        cols_ddls.append(f"{field.name} {_spark_to_redshift_type(field.dataType)}{not_null}")
    ddl = dedent(f"""
        CREATE TABLE IF NOT EXISTS {redshift_conn['schema_name']}.{config['target_table']} (
            {', '.join(cols_ddls)}
        );
    """)
    if execute_sql(ddl, redshift_conn, client).get("Status") == "FINISHED":
        log.info(f"'{config['target_table']}' table successfully created")
        create_views(config, redshift_conn, client, log)


@retry_on_exception(max_attempts=3, base_delay=5, max_delay=60, exceptions=(Exception,))
def alter_redshift_table(config: dict, redshift_conn: dict, df, redshift_df, client, log):
    """Add columns present in the SOURCE frame but missing from the target.

    NOTE: production compares the Redshift schema against itself (a bug that silently
    drops genuinely new source columns). This variant compares the source `df` against
    the target `redshift_df`, which is the intended additive-evolution behaviour.
    """
    target_cols = [c.name for c in redshift_df.schema.fields]
    missed_cols = [f.name for f in df.schema.fields if f.name not in target_cols]
    if not missed_cols:
        return
    # views bind to the table — drop before ALTER, recreate after
    drop_views(config, redshift_conn, client, log)
    for colf in df.schema.fields:
        if colf.name in target_cols:
            continue
        if not _valid_ident(colf.name):
            raise ValueError(f"Unsafe column identifier rejected: {colf.name!r}")
        rtype = _spark_to_redshift_type(colf.dataType)
        sql = (f"ALTER TABLE {redshift_conn['schema_name']}.{config['target_table']} "
               f"ADD COLUMN {colf.name} {rtype};")
        execute_sql(sql, redshift_conn, client)
    log.info(f"columns: {missed_cols} are added successfully")
    desc = create_views(config, redshift_conn, client, log)
    if desc and desc.get('Status') == 'FINISHED':
        log.info("view is refreshed successfully")


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


@retry_on_exception(max_attempts=3, base_delay=5, max_delay=120, exceptions=(Exception,))
def alter_varchar_columns(config: dict, redshift_conn: dict, df, client, log):
    """Widen VARCHAR to fit, and promote SMALLINT -> INTEGER -> BIGINT. Ported verbatim
    from production (with view drop/recreate around each change)."""
    metadata = get_metadata(config, redshift_conn, client)
    INT_RANGES = {
        "smallint": 32767, "int2": 32767,
        "integer": 2147483647, "int": 2147483647, "int4": 2147483647,
        "bigint": 9223372036854775807, "int8": 9223372036854775807,
    }

    string_cols, int_cols = [], []
    for f in df.schema.fields:
        if isinstance(f.dataType, StringType):
            string_cols.append(f.name)
        elif isinstance(f.dataType, (IntegerType, LongType, ShortType)):
            int_cols.append(f.name)

    if not string_cols and not int_cols:
        return

    agg_expr = [F.max(F.length(F.col(c))).alias(c) for c in string_cols]
    agg_expr += [F.max(F.abs(F.col(c))).alias(c) for c in int_cols]
    row = df.agg(*agg_expr).collect()[0]

    # ---- VARCHAR widening ----
    str_altered_cols = []
    for colname in string_cols:
        src_len = int(row[colname] or 0)
        curr_len = int(metadata.get(colname, {}).get("length") or 0)
        if src_len > curr_len:
            drop_views(config, redshift_conn, client, log)
            new_len = min(src_len + 10, 65535)
            sql = (f"ALTER TABLE {redshift_conn['schema_name']}.{config['target_table']} "
                   f"ALTER COLUMN {colname} TYPE VARCHAR({new_len});")
            execute_sql(sql, redshift_conn, client)
            str_altered_cols.append({"column_name": colname, "source_length": src_len,
                                     "current_length": curr_len, "new_length": new_len})
    if str_altered_cols:
        log.info(f"columns: {str_altered_cols} are altered with new length")
        desc = create_views(config, redshift_conn, client, log)
        if desc and desc.get('Status') == 'FINISHED':
            log.info("view is refreshed successfully")

    # ---- INTEGER widening (add -> update -> drop -> rename) ----
    int_altered_cols = []
    for colname in int_cols:
        max_val = int(row[colname] or 0)
        curr_dtype = metadata.get(colname, {}).get("dtype")
        if not curr_dtype or curr_dtype not in INT_RANGES:
            continue
        if max_val > INT_RANGES[curr_dtype]:
            if curr_dtype in ("smallint", "int2"):
                new_type = "INTEGER"
            elif curr_dtype in ("integer", "int", "int4"):
                new_type = "BIGINT"
            else:
                continue  # already BIGINT
            drop_views(config, redshift_conn, client, log)
            schema, tbl = redshift_conn['schema_name'], config['target_table']
            add_sql = f"ALTER TABLE {schema}.{tbl} ADD COLUMN sample_col {new_type};"
            set_sql = f"UPDATE {schema}.{tbl} SET sample_col = {colname}::{new_type};"
            drop_sql = f"ALTER TABLE {schema}.{tbl} DROP COLUMN {colname};"
            rename_sql = f"ALTER TABLE {schema}.{tbl} RENAME COLUMN sample_col TO {colname};"
            desc = execute_sql(add_sql, redshift_conn, client)
            if desc['Status'] == 'FINISHED':
                desc = execute_sql(set_sql, redshift_conn, client)
                if desc['Status'] == 'FINISHED':
                    desc = execute_sql(drop_sql, redshift_conn, client)
                    if desc['Status'] == 'FINISHED':
                        execute_sql(rename_sql, redshift_conn, client)
            int_altered_cols.append({"column_name": colname, "current_datatype": curr_dtype,
                                     "new_datatype": new_type})
    if int_altered_cols:
        log.info(f"columns: {int_altered_cols} are altered with new datatype")
        desc = create_views(config, redshift_conn, client, log)
        if desc and desc.get('Status') == 'FINISHED':
            log.info("view is refreshed successfully")


def fill_missing_columns(df, redshift_df, log):
    """Back-fill columns the target has but the source lacks (keeps COPY aligned)."""
    src_cols = set(df.columns)
    missed_cols = []
    for colf in redshift_df.schema.fields:
        if colf.name not in src_cols:
            missed_cols.append(colf.name)
            df = df.withColumn(colf.name, lit(get_default_value(colf.dataType)))
    if missed_cols:
        log.info(f"columns: {missed_cols} filled with null values")
    return df


def check_datatype_matching(redshift_df, df, log):
    """Reject lossy non-numeric -> numeric loads. Parity with production (call site is
    commented out below, matching production)."""
    log.info("checking for datatype mismatch between source file and redshift table")
    redshift_cols = {field.name: field.dataType for field in redshift_df.schema.fields}
    non_numeric_types = (StringType, BooleanType, BinaryType, DateType, TimestampType,
                         ArrayType, MapType, StructType)
    numeric_types = (ByteType, ShortType, IntegerType, LongType, FloatType, DoubleType, DecimalType)
    for field in df.schema.fields:
        if field.name in redshift_cols:
            src_type = type(field.dataType)
            tgt_type = type(redshift_cols[field.name])
            non_null_count = df.select(count(field.name)).collect()[0][0]
            if issubclass(src_type, non_numeric_types) and issubclass(tgt_type, numeric_types) and non_null_count != 0:
                raise Exception(
                    f"Datatype mismatch for column '{field.name}': "
                    f"source={src_type.__name__}, target={tgt_type.__name__}")

# ===============================================================
# VIEW CONFIG + CREATE/DROP  — ported from production
# ===============================================================


def _load_view_config(config: dict, log):
    bucket = config['src_bucket']
    if not bucket:
        log.error("source bucket is not defined")
        raise ValueError("source bucket not found")
    s3 = boto3.client('s3')
    try:
        response = s3.get_object(Bucket=bucket, Key="config/config_view.json")
        data = json.loads(response['Body'].read().decode('utf-8'))
    except Exception as e:
        log.error(f"Failed to download or parse config file from S3: {e}")
        raise
    v_config = None
    for d in data:
        if d['source_table'] == config['target_table']:
            v_config = {
                'source_table': d['source_table'], 'view_name': d['view_name'],
                'schema_name': d['schema_name'], 'definition': d['definition']}
    return v_config


def create_views(config: dict, redshift_conn: dict, client, log):
    v_config = _load_view_config(config, log)
    if not v_config:
        log.info("No configuration found for the target table")
        return None
    ddl = v_config['definition'].format(
        schema_name=v_config['schema_name'], view_name=v_config['view_name'],
        source_table=v_config['source_table'])
    try:
        resp = client.execute_statement(
            WorkgroupName=redshift_conn['workgroup_name'], Database=redshift_conn['database'],
            Sql=ddl, SecretArn=redshift_conn['secret_arn'])
        return _poll_statement(client, resp["Id"], ctx="Create view")
    except Exception as e:
        log.error(f"Failed to create view '{v_config['view_name']}' with error: {e}")
        raise


def drop_views(config: dict, redshift_conn: dict, client, log):
    v_config = _load_view_config(config, log)
    if not v_config:
        log.warning("No view configuration found for the target table — nothing to drop")
        return None
    ddl = f"DROP VIEW IF EXISTS {v_config['schema_name']}.{v_config['view_name']}"
    try:
        resp = client.execute_statement(
            WorkgroupName=redshift_conn['workgroup_name'], Database=redshift_conn['database'],
            Sql=ddl, SecretArn=redshift_conn['secret_arn'])
        return _poll_statement(client, resp["Id"], ctx="Drop view")
    except Exception as e:
        log.error(f"Failed to drop view '{v_config['view_name']}' with error: {e}")
        raise

# ===============================================================
# AUDIT + ROW COUNT  — ported from production
# ===============================================================


def get_row_count(config: dict, redshift_conn: dict, client) -> int:
    sql = f"SELECT COUNT(*) FROM {redshift_conn['schema_name']}.{config['target_table']};"
    resp = client.execute_statement(
        WorkgroupName=redshift_conn['workgroup_name'], Database=redshift_conn['database'],
        Sql=sql, SecretArn=redshift_conn['secret_arn'])
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
# S3 HELPERS (archive & cleanup)  — ported from production
# ===============================================================


def move_s3_file_to_archive(config: dict, target_file_path: str, log):
    s3_client = boto3.client('s3')
    source_file_path = f"s3://{config['src_bucket']}/data/in/{config['source_file_name']}"
    source_bucket, source_key = source_file_path.replace("s3://", "").split("/", 1)
    target_bucket, target_key = target_file_path.replace("s3://", "").split("/", 1)
    try:
        s3_client.copy_object(Bucket=target_bucket,
                              CopySource={'Bucket': source_bucket, 'Key': source_key},
                              Key=target_key)
        log.info(f"File copied from {source_file_path} to {target_file_path}")
        s3_client.delete_object(Bucket=source_bucket, Key=source_key)
        log.info(f"Original file {source_file_path} deleted.")
    except Exception as e:
        log.error("Error while moving file", error=str(e))


def delete_staging_s3_files(s3_staging_path: str, log):
    s3_client = boto3.client('s3')
    bucket_name, prefix = s3_staging_path.replace("s3://", "").split("/", 1)
    try:
        objects = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
        for obj in objects.get("Contents", []):
            s3_client.delete_object(Bucket=bucket_name, Key=obj["Key"])
            log.info(f"Deleted staging file: {obj['Key']}")
    except Exception as e:
        log.error("Error deleting staging files", error=str(e))


def move_s3_file_to_unprocessed(config: dict, target_file_path: str, log):
    s3_client = boto3.client('s3')
    source_file_path = f"s3://{config['src_bucket']}/data/in/{config['source_file_name']}"
    source_bucket, source_key = source_file_path.replace("s3://", "").split("/", 1)
    target_bucket, target_key = target_file_path.replace("s3://", "").split("/", 1)
    try:
        s3_client.copy_object(Bucket=target_bucket,
                              CopySource={'Bucket': source_bucket, 'Key': source_key},
                              Key=target_key)
        log.info(f"File copied from {source_file_path} to {target_file_path}")
        s3_client.delete_object(Bucket=source_bucket, Key=source_key)
        log.info(f"Original file {source_file_path} deleted.")
    except Exception as e:
        log.error("Error while moving file", error=str(e))

# ===============================================================
# LOAD + MERGE via the Redshift connector  (variant-specific)
# ===============================================================


@retry_on_exception(max_attempts=3, base_delay=5, max_delay=120, exceptions=(Exception,))
def load_and_merge(aligned, config, redshift_conn, glue_context, log):
    """Section 8: one connector write replaces production's
    write(CSV) + sp_create_staging_table + sp_copy_from_s3 + sp_merge_from_staging.

    Schema evolution has ALREADY run (via the Data API) before this call, so the staging
    table created in preactions inherits the evolved target schema. The connector stages
    `aligned` to redshiftTmpDir and COPYs into staging; postactions run the existing merge
    SP, which also drops the staging table.
    """
    schema = redshift_conn['schema_name']
    target = config['target_table']
    staging = f"{target}_stg_{uuid.uuid4().hex[:8]}"
    keys_csv = ",".join(config['upsert_keys'])

    preactions = f"CALL public.sp_create_staging_table('{schema}', '{staging}', '{target}')"
    postactions = (f"CALL public.sp_merge_from_staging("
                   f"'{schema}', '{target}', '{staging}', '{keys_csv}')")

    log.info("Loading via Redshift connector", staging=staging)
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
            "aws_iam_role": redshift_conn["iam_role"],
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
    quarantine_prefix = f"s3://{config['src_bucket']}/data/quarantine/{config['target_table']}/"

    records_read = 0
    try:
        # --- Ingest (bookmarked) -> quality gate -> conform ---
        dyf = read_source(config, glue_context, log)
        dyf = quality_gate(dyf, config, glue_context, log)   # operate on raw frame
        df = conform_to_df(dyf, config, log)                  # -> Spark DataFrame

        records_read = df.count()
        log.info("Records read", count=records_read)

        # --- Schema reconciliation (Data API), identical semantics to production ---
        if check_table_exists(redshift_conn, config, client):
            log.info("Target table exists")
            redshift_df = read_redshift_table_schema(config, redshift_conn, spark, client)
            rows_before = get_row_count(config, redshift_conn, client)

            # check_datatype_matching(redshift_df, df, log)   # parity: optional guard

            if set(df.columns) != set(redshift_df.columns):
                log.info("Reconciling new/missing columns")
                df = fill_missing_columns(df, redshift_df, log)          # back-fill target-only
                alter_redshift_table(config, redshift_conn, df, redshift_df, client, log)  # ADD new
        else:
            create_new_redshift_table(config, redshift_conn, df, client, log)
            rows_before = 0

        # widen VARCHAR / promote integers (must precede frame alignment)
        alter_varchar_columns(config, redshift_conn, df, client, log)

        # re-read post-evolution schema and align column order (COPY is positional)
        redshift_df = read_redshift_table_schema(config, redshift_conn, spark, client)
        df = df.select(*[c.name for c in redshift_df.schema.fields])

        # --- Load + upsert via the connector (staging + COPY + merge in one call) ---
        aligned = DynamicFrame.fromDF(df, glue_context, "aligned")
        load_and_merge(aligned, config, redshift_conn, glue_context, log)

        job.commit()  # persist bookmark state (meaningful here, unlike production)

        # --- Audit + archive + logs ---
        rows_after = get_row_count(config, redshift_conn, client)
        records_inserted = max(rows_after - rows_before, 0)
        records_updated = max(records_read - records_inserted, 0)
        run_end_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        update_job_sts_table(
            config, redshift_conn, run_start_ts, run_end_ts, source_filename,
            records_read, records_updated, records_inserted, "SUCCESS", "NULL", client)
        log.info("ETL Job Completed Successfully")

        move_s3_file_to_archive(config, s3_archive_path, log)
        log.export_to_s3(config['src_bucket'], f"logs/{now.strftime('%Y/%m/%d')}", log_base_name)

    except Exception as e:
        log.error("ETL FAILED", error=str(e))
        traceback.print_exc()
        try:
            run_end_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            fail_records_read = records_read if 'records_read' in dir() else 0
            update_job_sts_table(
                config, redshift_conn, run_start_ts, run_end_ts, source_filename,
                fail_records_read, 0, 0, "FAILED", str(e), client)
        except Exception as audit_ex:
            log.error("Failed to record job status", error=str(audit_ex))
        finally:
            try:
                move_s3_file_to_unprocessed(config, s3_unprocessed_path, log)
                log.export_to_s3(config['src_bucket'], f"logs/{now.strftime('%Y/%m/%d')}", log_base_name)
            except Exception:
                pass
        raise


if __name__ == '__main__':
    main()
