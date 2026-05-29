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
| Schema evolution | `ALTER`/back-fill/widen via Data API | Same logic, emitted as connector **`preactions`** |
| Load path | `coalesce(1).write` + create-staging + COPY + `run_merge` | **One** `write_dynamic_frame` (connector) with `preactions` + `postactions` |
| Merge | `CALL sp_merge_from_staging` | **Same SP**, run as a `postaction` |
| Redshift access | Redshift **Data API** (no VPC) | Data API for schema/audit **+** JDBC **Glue Connection** for the connector load |

The schema-evolution semantics are identical to production (additive only — see doc
Section 4): new source columns are added, target-only columns are back-filled, and
`VARCHAR` columns are widened. Integer promotion and view drop/recreate are intentionally
left as a documented extension point in `reconcile_and_build_preactions()`.

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
