# IAM Roles & Permissions

This document describes the IAM roles required to connect all services in the ETL orchestration architecture.

---

## Architecture Service Map

```
S3 → EventBridge → Lambda (Orchestrator) → Step Functions → Glue → Redshift
                        ↓                                              ↑
                       SNS                                      Secrets Manager
                        ↓
                   Lambda (SFTP) → Shared Drive
```

---

## Roles Overview

| Role | Assumed By | Purpose |
|------|-----------|---------|
| `lambda-orchestrator` | `lambda.amazonaws.com` | Validates files, triggers Step Functions, sends SNS alerts, reads SQS |
| `lambda-sftp` | `lambda.amazonaws.com` | Replicates logs to shared drive via SFTP |
| `glue-etl` | `glue.amazonaws.com` | Reads/writes S3, executes Redshift COPY/UPSERT |
| `sfn-orchestrator` | `states.amazonaws.com` | Starts and monitors Glue job runs |

---

## Role Details

### 1. Lambda Orchestrator Role
**Name:** `{project_name}-{environment}-lambda-orchestrator`  
**Trust Principal:** `lambda.amazonaws.com`

| Service | Actions | Resource Scope |
|---------|---------|---------------|
| S3 | `GetObject`, `ListBucket` | `config/*`, `data/*` prefixes |
| S3 | `PutObject`, `DeleteObject` | `data/*` prefix |
| SNS | `Publish` | Specified SNS topic ARN |
| Step Functions | `StartExecution` | Specified state machine ARN |
| SQS | `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes` | Specified SQS queue ARN |
| CloudWatch Logs | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | Lambda log group |

**Why:** This Lambda is the entry point — it reads the config from S3, validates the incoming file, notifies via SNS, and hands off orchestration to Step Functions. SQS acts as the event source trigger.

---

### 2. Lambda SFTP Role
**Name:** `{project_name}-{environment}-lambda-sftp`  
**Trust Principal:** `lambda.amazonaws.com`

| Service | Actions | Resource Scope |
|---------|---------|---------------|
| S3 | `GetObject`, `ListBucket` | `logs/*` prefix |
| Secrets Manager | `GetSecretValue`, `DescribeSecret` | `{sftp_secret_name}-*` |
| CloudWatch Logs | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | Lambda log group |

**Why:** This Lambda reads processed log files from S3 and copies them to the shared drive over SFTP. Secrets Manager stores the SFTP credentials securely.

---

### 3. Glue ETL Role
**Name:** `{project_name}-{environment}-glue-etl`  
**Trust Principal:** `glue.amazonaws.com`

| Service | Actions | Resource Scope |
|---------|---------|---------------|
| S3 | `GetObject`, `PutObject`, `DeleteObject`, `ListBucket` | Entire ETL S3 bucket |
| Redshift Data API | `ExecuteStatement`, `DescribeStatement`, `GetStatementResult` | `*` |
| Secrets Manager | `GetSecretValue`, `DescribeSecret` | Redshift credentials secret ARN |
| CloudWatch Logs | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | `/aws-glue/*` log groups |
| AWS Managed Policy | `AWSGlueServiceRole` | — |

**Why:** Glue needs broad S3 access to read source files, write transformed output, and archive processed files. It uses the Redshift Data API (with credentials from Secrets Manager) to run COPY, UPSERT, and validation statements.

---

### 4. Step Functions Orchestrator Role
**Name:** `{project_name}-{environment}-sfn-orchestrator`  
**Trust Principal:** `states.amazonaws.com`

| Service | Actions | Resource Scope |
|---------|---------|---------------|
| Glue | `StartJobRun`, `GetJobRun` | Jobs matching `{project_name}-{environment}-*` |
| CloudWatch Logs | `CreateLogDelivery`, `GetLogDelivery`, `UpdateLogDelivery`, `DeleteLogDelivery`, `ListLogDeliveries`, `PutResourcePolicy`, `DescribeResourcePolicies`, `DescribeLogGroups` | `*` |

**Why:** Step Functions must be able to start Glue jobs and poll their status (`.sync` integration pattern). CloudWatch Logs permissions are required for state machine execution logging.

---

## Service Connection Matrix

| From | To | Mechanism | Permission Required |
|------|----|-----------|-------------------|
| EventBridge | Lambda (Orchestrator) | Invoke | EventBridge resource-based policy on Lambda |
| Lambda (Orchestrator) | S3 | Read config & data | `s3:GetObject` on `config/*`, `data/*` |
| Lambda (Orchestrator) | SNS | Alert on file arrival | `sns:Publish` |
| Lambda (Orchestrator) | SQS | Consume trigger events | `sqs:ReceiveMessage`, `sqs:DeleteMessage` |
| Lambda (Orchestrator) | Step Functions | Start execution | `states:StartExecution` |
| Step Functions | Glue | Run ETL jobs | `glue:StartJobRun`, `glue:GetJobRun` |
| Glue | S3 | Read source, write output | `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` |
| Glue | Redshift (Data API) | COPY / UPSERT / Validate | `redshift-data:ExecuteStatement` etc. |
| Glue | Secrets Manager | Fetch Redshift credentials | `secretsmanager:GetSecretValue` |
| Lambda (SFTP) | S3 | Read log files | `s3:GetObject` on `logs/*` |
| Lambda (SFTP) | Secrets Manager | Fetch SFTP credentials | `secretsmanager:GetSecretValue` |

---

## Notes

- All roles follow least-privilege: permissions are scoped to specific ARNs or prefixes where possible.
- The `step_function_arn` passed to the Lambda Orchestrator role is currently set to `"*"` in Terraform to avoid a circular dependency between the IAM and Step Functions modules. Consider scoping this once the state machine ARN is known.
- The Redshift Data API resource is `"*"` — consider scoping to the specific Redshift cluster ARN for tighter control.
- Naming convention: `{project_name}-{environment}-{role-purpose}` ensures roles are environment-isolated and identifiable.
