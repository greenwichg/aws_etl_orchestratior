# DynamicFrame variant (reference implementation)

`glue_job_dynamicframe.py` is a **standalone, non-production reference** that rebuilds
the ETL on AWS Glue **DynamicFrames**, implementing the design in
[`docs/pipeline/glue_schema_evolution_and_dynamicframes.md`](../../docs/pipeline/glue_schema_evolution_and_dynamicframes.md)
(Section 8).

> The production scripts are **not** modified by this folder:
> - `glue_jobs/without_data_model/glue_job.py`
> - `glue_jobs/with_data_model/glue_job_with_data_model.py`

## How it differs from the production (DataFrame) jobs

| Concern | Production (`glue_job.py`) | This variant (`glue_job_dynamicframe.py`) |
|---------|----------------------------|-------------------------------------------|
| Source read | `spark.read.csv` (bookmarks **inert**) | `create_dynamic_frame.from_options(..., transformation_ctx=...)` → **bookmarks active** |
| Type ambiguity | n/a (fixed schema) | `resolveChoice` resolves `choice` columns |
| Typing rule | `cast_like` (non-key `double → DECIMAL(38,18)`) | **Same** `cast_like` rule, reused |
| Bad records | exception | `errorsAsDynamicFrame()` → quarantined to `data/quarantine/` |
| Schema evolution | `ALTER`/back-fill/widen/promote via Data API | **Same logic, same Data API**, run up-front (before the load) |
| Load path | `coalesce(1).write` + create-staging + COPY + `run_merge` | **One** `write_dynamic_frame` (connector) with `preactions` + `postactions` |
| Staging create | `CALL sp_create_staging_table` | **Same SP**, run as a `preaction` |
| Merge | `CALL sp_merge_from_staging` | **Same SP**, run as a `postaction` |
| Redshift access | Redshift **Data API** (no VPC) | Data API for schema/audit **+** JDBC **Glue Connection** for the connector load |

The schema-evolution semantics are **full parity** with the simple production job (additive
only — see doc Section 4): new source columns are added (`ALTER TABLE ADD COLUMN`),
target-only columns are back-filled, `VARCHAR` columns are widened, and integers are
promoted `SMALLINT → INTEGER → BIGINT` — each wrapped in view drop/recreate because
Redshift views bind to their table. Schema evolution runs via the Redshift Data API
**before** the connector load, because integer promotion reorders columns and `COPY` is
positional (the frame is aligned to the post-evolution schema first).

**Bug fixed vs production:** production `alter_redshift_table` compares the Redshift schema
against itself, so genuinely new source columns are never added (silently dropped). This
variant compares the source frame against the target and adds the missing columns — the
intended behaviour. The production scripts are left as-is.

**Scope:** this mirrors the simple (`without_data_model`) job only. The star-schema
dimensional model (`with_data_model`) is not implemented here.

## Prerequisites (beyond the production jobs)

1. **Glue Connection (JDBC) to Redshift** — passed via `--connection_name`. The job must
   run in a VPC/subnet that can reach the Redshift endpoint. (Production uses the Data
   API, which needs no VPC — this is the documented trade-off, doc §8.5 / §10.14.)
2. **S3 temp dir** for the connector's COPY staging — passed via `--redshift_tmp_dir`.
3. **Bookmarks enabled** — set the job parameter `--job-bookmark-option job-bookmark-enable`.
4. The existing stored procedures must be installed (`sp_merge_from_staging`,
   `sp_log_job_status`) — see `stored_procedures/`.

## Job parameters

Same as the production jobs, plus two:

```
--job_id            --job_name           --source_file_name   --target_table
--upsert_keys       --workgroup_name     --database           --region
--secret_arn        --iam_role           --schema_name        --src_bucket
--connection_name        # Glue JDBC Connection to Redshift   (NEW)
--redshift_tmp_dir       # s3://.../tmp/ used by the connector (NEW)
--job-bookmark-option job-bookmark-enable   # to activate bookmarks
```

## Status

Reference/illustrative. It has not been wired into Terraform or the Step Functions
routing (`config.json` `glue_job` values remain `simple` / `data_model`). Treat it as a
worked example of the DynamicFrame approach, not a deployed job.
