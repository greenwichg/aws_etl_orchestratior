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
| `redshift-s3-access` | `redshift.amazonaws.com` | Allows Redshift Serverless to read from S3 for COPY operations |

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

### 5. Redshift S3 Access Role
**Name:** `{project_name}-{environment}-redshift-s3-access`  
**Trust Principal:** `redshift.amazonaws.com`  
**Defined in:** `terraform/modules/redshift/main.tf`

| Service | Actions | Resource Scope |
|---------|---------|---------------|
| S3 | `GetObject`, `GetBucketLocation`, `ListBucket` | Entire ETL S3 bucket |

**Why:** Redshift Serverless needs this role to execute COPY commands — it pulls data directly from S3 into Redshift tables. Glue passes this role ARN as the `--iam_role` job parameter so Redshift can authenticate the read request against S3.

---

### 6. CloudWatch Monitoring (Cross-cutting)

CloudWatch permissions are embedded within each service role rather than a separate role. Here is the full picture across the architecture:

| Service | CloudWatch Actions | Resource Scope |
|---------|-------------------|---------------|
| Lambda (Orchestrator) | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | `/aws/lambda/{orchestrator-fn}:*` |
| Lambda (SFTP) | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | `/aws/lambda/{sftp-fn}:*` |
| Glue ETL | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | `/aws-glue/*` |
| Step Functions | `CreateLogDelivery`, `GetLogDelivery`, `UpdateLogDelivery`, `DeleteLogDelivery`, `ListLogDeliveries`, `PutResourcePolicy`, `DescribeResourcePolicies`, `DescribeLogGroups` | `*` |
| Lambda / Glue (metrics) | `PutMetricData` | `AWS/Lambda`, `Glue` namespaces |
| CloudWatch Alarms | `PutMetricAlarm`, `DescribeAlarms` | Alarm ARNs for job failure alerting |

**Why Step Functions needs extra log actions:** The `.sync` integration pattern requires state machine execution logging via CloudWatch Log Delivery (a separate delivery-management API distinct from standard `PutLogEvents`).

---

## Service Connection Matrix

| From | To | Mechanism | Permission Required |
|------|----|-----------|-------------------|
| S3 | EventBridge | S3 EventBridge notification | S3 bucket notification config (`eventbridge: true`) |
| EventBridge | SQS | Resource-based policy on SQS | `sqs:SendMessage` with `aws:SourceArn` condition on EventBridge rule |
| SQS | Lambda (Orchestrator) | Event source mapping | `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` in Lambda role |
| S3 | Lambda (SFTP) | `aws_lambda_permission` | S3 service principal allowed to invoke Lambda on `logs/*` prefix events |
| Lambda (Orchestrator) | S3 | Read config & data | `s3:GetObject` on `config/*`, `data/*` |
| Lambda (Orchestrator) | SNS | Alert on file arrival | `sns:Publish` |
| Lambda (Orchestrator) | Step Functions | Start execution | `states:StartExecution` |
| Step Functions | Glue | Run ETL jobs | `glue:StartJobRun`, `glue:GetJobRun` |
| Glue | S3 | Read source, write output | `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` |
| Glue | Redshift (Data API) | COPY / UPSERT / Validate | `redshift-data:ExecuteStatement`, `DescribeStatement`, `GetStatementResult` |
| Glue | Secrets Manager | Fetch Redshift credentials | `secretsmanager:GetSecretValue` |
| Redshift | S3 | COPY command reads source data | `s3:GetObject`, `s3:GetBucketLocation`, `s3:ListBucket` via `redshift-s3-access` role |
| Lambda (SFTP) | Secrets Manager | Fetch SFTP credentials | `secretsmanager:GetSecretValue` |

---

## IAM Policy JSON — Field Reference

Every IAM policy document is built from the same set of fields. Understanding each one lets you read, write, and critique any policy on the spot.

---

### Top-Level Fields

| Field | Required | What It Means |
|-------|----------|--------------|
| `Version` | Yes | The policy language version. Always use `"2012-10-17"` — the older `"2008-10-17"` version lacks support for policy variables like `${aws:username}`. |
| `Statement` | Yes | An array of one or more individual permission rules. Each object in this array is evaluated independently. |

---

### Statement-Level Fields

| Field | Required | What It Means |
|-------|----------|--------------|
| `Sid` | No | **Statement ID** — a human-readable label for the statement. Has no effect on evaluation; used for auditing, documentation, and referencing statements in error messages. Example: `"S3ReadConfig"`. |
| `Effect` | Yes | Either `"Allow"` or `"Deny"`. AWS defaults to **implicit deny** — if no statement allows an action, it is denied. An explicit `"Deny"` overrides any `"Allow"`, even from another policy. |
| `Principal` | Conditionally | **Who** this statement applies to. Required in trust policies and resource-based policies; omitted in identity-based policies (where the principal is whoever the policy is attached to). Can be a service (`"lambda.amazonaws.com"`), an AWS account, an IAM role/user ARN, or `"*"` (anyone). |
| `Action` | Yes | **What** API operations are being allowed or denied. Format is `"service:OperationName"` (e.g. `"s3:GetObject"`). Can be a single string or an array. Wildcards are supported: `"s3:*"` means all S3 actions. |
| `Resource` | Yes (in most policies) | **What** AWS resources the actions apply to. Specified as ARNs. Use `"*"` only when a specific ARN cannot be used (e.g. list operations). Wildcards within ARNs are supported: `"arn:aws:s3:::my-bucket/*"`. |
| `Condition` | No | **When** the statement applies. Adds extra constraints using condition keys. Only takes effect if all conditions evaluate to true. See examples below. |

---

### Effect Values Explained

```
AWS Default: everything is IMPLICITLY DENIED unless a policy explicitly allows it.

Allow  →  grants the action if no Deny overrides it
Deny   →  always wins, overrides any Allow from any policy (explicit deny)
```

| Scenario | Result |
|----------|--------|
| No matching statement | Implicit deny — access blocked |
| Statement with `Effect: Allow` matches | Access granted |
| Statement with `Effect: Deny` matches | Access blocked, even if another policy allows it |
| Both Allow and Deny match | Deny always wins |

---

### Principal Values Explained

| Value | Meaning |
|-------|---------|
| `"Service": "lambda.amazonaws.com"` | AWS Lambda service can assume this role |
| `"Service": "states.amazonaws.com"` | AWS Step Functions service can assume this role |
| `"AWS": "arn:aws:iam::123456789012:root"` | An AWS account (all principals in that account) |
| `"AWS": "arn:aws:iam::123456789012:role/my-role"` | A specific IAM role |
| `"*"` | Anyone — use only when intentional (e.g. public S3 bucket) |

---

### Action Naming Pattern

```
"service-prefix:OperationName"

Examples:
  s3:GetObject           → S3 GetObject API
  states:StartExecution  → Step Functions StartExecution API
  redshift-data:ExecuteStatement → Redshift Data API ExecuteStatement
  logs:PutLogEvents      → CloudWatch Logs PutLogEvents API
```

Wildcards:
- `"s3:*"` — all S3 actions
- `"s3:Get*"` — all S3 Get actions (GetObject, GetBucketPolicy, etc.)

---

### Condition Block Explained

```json
"Condition": {
  "ConditionOperator": {
    "ConditionKey": "ConditionValue"
  }
}
```

| Component | What It Is | Example |
|-----------|-----------|---------|
| **Condition operator** | How to compare key to value | `StringEquals`, `ArnLike`, `ArnEquals`, `IpAddress`, `Bool` |
| **Condition key** | The attribute being tested | `aws:SourceArn`, `aws:SourceAccount`, `aws:RequestedRegion` |
| **Condition value** | The expected value | A specific ARN, account ID, region string |

Common patterns in this architecture:

```json
// Restrict EventBridge → SQS to a specific rule (prevents other rules writing to the queue)
"Condition": {
  "ArnEquals": { "aws:SourceArn": "arn:aws:events:...:rule/my-rule" }
}

// Restrict S3 → Lambda invocation to a specific bucket
"Condition": {
  "ArnLike": { "aws:SourceArn": "arn:aws:s3:::my-bucket" }
}

// Restrict CloudWatch PutMetricData to specific namespaces
"Condition": {
  "StringEquals": { "cloudwatch:namespace": ["AWS/Lambda", "Glue"] }
}
```

> **ArnEquals vs ArnLike:** `ArnEquals` requires an exact match. `ArnLike` supports wildcards (`*`, `?`) within the ARN — useful when the full ARN isn't known at policy creation time (e.g. any object in a bucket).

---

### Two Types of IAM Policies

| Type | Where It Lives | Who It Applies To | Used For |
|------|---------------|------------------|---------|
| **Identity-based policy** | Attached to a role/user/group | Whoever holds the role | Granting permissions to a service or user |
| **Resource-based policy** | Attached to the resource itself | Anyone who calls the resource | Cross-account access, service-to-service (e.g. EventBridge → SQS, S3 → Lambda) |

> In this architecture: Lambda roles, Glue role, Step Functions role, and Redshift role are **identity-based**. The EventBridge → SQS permission and S3 → Lambda invocation are **resource-based policies**.

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

### 5. Redshift S3 Access — Trust Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "redshift.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

### 5. Redshift S3 Access — Permission Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3CopyAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetBucketLocation",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::{bucket-name}",
        "arn:aws:s3:::{bucket-name}/*"
      ]
    }
  ]
}
```

> **Note:** This role ARN is attached directly to the Redshift Serverless namespace (`iam_roles` attribute) and is also passed to Glue jobs as the `--iam_role` parameter so Glue can instruct Redshift to perform COPY operations against S3.

---

### 6. EventBridge → SQS Resource-Based Policy

EventBridge does not assume an IAM role to send messages to SQS. Instead, a **resource-based policy** is attached directly to the SQS queue granting EventBridge permission.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowEventBridge",
      "Effect": "Allow",
      "Principal": {
        "Service": "events.amazonaws.com"
      },
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:{region}:{account-id}:{queue-name}",
      "Condition": {
        "ArnEquals": {
          "aws:SourceArn": "arn:aws:events:{region}:{account-id}:rule/{rule-name}"
        }
      }
    }
  ]
}
```

> **Note:** The `aws:SourceArn` condition restricts which specific EventBridge rule can send messages — preventing other rules or services from writing to this queue.

---

### 7. S3 → Lambda SFTP Invocation Permission

S3 triggers Lambda via a **resource-based policy** on the Lambda function (not an IAM role). This is managed by `aws_lambda_permission` in Terraform.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowS3Invoke",
      "Effect": "Allow",
      "Principal": {
        "Service": "s3.amazonaws.com"
      },
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:{region}:{account-id}:function:{sftp-function-name}",
      "Condition": {
        "ArnLike": {
          "aws:SourceArn": "arn:aws:s3:::{bucket-name}"
        }
      }
    }
  ]
}
```

> **Note:** This permission fires on `s3:ObjectCreated:*` events under the `logs/` prefix, triggering the SFTP Lambda to replicate log files to the shared drive.

---

### 8. CloudWatch Metrics & Alarms (Additional Permissions)

These permissions extend the CloudWatch Logs actions already embedded in the Lambda and Glue roles:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudWatchMetrics",
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": ["AWS/Lambda", "Glue"]
        }
      }
    },
    {
      "Sid": "CloudWatchAlarms",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:DescribeAlarms",
        "cloudwatch:DeleteAlarms"
      ],
      "Resource": "arn:aws:cloudwatch:{region}:{account-id}:alarm:{project-name}-{environment}-*"
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
| **Condition keys** | `aws:SourceArn` restricts which specific EventBridge rule can write to SQS — prevents other rules hijacking the queue. |
| **`.sync` Step Functions pattern** | Requires `glue:GetJobRun` in addition to `glue:StartJobRun` — a detail many candidates miss. |
| **Circular dependency** | Lambda needs the Step Functions ARN to scope its policy, but Step Functions depends on Lambda being created first. Common Terraform/CDK interview topic. |
| **Managed vs inline policy** | `AWSGlueServiceRole` is AWS-managed (maintained by AWS); the rest are inline/customer-managed for tighter control. |
| **IAM role vs resource-based policy** | EventBridge → SQS and S3 → Lambda use *resource-based policies*, not IAM roles. Services that don't "assume" a role use this pattern instead. |
| **Redshift COPY role** | Redshift needs an IAM role attached to the cluster/namespace (not just passed at runtime) to read from S3 — a common gap in architecture reviews. |
| **CloudWatch Logs vs Metrics namespaces** | `logs:*` actions control log writing; `cloudwatch:PutMetricData` is a separate namespace for custom metrics. Many candidates confuse the two. |

---

## Notes

- All roles follow least-privilege: permissions are scoped to specific ARNs or prefixes where possible.
- The `step_function_arn` passed to the Lambda Orchestrator role is currently set to `"*"` in Terraform to avoid a circular dependency between the IAM and Step Functions modules. Consider scoping this once the state machine ARN is known.
- The Redshift Data API resource is `"*"` — consider scoping to the specific Redshift cluster ARN for tighter control.
- Naming convention: `{project_name}-{environment}-{role-purpose}` ensures roles are environment-isolated and identifiable.
