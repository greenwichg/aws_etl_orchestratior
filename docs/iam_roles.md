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

## IAM Policy JSON Definitions

Full policy documents as they would appear in AWS — useful for direct deployment via AWS Console, CloudFormation, or boto3.

---

### 1. Lambda Orchestrator — Trust Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

### 1. Lambda Orchestrator — Permission Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadConfig",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::{bucket-name}",
        "arn:aws:s3:::{bucket-name}/config/*",
        "arn:aws:s3:::{bucket-name}/data/*"
      ]
    },
    {
      "Sid": "S3WriteData",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::{bucket-name}/data/*"
    },
    {
      "Sid": "SNSPublish",
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:{region}:{account-id}:{topic-name}"
    },
    {
      "Sid": "StepFunctionsStart",
      "Effect": "Allow",
      "Action": "states:StartExecution",
      "Resource": "arn:aws:states:{region}:{account-id}:stateMachine:{state-machine-name}"
    },
    {
      "Sid": "SQSConsume",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:{region}:{account-id}:{queue-name}"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:{region}:{account-id}:log-group:/aws/lambda/{function-name}:*"
    }
  ]
}
```

---

### 2. Lambda SFTP — Trust Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

### 2. Lambda SFTP — Permission Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadLogs",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::{bucket-name}",
        "arn:aws:s3:::{bucket-name}/logs/*"
      ]
    },
    {
      "Sid": "SecretsManagerSFTP",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:{region}:{account-id}:secret:{sftp-secret-name}-*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:{region}:{account-id}:log-group:/aws/lambda/{sftp-function-name}:*"
    }
  ]
}
```

---

### 3. Glue ETL — Trust Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "glue.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

### 3. Glue ETL — Permission Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3FullAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::{bucket-name}",
        "arn:aws:s3:::{bucket-name}/*"
      ]
    },
    {
      "Sid": "RedshiftDataAPI",
      "Effect": "Allow",
      "Action": [
        "redshift-data:ExecuteStatement",
        "redshift-data:DescribeStatement",
        "redshift-data:GetStatementResult"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecretsManagerRedshift",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:{region}:{account-id}:secret:{redshift-secret-name}-*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:{region}:{account-id}:log-group:/aws-glue/*"
    }
  ]
}
```

> **Note:** The AWS managed policy `arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole` is also attached to this role in addition to the inline policy above.

---

### 4. Step Functions Orchestrator — Trust Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "states.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

### 4. Step Functions Orchestrator — Permission Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GlueJobControl",
      "Effect": "Allow",
      "Action": [
        "glue:StartJobRun",
        "glue:GetJobRun"
      ],
      "Resource": "arn:aws:glue:{region}:{account-id}:job/{project-name}-{environment}-*"
    },
    {
      "Sid": "CloudWatchLogsDelivery",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## Interview Tips

These policies reflect common AWS interview patterns worth understanding deeply:

| Topic | What Interviewers Test |
|-------|----------------------|
| **Trust vs Permission policy** | Trust policy controls *who can assume* the role (`sts:AssumeRole`); permission policy controls *what the role can do*. Both are required. |
| **Resource ARN scoping** | Always prefer specific ARNs over `"*"`. Wildcard on `Resource` is a red flag in security reviews. |
| **`Sid` field** | Optional but recommended — makes policies readable and easier to audit. |
| **Condition keys** | Not used here but powerful: e.g. `aws:SourceArn` to restrict which EventBridge rule can invoke Lambda. |
| **`.sync` Step Functions pattern** | Requires `glue:GetJobRun` in addition to `glue:StartJobRun` — a detail many candidates miss. |
| **Circular dependency** | Lambda needs the Step Functions ARN to scope its policy, but Step Functions depends on Lambda being created first. Common Terraform/CDK interview topic. |
| **Managed vs inline policy** | `AWSGlueServiceRole` is AWS-managed (maintained by AWS); the rest are inline/customer-managed for tighter control. |

---

## Notes

- All roles follow least-privilege: permissions are scoped to specific ARNs or prefixes where possible.
- The `step_function_arn` passed to the Lambda Orchestrator role is currently set to `"*"` in Terraform to avoid a circular dependency between the IAM and Step Functions modules. Consider scoping this once the state machine ARN is known.
- The Redshift Data API resource is `"*"` — consider scoping to the specific Redshift cluster ARN for tighter control.
- Naming convention: `{project_name}-{environment}-{role-purpose}` ensures roles are environment-isolated and identifiable.
