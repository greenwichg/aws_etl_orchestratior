# Cost Estimation

This document provides a per-service cost breakdown for running the ETL orchestration pipeline. Figures are based on AWS `us-east-1` pricing and representative workload assumptions.

---

## Workload Assumptions

| Parameter | Value | Basis |
|-----------|-------|-------|
| Files processed per day | 20 | Typical daily batch |
| Average file size | 50 MB | Mid-size CSV |
| Average Glue job duration | 15 minutes | Single `G.1X` worker |
| Glue workers per job | 2 (dev), 5 (prod) | From `terraform.tfvars` |
| Redshift Serverless base capacity | 8 RPU | Minimum, scales automatically |
| Active query hours per day | 4 hours | Glue jobs querying Redshift |
| Lambda average duration | 2 seconds | Config load + Step Functions trigger |
| Lambda memory | 128 MB | Default |
| S3 data stored | 100 GB | Source + archive + staging + logs |

---

## Per-Service Cost Breakdown

### 1. AWS Lambda

**Pricing:** $0.20 per 1M requests + $0.0000166667 per GB-second

| Calculation | Dev (monthly) | Prod (monthly) |
|-------------|--------------|----------------|
| Invocations: 20 files/day × 30 days = 600 | $0.00 (free tier: 1M/month) | $0.00 |
| Duration: 600 × 2s × 0.125 GB = 150 GB-s | $0.00 (free tier: 400K GB-s) | $0.00 |
| **Lambda total** | **~$0.00** | **~$0.00** |

> Lambda is effectively free at this volume. Cost only becomes meaningful at millions of invocations.

---

### 2. AWS Glue

**Pricing:** $0.44 per DPU-hour (`G.1X` worker = 1 DPU)

| Calculation | Dev (monthly) | Prod (monthly) |
|-------------|--------------|----------------|
| Workers | 2 | 5 |
| Job duration | 15 min = 0.25 hr | 15 min = 0.25 hr |
| Jobs per day × days | 20 × 30 = 600 | 20 × 30 = 600 |
| DPU-hours = workers × duration × jobs | 2 × 0.25 × 600 = 300 | 5 × 0.25 × 600 = 750 |
| Cost | 300 × $0.44 = **$132** | 750 × $0.44 = **$330** |

> **Glue is the primary cost driver.** Optimising job duration or reducing worker count has the most impact on the overall bill.

**Cost levers:**
- Fewer workers → lower cost, slower processing
- Shorter job duration → optimise Spark transformations, partition input files
- Use `G.2X` workers only for jobs that need more memory (higher per-DPU cost but fewer workers needed)

---

### 3. Amazon Redshift Serverless

**Pricing:** $0.375 per RPU-hour (scales automatically from 8 RPU base)

| Calculation | Dev (monthly) | Prod (monthly) |
|-------------|--------------|----------------|
| Active hours (Glue querying) | 4 hr/day × 30 = 120 hr | 4 hr/day × 30 = 120 hr |
| RPU during active use | 8 (base) | 16 (scales up under load) |
| Idle hours | 20 hr/day × 30 = 600 hr | 600 hr |
| RPU-hours (active) | 8 × 120 = 960 | 16 × 120 = 1,920 |
| RPU-hours (idle — paused) | 0 (pauses when idle) | 0 |
| Cost | 960 × $0.375 = **$360** | 1,920 × $0.375 = **$720** |

> Redshift Serverless automatically pauses after inactivity and resumes on first query. No charge while paused.

---

### 4. Amazon S3

**Pricing:** $0.023 per GB/month (Standard) + $0.005 per 1K PUT requests + $0.0004 per 1K GET requests

| Component | Size | Monthly Cost |
|-----------|------|-------------|
| Source files (data/in/) — transient | ~30 GB average | $0.69 |
| Archive (data/archive/) — grows monthly | 100 GB baseline | $2.30 |
| Logs (logs/) | 5 GB | $0.12 |
| Scripts (Glue scripts) | < 1 MB | $0.00 |
| Staging (data/staging/) — transient | ~5 GB peak | $0.12 |
| Terraform state (tfstate bucket) | < 1 MB | $0.00 |
| PUT requests: 20 files/day × 30 = 600 | — | $0.00 |
| **S3 total** | | **~$3.23** |

---

### 5. Amazon SQS

**Pricing:** $0.40 per 1M requests (first 1M free each month)

| Calculation | Monthly |
|-------------|---------|
| Messages: 20 files/day × 30 = 600 | Free (< 1M) |
| **SQS total** | **$0.00** |

---

### 6. Amazon SNS

**Pricing:** $0.50 per 1M publishes + $2.00 per 100K email deliveries

| Calculation | Monthly |
|-------------|---------|
| Publishes: ~600 (one per file) | Free (< 1M) |
| Email notifications (alerts only) | ~20 emails × $0.02/1K = $0.00 |
| **SNS total** | **$0.00** |

---

### 7. Amazon EventBridge

**Pricing:** $1.00 per 1M custom events

| Calculation | Monthly |
|-------------|---------|
| Events: 20 files/day × 30 = 600 | Free (< 1M) |
| **EventBridge total** | **$0.00** |

---

### 8. AWS Secrets Manager

**Pricing:** $0.40 per secret per month + $0.05 per 10K API calls

| Secret | Monthly |
|--------|---------|
| Redshift credentials | $0.40 |
| SFTP credentials | $0.40 |
| API calls: ~600 Glue runs × 2 calls = 1,200 calls | $0.01 |
| **Secrets Manager total** | **$0.81** |

---

### 9. CloudWatch

**Pricing:** $0.50 per GB ingested + $0.03 per GB stored/month + $0.01 per 1K Log Insights queries

| Component | Monthly |
|-----------|---------|
| Log ingestion: ~2 GB (Lambda + Glue + Step Functions) | $1.00 |
| Log storage: 2 GB × 30 days retention | $0.06 |
| Alarms: 6 alarms × $0.10 | $0.60 |
| Dashboard: 1 × $3.00 | $3.00 |
| **CloudWatch total** | **$4.66** |

---

## Monthly Cost Summary

| Service | Dev | Prod |
|---------|-----|------|
| Lambda | $0.00 | $0.00 |
| Glue | $132.00 | $330.00 |
| Redshift Serverless | $360.00 | $720.00 |
| S3 | $3.23 | $3.23 |
| SQS | $0.00 | $0.00 |
| SNS | $0.00 | $0.00 |
| EventBridge | $0.00 | $0.00 |
| Secrets Manager | $0.81 | $0.81 |
| CloudWatch | $4.66 | $4.66 |
| **Total** | **~$500** | **~$1,059** |

> These are estimates. Actual costs vary with data volume, job duration, and Redshift RPU scaling. Use AWS Cost Explorer to track actual spend.

---

## Cost Optimisation Opportunities

| Opportunity | Potential Saving | Trade-off |
|-------------|-----------------|-----------|
| Reduce Glue workers in dev (2 → 1) | ~$66/month | Slower dev jobs |
| Use Glue job bookmarks to skip unchanged files | Varies | Requires bookmark implementation |
| S3 Intelligent-Tiering for archive prefix | 40-68% on archive storage | Small monitoring fee per object |
| S3 Lifecycle policy: delete staging files after 1 day | ~$0.12/month | Already handled by Glue on success |
| Redshift pause during weekends (if no weekend loads) | ~30% of Redshift cost | Requires scheduled pause/resume |
| Reserved capacity for Redshift (1-year commit) | ~37% discount | Upfront commitment |
| CloudWatch log retention: reduce from 30 → 7 days for Glue | ~80% on Glue log storage | Less history for debugging |

---

## Cost Monitoring Setup

### Budget Alert (AWS Budgets)

```bash
aws budgets create-budget \
  --account-id {account-id} \
  --budget '{
    "BudgetName": "etl-orchestrator-dev-monthly",
    "BudgetLimit": {"Amount": "600", "Unit": "USD"},
    "TimeUnit": "MONTHLY",
    "BudgetType": "COST",
    "CostFilters": {
      "TagKeyValue": ["user:Project$etl-orchestrator"]
    }
  }' \
  --notifications-with-subscribers '[{
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 80,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [{
      "SubscriptionType": "EMAIL",
      "Address": "Yerram.saisanath@gmail.com"
    }]
  }]' \
  --profile etl-dev
```

> This alerts at 80% of the $600 monthly budget — giving time to investigate before the budget is exceeded.

### Cost Explorer Query

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date -d '1 month ago' +%Y-%m-01),End=$(date +%Y-%m-01) \
  --granularity MONTHLY \
  --metrics "UnblendedCost" \
  --group-by Type=DIMENSION,Key=SERVICE \
  --profile etl-dev
```

---

## Interview Tips

| Topic | What Interviewers Test |
|-------|----------------------|
| **Biggest cost driver** | Glue and Redshift dominate. Lambda, SQS, SNS, EventBridge are negligible at typical ETL volumes. |
| **Redshift Serverless vs provisioned** | Serverless is pay-per-use and scales automatically — ideal for sporadic ETL workloads. Provisioned clusters are cheaper at sustained 24/7 high-throughput usage. |
| **Glue DPU pricing** | Each `G.1X` worker = 1 DPU. Cost is DPU × hours. Minimising job duration and worker count is the primary lever. |
| **S3 storage classes** | Standard for active data, Intelligent-Tiering for unknown access patterns, Glacier for long-term archive (retrieval delay, very cheap). |
| **AWS Budgets vs Cost Alerts** | Budgets are proactive (alert before you overspend). Cost Explorer is retrospective (analyse what you already spent). Use both. |
| **Tagging for cost allocation** | All resources in this project use `tags` from Terraform. Tags enable Cost Explorer filtering by project, environment, and team. |
