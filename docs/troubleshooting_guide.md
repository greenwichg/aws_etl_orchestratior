# Troubleshooting Guide

This guide covers how to diagnose and resolve failures at every layer of the ETL pipeline — from file delivery through to Redshift load.

---

## Pipeline at a Glance

```
S3 → EventBridge → SQS → Lambda → Step Functions → Glue → Redshift
```

Each layer has its own logs, failure modes, and recovery steps.

---

## 1. Finding Logs

### Lambda (Orchestrator & SFTP)

```bash
# List recent log streams
aws logs describe-log-streams \
  --log-group-name /aws/lambda/etl-orchestrator-dev-orchestrator \
  --order-by LastEventTime --descending \
  --profile etl-dev

# Tail last 30 minutes of logs
aws logs filter-log-events \
  --log-group-name /aws/lambda/etl-orchestrator-dev-orchestrator \
  --start-time $(date -d '30 minutes ago' +%s000) \
  --profile etl-dev
```

### Step Functions Execution History

```bash
# List recent executions
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-east-1:{account-id}:stateMachine:etl-orchestrator-dev-etl-orchestrator \
  --profile etl-dev

# Get full execution history for a specific run
aws stepfunctions get-execution-history \
  --execution-arn arn:aws:states:us-east-1:{account-id}:execution:etl-orchestrator-dev-etl-orchestrator:{execution-name} \
  --profile etl-dev
```

### Glue Job Logs

```bash
# List recent Glue job runs
aws glue get-job-runs \
  --job-name etl-orchestrator-dev-etl-simple \
  --profile etl-dev

# Glue logs are in two log groups:
# /aws-glue/jobs/output   → stdout (info messages)
# /aws-glue/jobs/error    → stderr (errors and tracebacks)

aws logs filter-log-events \
  --log-group-name /aws-glue/jobs/error \
  --log-stream-name-prefix {job-run-id} \
  --profile etl-dev
```

### S3 Job Logs (Exported by Glue)

Glue exports structured JSON logs to S3 after each run:
```
s3://etl-orchestrator-dev-*/logs/YYYY/MM/DD/{source_filename}_log_{timestamp}.txt
```

```bash
aws s3 ls s3://{bucket}/logs/$(date +%Y/%m/%d)/ --profile etl-dev
aws s3 cp s3://{bucket}/logs/YYYY/MM/DD/{logfile}.txt - --profile etl-dev
```

### SQS Queue Inspection

```bash
# Check queue depth (messages waiting)
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/{account-id}/etl-orchestrator-dev-ingestion.fifo \
  --attribute-names ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible \
  --profile etl-dev

# Check dead-letter queue for failed messages
aws sqs receive-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/{account-id}/etl-orchestrator-dev-ingestion-dlq.fifo \
  --profile etl-dev
```

---

## 2. Common Failures & Fixes

### Lambda — File Not Triggering the Pipeline

**Symptom:** File lands in S3 but nothing happens — no Step Functions execution started.

| Possible Cause | How to Check | Fix |
|---------------|-------------|-----|
| File landed in wrong S3 prefix | Check `s3://bucket/data/in/` — file must be in this prefix | Move file to correct prefix |
| EventBridge rule not matching | Check EventBridge rule pattern in AWS Console → Rules | Verify rule matches the bucket and prefix |
| SQS queue is throttled or full | Check `ApproximateNumberOfMessages` on queue | Investigate DLQ for stuck messages |
| Lambda has an error | Check `/aws/lambda/.../orchestrator` log group | See Lambda error codes below |
| File not in `config.json` | Lambda logs show "not_configured" | Add file config to `config/config.json` |
| Job `is_active: false` | Lambda logs show "inactive" | Set `is_active: true` in `config.json` |

---

### Lambda — Error Codes

| Log Message | Cause | Fix |
|------------|-------|-----|
| `config.json must be a list` | Wrong format in config file | Ensure `config.json` is a JSON array `[{...}, {...}]` |
| `SNS publish failed` | SNS topic ARN wrong or IAM permission missing | Check `SNS_TOPIC_ARN` env var and Lambda role |
| `ExecutionAlreadyExists` | Same file triggered twice before first run finished | Expected behaviour — Lambda returns `already_running` |
| `AccessDenied` on S3 | Lambda role missing S3 permission | Check `lambda-orchestrator` role S3 policy |
| `NoSuchKey` on config | `config/config.json` does not exist in bucket | Upload config file to correct S3 path |

---

### Step Functions — Execution Failed

**Symptom:** Step Functions execution shows status `FAILED` or `TIMED_OUT`.

```bash
# Get the error detail
aws stepfunctions get-execution-history \
  --execution-arn {execution-arn} \
  --profile etl-dev \
  | jq '.events[] | select(.type == "ExecutionFailed" or .type == "TaskFailed")'
```

| Error | Cause | Fix |
|-------|-------|-----|
| `Glue.ConcurrentRunsExceededException` | Too many Glue jobs running simultaneously | Increase Glue concurrent run limit or reduce concurrency |
| `States.Timeout` | Glue job exceeded 120-minute timeout | Optimise job or increase `timeout` in `aws_glue_job` Terraform resource |
| `States.TaskFailed` | Glue job itself failed | Check Glue job logs — see Glue section below |
| `AccessDeniedException` on Glue | Step Functions role missing `glue:StartJobRun` | Check `sfn-orchestrator` IAM role |

**Re-run a failed execution:**

```bash
# Start a new execution with the same input as the failed one
INPUT=$(aws stepfunctions get-execution-history \
  --execution-arn {failed-execution-arn} \
  | jq -r '.events[0].executionStartedEventDetails.input')

aws stepfunctions start-execution \
  --state-machine-arn {state-machine-arn} \
  --name "rerun-$(date +%Y%m%d%H%M%S)" \
  --input "$INPUT" \
  --profile etl-dev
```

---

### Glue — Job Failed

**Symptom:** Glue job run shows status `FAILED` in AWS Console or via CLI.

```bash
# Get the failure reason
aws glue get-job-run \
  --job-name etl-orchestrator-dev-etl-simple \
  --run-id {job-run-id} \
  --profile etl-dev \
  | jq '.JobRun.ErrorMessage'
```

| Error Message | Cause | Fix |
|--------------|-------|-----|
| `CSV file not found at s3://...` | Source file missing from `data/in/` | Check file was not already archived or moved |
| `AccessDenied` on S3 | Glue role missing S3 permission | Check `glue-etl` IAM role |
| `Connection refused` on Redshift | Redshift workgroup not running | Check Redshift Serverless workgroup status |
| `Schema mismatch` | New source column type incompatible with Redshift column | Check `alter_redshift_table` logic or manually alter column |
| `UnicodeDecodeError` | Source file has non-UTF-8 encoding | Convert file to UTF-8 before delivering to S3 |
| `COPY failed: delimiter not found` | CSV has inconsistent delimiters | Validate CSV structure before delivery |
| `Worker ran out of memory` | Data volume too large for `G.1X` workers | Increase `glue_workers` in `terraform.tfvars` or upgrade to `G.2X` |
| `Job timed out` | Job exceeded 120-minute limit | Partition the source file or increase `timeout` in Terraform |

**Check unprocessed files (Glue moves failures here):**

```bash
aws s3 ls s3://{bucket}/data/unprocessed/ --recursive --profile etl-dev
```

**Manually re-run a Glue job:**

```bash
aws glue start-job-run \
  --job-name etl-orchestrator-dev-etl-simple \
  --arguments '{
    "--job_id": "JOB_001",
    "--job_name": "my_job",
    "--source_file_name": "customers.csv",
    "--target_table": "customers",
    "--upsert_keys": "[\"customer_id\"]"
  }' \
  --profile etl-dev
```

---

### Redshift — Load Errors

**Symptom:** Glue job completes but data is wrong or the COPY command fails.

```bash
# Query Redshift load errors via Data API
aws redshift-data execute-statement \
  --workgroup-name etl-dev-workgroup \
  --database etl_dev \
  --sql "SELECT * FROM stl_load_errors ORDER BY starttime DESC LIMIT 20;" \
  --profile etl-dev

# Get statement results
aws redshift-data get-statement-result \
  --id {statement-id} \
  --profile etl-dev
```

| Error | Cause | Fix |
|-------|-------|-----|
| `S3ServiceException: Access Denied` | Redshift role missing S3 permission | Check `redshift-s3-access` IAM role |
| `Invalid timestamp format` | Date column format doesn't match Redshift expectation | Add `TIMEFORMAT 'auto'` to COPY command |
| `String length exceeds DDL length` | VARCHAR column too short for value | `alter_varchar_columns` handles this automatically; check logs |
| `Numeric value out of range` | Integer overflow from source data | Review source data for outliers |
| Duplicate rows after UPSERT | `upsert_keys` misconfigured | Verify `upsert_keys` matches the primary key in `config.json` |

**Check JOB_STS audit table:**

```bash
aws redshift-data execute-statement \
  --workgroup-name etl-dev-workgroup \
  --database etl_dev \
  --sql "SELECT * FROM job_sts ORDER BY run_start_ts DESC LIMIT 10;" \
  --profile etl-dev
```

---

### SQS — Messages Stuck in DLQ

**Symptom:** Messages accumulate in the dead-letter queue.

```bash
# View DLQ messages
aws sqs receive-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/{account-id}/etl-orchestrator-dev-ingestion-dlq.fifo \
  --max-number-of-messages 10 \
  --profile etl-dev
```

**Re-drive DLQ messages back to main queue (after fixing the root cause):**

```bash
aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:us-east-1:{account-id}:etl-orchestrator-dev-ingestion-dlq.fifo \
  --destination-arn arn:aws:sqs:us-east-1:{account-id}:etl-orchestrator-dev-ingestion.fifo \
  --profile etl-dev
```

---

## 3. File State Reference

Glue moves files between S3 prefixes depending on the outcome. Use this to determine what happened to a file.

| S3 Prefix | Meaning |
|-----------|---------|
| `data/in/` | File waiting to be processed |
| `data/new_files/` | File arrived but not in `config.json` |
| `data/staging/{table}/` | Temporary Parquet written by Glue before COPY (cleaned up on success) |
| `data/archive/YYYY/MM/` | Successfully processed and archived |
| `data/unprocessed/YYYY/MM/` | Processing failed — file moved here for manual review |
| `logs/YYYY/MM/DD/` | Structured JSON log from each Glue run |

---

## 4. Useful CloudWatch Log Insights Queries

Open CloudWatch → Log Insights → select the relevant log group.

### Lambda — Find All Errors

```
fields @timestamp, @message
| filter @message like /ERROR/
| sort @timestamp desc
| limit 50
```

### Lambda — Track File Processing Outcomes

```
fields @timestamp, @message
| filter @message like /status/
| parse @message '"status": "*"' as status
| stats count() by status
```

### Glue — Find Failed Runs

```
fields @timestamp, @message
| filter level = "ERROR"
| sort @timestamp desc
| limit 50
```

### Glue — Record Count Summary

```
fields @timestamp, @message
| filter msg = "Records read"
| parse @message '"count": *,' as record_count
| stats avg(record_count), max(record_count), min(record_count)
```

### Step Functions — Execution Duration

```
fields @timestamp, @message
| filter @message like /ExecutionSucceeded/
| parse @message '"duration": *' as duration_ms
| stats avg(duration_ms), max(duration_ms) by bin(1h)
```

---

## 5. Emergency Runbook

### Pipeline completely stopped — nothing is processing

1. Check SQS queue depth — if 0, events are not arriving from EventBridge
2. Check EventBridge rule is enabled: AWS Console → EventBridge → Rules
3. Check Lambda is not throttled: CloudWatch → Lambda → Throttles metric
4. Check Lambda has no errors in the last hour: `/aws/lambda/...` log group
5. Check Step Functions for stuck running executions
6. Check Glue for stuck running jobs — if stuck, stop manually:
   ```bash
   aws glue batch-stop-job-run \
     --job-name etl-orchestrator-dev-etl-simple \
     --job-run-ids {run-id} \
     --profile etl-dev
   ```

### Data loaded but looks wrong

1. Check `JOB_STS` table in Redshift for `records_read`, `records_inserted`, `records_updated`
2. Check `stl_load_errors` in Redshift for COPY errors
3. Check Glue logs in S3 `logs/` prefix for the specific run
4. If UPSERT is wrong, verify `upsert_keys` in `config.json` matches the actual primary key

### Terraform state is locked

```bash
# Find the lock ID
aws dynamodb scan \
  --table-name etl-orchestrator-tflock-dev \
  --profile etl-dev

# Force-unlock only if the process that locked it is confirmed dead
terraform force-unlock {lock-id}
```
