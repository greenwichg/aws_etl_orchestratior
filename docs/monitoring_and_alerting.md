# Monitoring & Alerting

This document covers what to monitor, how to set up CloudWatch alarms, and the Log Insights queries most useful for operating this ETL pipeline day-to-day.

---

## What to Monitor — Service Summary

| Service | Key Metric | Healthy Threshold | Alert On |
|---------|-----------|------------------|---------|
| Lambda (Orchestrator) | `Errors` | 0 | Any error |
| Lambda (Orchestrator) | `Duration` | < 30s | > 60s |
| Lambda (Orchestrator) | `Throttles` | 0 | Any throttle |
| SQS Ingestion Queue | `ApproximateNumberOfMessagesVisible` | < 10 | > 50 (backlog building) |
| SQS DLQ | `ApproximateNumberOfMessagesVisible` | 0 | Any message (processing failure) |
| Step Functions | `ExecutionsFailed` | 0 | Any failure |
| Step Functions | `ExecutionTime` | < 60 min | > 90 min |
| Glue (Simple ETL) | `glue.driver.aggregate.numFailedTasks` | 0 | Any failure |
| Glue (Data Model ETL) | `glue.driver.aggregate.numFailedTasks` | 0 | Any failure |
| Redshift Serverless | `ComputeCapacity` | < 80% of base RPU | > 80% sustained |

---

## CloudWatch Alarms

### 1. Lambda Error Alarm

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "etl-dev-lambda-orchestrator-errors" \
  --alarm-description "Lambda orchestrator has errors" \
  --namespace AWS/Lambda \
  --metric-name Errors \
  --dimensions Name=FunctionName,Value=etl-orchestrator-dev-orchestrator \
  --statistic Sum \
  --period 60 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-east-1:{account-id}:{sns-topic-name} \
  --profile etl-dev
```

### 2. SQS Dead-Letter Queue Alarm

Trigger immediately when any message lands in the DLQ — every DLQ message means a processing failure.

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "etl-dev-sqs-dlq-not-empty" \
  --alarm-description "Messages in DLQ — processing failures detected" \
  --namespace AWS/SQS \
  --metric-name ApproximateNumberOfMessagesVisible \
  --dimensions Name=QueueName,Value=etl-orchestrator-dev-ingestion-dlq.fifo \
  --statistic Maximum \
  --period 60 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-east-1:{account-id}:{sns-topic-name} \
  --profile etl-dev
```

### 3. SQS Queue Backlog Alarm

Detects when files are piling up faster than Lambda can process them.

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "etl-dev-sqs-backlog" \
  --alarm-description "SQS ingestion queue backlog is growing" \
  --namespace AWS/SQS \
  --metric-name ApproximateNumberOfMessagesVisible \
  --dimensions Name=QueueName,Value=etl-orchestrator-dev-ingestion.fifo \
  --statistic Maximum \
  --period 300 \
  --threshold 50 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 2 \
  --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-east-1:{account-id}:{sns-topic-name} \
  --profile etl-dev
```

### 4. Step Functions Execution Failure Alarm

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "etl-dev-sfn-executions-failed" \
  --alarm-description "Step Functions ETL orchestrator execution failed" \
  --namespace AWS/States \
  --metric-name ExecutionsFailed \
  --dimensions Name=StateMachineArn,Value=arn:aws:states:us-east-1:{account-id}:stateMachine:etl-orchestrator-dev-etl-orchestrator \
  --statistic Sum \
  --period 60 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-east-1:{account-id}:{sns-topic-name} \
  --profile etl-dev
```

### 5. Step Functions Execution Duration Alarm

Flags executions that are taking too long (Glue job hanging or very large files).

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "etl-dev-sfn-execution-too-slow" \
  --alarm-description "Step Functions execution taking longer than 90 minutes" \
  --namespace AWS/States \
  --metric-name ExecutionTime \
  --dimensions Name=StateMachineArn,Value=arn:aws:states:us-east-1:{account-id}:stateMachine:etl-orchestrator-dev-etl-orchestrator \
  --statistic Maximum \
  --period 300 \
  --threshold 5400000 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-east-1:{account-id}:{sns-topic-name} \
  --profile etl-dev
```

> **Note:** `ExecutionTime` is in milliseconds. 5,400,000 ms = 90 minutes.

### 6. Lambda Throttle Alarm

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "etl-dev-lambda-throttles" \
  --alarm-description "Lambda orchestrator is being throttled" \
  --namespace AWS/Lambda \
  --metric-name Throttles \
  --dimensions Name=FunctionName,Value=etl-orchestrator-dev-orchestrator \
  --statistic Sum \
  --period 60 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-east-1:{account-id}:{sns-topic-name} \
  --profile etl-dev
```

---

## CloudWatch Dashboard

Create a single dashboard that gives a live view of the whole pipeline.

```bash
aws cloudwatch put-dashboard \
  --dashboard-name "ETL-Orchestrator-Dev" \
  --dashboard-body '{
    "widgets": [
      {
        "type": "metric",
        "properties": {
          "title": "Lambda Errors & Throttles",
          "metrics": [
            ["AWS/Lambda", "Errors", "FunctionName", "etl-orchestrator-dev-orchestrator"],
            ["AWS/Lambda", "Throttles", "FunctionName", "etl-orchestrator-dev-orchestrator"]
          ],
          "period": 60,
          "stat": "Sum",
          "view": "timeSeries"
        }
      },
      {
        "type": "metric",
        "properties": {
          "title": "SQS Queue Depth",
          "metrics": [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "etl-orchestrator-dev-ingestion.fifo"],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "etl-orchestrator-dev-ingestion-dlq.fifo"]
          ],
          "period": 60,
          "stat": "Maximum",
          "view": "timeSeries"
        }
      },
      {
        "type": "metric",
        "properties": {
          "title": "Step Functions Executions",
          "metrics": [
            ["AWS/States", "ExecutionsStarted", "StateMachineArn", "arn:aws:states:us-east-1:{account-id}:stateMachine:etl-orchestrator-dev-etl-orchestrator"],
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", "arn:aws:states:us-east-1:{account-id}:stateMachine:etl-orchestrator-dev-etl-orchestrator"],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", "arn:aws:states:us-east-1:{account-id}:stateMachine:etl-orchestrator-dev-etl-orchestrator"]
          ],
          "period": 300,
          "stat": "Sum",
          "view": "timeSeries"
        }
      },
      {
        "type": "metric",
        "properties": {
          "title": "Lambda Duration (ms)",
          "metrics": [
            ["AWS/Lambda", "Duration", "FunctionName", "etl-orchestrator-dev-orchestrator", {"stat": "Average"}],
            ["AWS/Lambda", "Duration", "FunctionName", "etl-orchestrator-dev-orchestrator", {"stat": "Maximum"}]
          ],
          "period": 300,
          "view": "timeSeries"
        }
      }
    ]
  }' \
  --profile etl-dev
```

---

## CloudWatch Log Insights Queries

Open: CloudWatch → Log Insights → select log group → paste query → run.

### Lambda — All Processing Outcomes (Last 24h)

**Log group:** `/aws/lambda/etl-orchestrator-dev-orchestrator`

```
fields @timestamp, @message
| filter @message like /status/
| parse @message '"status": "*"' as outcome
| stats count() as total by outcome
| sort total desc
```

### Lambda — Files Not in Config (Last 7 Days)

```
fields @timestamp, @message
| filter @message like /not_configured/
| parse @message '"moved_to": "*"' as moved_to
| sort @timestamp desc
```

### Lambda — Average Duration Per Day

```
fields @timestamp, @duration
| stats avg(@duration) as avg_ms, max(@duration) as max_ms by datefloor(@timestamp, 1d)
| sort @timestamp desc
```

### Glue — Failed Jobs With Error Details

**Log group:** `/aws-glue/jobs/error`

```
fields @timestamp, @message
| filter level = "ERROR"
| sort @timestamp desc
| limit 100
```

### Glue — Record Count Over Time

**Log group:** `/aws-glue/jobs/output`

```
fields @timestamp, @message
| filter msg = "Records read"
| parse @message '"count": *}' as record_count
| stats avg(record_count) as avg_records, max(record_count) as max_records by datefloor(@timestamp, 1d)
```

### Glue — ETL Job Duration (Minutes)

```
fields @timestamp, @message
| filter msg = "ETL Job Completed Successfully"
| stats count() as successful_runs by datefloor(@timestamp, 1d)
```

### Step Functions — Execution Log Summary

**Log group:** `/aws/states/etl-orchestrator-dev-etl-orchestrator`

```
fields @timestamp, type, @message
| filter type in ["ExecutionSucceeded", "ExecutionFailed", "ExecutionTimedOut"]
| stats count() by type
```

---

## SNS Alerting

The pipeline already creates an SNS topic and subscribes the `notification_email` from `terraform.tfvars`. Alerts are sent for:

| Event | Triggered By | SNS Subject |
|-------|-------------|------------|
| New file not in config | Lambda | `New / Not Configured File` |
| Inactive job file arrived | Lambda | `Inactive Job` |
| CloudWatch alarms (above) | CloudWatch → SNS | Alarm name as subject |

### Verify SNS Subscription

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:{account-id}:etl-orchestrator-dev-notifications \
  --profile etl-dev
```

### Test SNS Manually

```bash
aws sns publish \
  --topic-arn arn:aws:sns:us-east-1:{account-id}:etl-orchestrator-dev-notifications \
  --subject "Test Alert" \
  --message "This is a test notification from the ETL pipeline." \
  --profile etl-dev
```

---

## Metric Dimensions Reference

| Service | Namespace | Key Dimension |
|---------|----------|--------------|
| Lambda | `AWS/Lambda` | `FunctionName` |
| SQS | `AWS/SQS` | `QueueName` |
| Step Functions | `AWS/States` | `StateMachineArn` |
| Glue | `Glue` | `JobName`, `JobRunId` |
| Redshift Serverless | `AWS/Redshift-Serverless` | `WorkgroupName` |
| CloudWatch Logs | `AWS/Logs` | `LogGroupName` |

---

## Interview Tips

| Topic | What Interviewers Test |
|-------|----------------------|
| **Metric vs Log** | Metrics are numeric time-series (count, duration, bytes) — cheap to query and graph. Logs are raw text events — richer but more expensive to query at scale. Alarms are built on metrics, not logs. |
| **Period vs Evaluation Periods** | `period` is the length of each data point window (e.g. 60s). `evaluation-periods` is how many consecutive windows must breach the threshold before the alarm fires — reduces noise from brief spikes. |
| **DLQ pattern** | A DLQ catches messages that fail processing after the configured retry count. It's a safety net — not a queue to ignore. Any message in the DLQ means a bug or upstream issue needs investigation. |
| **`treat-missing-data`** | `notBreaching` (good for infrequent events like DLQ) vs `breaching` (good for heartbeat metrics where missing data is itself a problem). |
| **Log Insights cost** | Charged per GB of log data scanned. Narrow queries with time ranges and `filter` before `stats` to reduce cost. |
| **Composite alarms** | You can combine multiple alarms with AND/OR logic using `aws cloudwatch put-composite-alarm` — e.g. alert only when BOTH Lambda errors AND SQS backlog are high. |
