# Software Development Life Cycle (SDLC)

This document walks through how SDLC was applied to this ETL orchestration project — from the initial idea through production deployment — and how new feature requests and enhancements are handled after release.

---

## SDLC Model Used: Iterative & Agile

This project follows an **iterative approach** — a working pipeline was delivered in phases rather than waiting for a perfect system. Each iteration adds capability on top of the previous one.

```
Phase 1 → Core pipeline (S3 → Lambda → Step Functions → Glue → Redshift)
Phase 2 → Star schema support (data model Glue job)
Phase 3 → Operational hardening (monitoring, alerting, CI/CD)
Phase 4 → Ongoing enhancements (driven by review_findings.md + enhancements.md)
```

---

## Phase 1 — Planning

**Goal:** Define what the system needs to do, who it serves, and whether it is worth building.

### What Was Done

- Defined the business problem: manual file delivery to Redshift was slow, error-prone, and had no audit trail
- Identified stakeholders: data engineering team, investment management analysts, operations
- Scoped the initial delivery: automated CSV ingestion → Redshift with UPSERT logic
- Estimated infrastructure costs and compared against the manual effort cost

### Artefacts Produced

| Artefact | Location |
|----------|----------|
| Business domain understanding | `docs/planning/investment_mgmt.md` |
| Infrastructure cost model | `docs/operations/cost_estimation.md` |
| Enhancement backlog (initial scope) | `docs/planning/enhancements.md` |
| Multi-environment strategy | `terraform/environments/dev`, `qa`, `staging`, `prod` |

### Key Decisions Made at This Stage

- **4 environments** (dev → qa → staging → prod) to reduce deployment risk
- **Serverless-first** (Lambda, Glue, Step Functions) to avoid cluster management overhead
- **Config-driven design** — job definitions live in `config/config.json`, not hardcoded in the pipeline, so new tables can be onboarded without code changes
- **AWS region:** `us-east-1` for all environments

---

## Phase 2 — Requirements Analysis

**Goal:** Understand exactly what the system must do, what the data looks like, and what the constraints are.

### What Was Done

- Mapped the end-to-end workflow step by step — file arrival to Redshift load to audit log
- Identified all source file patterns (CSV, variable schema)
- Defined the three Lambda routing outcomes: not configured, inactive, active
- Captured Redshift target schema requirements per table
- Identified non-functional requirements: retry logic, audit trail, error handling, log export

### Artefacts Produced

| Artefact | Location |
|----------|----------|
| End-to-end workflow | `docs/architecture/design_flow.md` |
| S3 folder structure requirements | `docs/architecture/s3_directory_structure.md` |
| State machine flow | `docs/architecture/state_machine.json` |
| EventBridge rule definition | `docs/architecture/event_bridge_rule.json` |
| Data model requirements | `docs/pipeline/data_modeling/star_schema_model.md` |
| Table DDL specifications | `docs/pipeline/data_modeling/data_definition_language.md` |
| Domain terminology | `docs/pipeline/data_modeling/terminologies/` |

### Config-Driven Requirements

A key requirement identified was that **new tables should require zero code changes**. This drove the `config.json` design:

```json
[
  {
    "job_id": "JOB_001",
    "job_name": "customer_load",
    "source_file_name": "customers.csv",
    "target_table": "customers",
    "upsert_keys": ["customer_id"],
    "is_active": true,
    "glue_job": "simple"
  }
]
```

Adding a new table = adding one JSON block to this file. No Lambda, Glue, or Terraform changes required.

---

## Phase 3 — System Design

**Goal:** Decide how the system will be built — architecture, security, network, data model, infrastructure.

### What Was Done

- Designed the service architecture and inter-service connections
- Defined IAM roles for every service with least-privilege permissions
- Designed the S3 folder structure to support the full file lifecycle
- Designed the Redshift schema including staging tables, target tables, audit table, and views
- Selected infrastructure tooling: Terraform for IaC, GitHub Actions for CI/CD
- Designed the two Glue job variants: simple ETL and star schema (data model)

### Artefacts Produced

| Artefact | Location |
|----------|----------|
| Architecture diagram and service connections | `docs/architecture/design_flow.md` |
| Network design (current + recommended VPC) | `docs/architecture/network_architecture.md` |
| All IAM roles with trust + permission policies | `docs/security/iam_roles.md` |
| Star schema model | `docs/pipeline/data_modeling/star_schema_model.md` |
| SCD Type 2 design | `docs/pipeline/data_modeling/scd_type2_implementation_guide.md` |
| Terraform module structure | `terraform/modules/` |

### Architecture Decisions Made

| Decision | Rationale |
|----------|-----------|
| SQS between EventBridge and Lambda | Decouples event delivery from processing; provides retry and DLQ |
| Step Functions `.sync` pattern | Waits for Glue to finish before marking job complete |
| `MaxConcurrency: 1` in state machine | Prevents two jobs writing to the same Redshift table simultaneously |
| Redshift Data API over JDBC | No VPC or driver required; simpler connectivity from Glue |
| S3 remote state with DynamoDB locking | Shared Terraform state for team; prevents concurrent apply corruption |

---

## Phase 4 — Implementation (Development)

**Goal:** Build the system according to the design, using Infrastructure as Code for all AWS resources.

### What Was Built

#### Lambda Functions
| File | Purpose |
|------|---------|
| `lambda_functions/lambda_function.py` | Orchestrator — validates file, routes to Step Functions |
| `lambda_functions/logs_to_smb.py` | SFTP Lambda — replicates logs to shared drive |

#### Glue Jobs
| File | Purpose |
|------|---------|
| `glue_jobs/without_data_model/glue_job.py` | Simple ETL — CSV → Redshift UPSERT (946 lines) |
| `glue_jobs/with_data_model/glue_job_with_data_model.py` | Data model ETL — CSV → dimensions + facts (1,165 lines) |

#### Infrastructure (Terraform Modules)
| Module | What It Provisions |
|--------|--------------------|
| `terraform/modules/s3` | Buckets, lifecycle rules, EventBridge notification |
| `terraform/modules/lambda` | Lambda functions, SQS event source mapping |
| `terraform/modules/sqs` | FIFO queue, DLQ, EventBridge resource policy |
| `terraform/modules/eventbridge` | S3 ObjectCreated rule and SQS target |
| `terraform/modules/step_functions` | State machine, CloudWatch log group |
| `terraform/modules/glue` | Two Glue jobs with all arguments wired |
| `terraform/modules/iam` | All four IAM roles with policies |
| `terraform/modules/redshift` | Serverless namespace, workgroup, Redshift IAM role |
| `terraform/modules/sns` | Topic and email subscription |
| `terraform/modules/secrets_manager` | Redshift and SFTP secrets |

### Coding Standards Applied

- **Retry with exponential backoff** on all Redshift Data API calls (`@retry_on_exception`)
- **Structured JSON logging** via `LogBuffer` — emits to CloudWatch and exports to S3
- **Config-driven routing** — Lambda reads `config.json` at runtime, no hardcoded table names
- **Schema auto-evolution** — Glue detects new/missing columns and applies `ALTER TABLE` automatically
- **Audit trail** — every run writes to `JOB_STS` table in Redshift with record counts and status

---

## Phase 5 — Testing

**Goal:** Validate that the system behaves correctly before promoting to higher environments.

### Testing Strategy

| Test Type | Where | How |
|-----------|-------|-----|
| Unit exploration | `docs/scratch/` | Feature scripts tested in isolation before integration |
| Integration testing | `terraform/environments/dev` | Full pipeline run with real files in dev AWS account |
| QA validation | `terraform/environments/qa` | Stakeholder-verified data output against expected results |
| Staging regression | `terraform/environments/staging` | Production-like load test before final promotion |
| Production | `terraform/environments/prod` | Live — monitored via CloudWatch alarms |

### Scratch Scripts Used During Development

| File | Purpose |
|------|---------|
| `docs/scratch/feature_test.py` | Testing individual feature behaviour in isolation |
| `docs/scratch/fill_null.py` | Validating null-fill logic before integrating into Glue job |
| `docs/scratch/s3_2_redshift.py` | Proving S3 → Redshift COPY connectivity early |

### What the Code Review Found

The `docs/planning/review_findings.md` captured issues discovered during peer review before the first production release:

- **6 critical runtime bugs** (undefined type aliases, missing arguments, parameter name mismatch)
- **4 security concerns** (SSH host key validation disabled, SQL string interpolation)
- **5 architecture gaps** (no IaC originally, monolithic Glue job, no tests)
- **4 operational gaps** (no CloudWatch alarms, fixed retry delays)

These findings directly drove the improvements implemented in subsequent iterations — Terraform was added, exponential backoff replaced fixed delays, and CloudWatch alarms were defined.

---

## Phase 6 — Deployment

**Goal:** Release the system to production safely, with the ability to roll back if something goes wrong.

### Deployment Pipeline

Every code change follows this promotion path:

```
Developer branch
      │
      ▼ Pull Request opened
GitHub Actions runs terraform plan → posts output as PR comment
      │
      ▼ PR reviewed and merged to main
GitHub Actions applies in sequence:
      │
      ├── dev   (automatic)
      ├── qa    (automatic, after dev succeeds)
      ├── staging (automatic, after qa succeeds)
      └── prod  (requires manual approval from reviewer)
```

Full workflow definitions: `docs/setup/cicd_pipeline.md`

### Infrastructure Deployment Order

Terraform modules have dependencies that enforce a safe creation order:

```
S3 → IAM → SQS → EventBridge → SNS → Secrets Manager
                                          ↓
                              Redshift ← Glue ← Step Functions ← Lambda
```

### Rollback Strategy

- Terraform state is versioned in S3 — any previous state can be restored
- Each environment is independent — a bad prod deploy does not affect dev/qa state
- Lambda and Glue scripts are versioned in Git — reverting a deploy means reverting the commit and re-running `terraform apply`

---

## Phase 7 — Maintenance & Monitoring

**Goal:** Keep the system healthy in production and detect problems before users do.

### Monitoring Setup

| What is Monitored | Where | Reference |
|-------------------|-------|-----------|
| Lambda errors and throttles | CloudWatch Alarms | `docs/operations/monitoring_and_alerting.md` |
| SQS DLQ — any message = failure | CloudWatch Alarm | `docs/operations/monitoring_and_alerting.md` |
| Step Functions execution failures | CloudWatch Alarm | `docs/operations/monitoring_and_alerting.md` |
| Glue job run status | CloudWatch + S3 logs | `docs/operations/troubleshooting_guide.md` |
| Redshift load errors | `stl_load_errors` table | `docs/operations/troubleshooting_guide.md` |
| Job audit trail | `JOB_STS` table in Redshift | `docs/pipeline/data_pipeline_logic.md` |

### Incident Response

When a pipeline failure is reported the standard approach is:

1. Check `JOB_STS` table — what was the last status and error message?
2. Check CloudWatch logs for the relevant service (Lambda → Glue → Redshift)
3. Check S3 `data/unprocessed/` — was the file moved there on failure?
4. Fix root cause, re-run via Step Functions start-execution CLI command
5. Verify record counts in Redshift match source file

Full runbook: `docs/operations/troubleshooting_guide.md`

---

## Phase 8 — Handling New Feature Requests & Enhancements

**Goal:** Add new capability without destabilising the existing pipeline.

### How a New Feature Request Flows

```
Request received (from stakeholder or review)
        │
        ▼
Logged in docs/planning/enhancements.md
        │
        ▼
Impact assessed:
  ├── Config-only change?  → No code change needed (same day)
  ├── New Glue argument?   → Small code + Terraform change (1-2 days)
  └── New service/module?  → Full SDLC cycle for that component
        │
        ▼
Developed on feature branch
        │
        ▼
PR opened → terraform plan posted → code reviewed
        │
        ▼
Promoted dev → qa → staging → prod
```

### Three Tiers of Changes

#### Tier 1 — Config Change Only (No Code, No Terraform)

**Use case:** Onboard a new source file to an existing target table.

```json
// Add one block to config/config.json in S3
{
  "job_id": "JOB_015",
  "job_name": "portfolio_load",
  "source_file_name": "portfolio.csv",
  "target_table": "portfolio",
  "upsert_keys": ["portfolio_id"],
  "is_active": true,
  "glue_job": "simple"
}
```

Upload the updated `config.json` to S3. The next file delivery picks it up automatically. Zero deployment needed.

#### Tier 2 — Code Change, Existing Infrastructure

**Use case:** Add a new transformation, fix a bug, extend retry logic.

1. Branch from `main`
2. Edit the relevant Lambda or Glue script
3. Open PR → CI runs `terraform plan` (shows no infrastructure change, only script update)
4. Merge → CI deploys automatically through environments

Current backlog items in this tier (`docs/planning/enhancements.md`):
- Raise exception when primary key contributing to composite key is nullable
- Apply transformations based on source file name pattern

#### Tier 3 — New Infrastructure Component

**Use case:** Add a new AWS service, new Terraform module, new environment.

Full mini-SDLC cycle:
1. Design the component (add to `docs/architecture/`)
2. Define IAM permissions (update `docs/security/iam_roles.md`)
3. Write Terraform module under `terraform/modules/`
4. Wire into `terraform/main.tf`
5. Test in dev → promote through environments via CI/CD

---

## Current Enhancement Backlog

Tracked in `docs/planning/enhancements.md` and `docs/planning/review_findings.md`:

| Priority | Enhancement | Tier | Status |
|----------|------------|------|--------|
| High | Fix 6 critical runtime bugs (review_findings #1-6) | 2 | Identified |
| High | Add unit tests for schema reconciliation and merge logic | 2 | Identified |
| High | Fix SSH host-key validation (AutoAddPolicy) | 2 | Identified |
| Medium | Integrate data quality framework into pipeline | 2 | Identified |
| Medium | Nullable primary key validation | 2 | Backlog |
| Medium | Transformation based on source file name pattern | 2 | Backlog |
| Medium | Decompose monolithic Glue job into focused modules | 3 | Backlog |
| Low | VPC + private subnets + VPC endpoints | 3 | Planned |
| Low | Athena query layer on S3 archive (Parquet conversion) | 3 | Planned |

---

## SDLC Summary — Mapped to This Project

```
PLANNING          → investment_mgmt.md, cost_estimation.md
                    Multi-env strategy, serverless-first decision

REQUIREMENTS      → design_flow.md, config.json structure
                    3 Lambda outcomes, schema auto-evolution requirement

DESIGN            → iam_roles.md, network_architecture.md
                    state_machine.json, data_modeling/

IMPLEMENTATION    → lambda_function.py, glue_job.py
                    terraform/modules/ (10 modules, 42 resources)

TESTING           → docs/scratch/, dev + qa environments
                    review_findings.md (23 issues found and logged)

DEPLOYMENT        → cicd_pipeline.md (GitHub Actions + OIDC)
                    dev → qa → staging → prod with approval gate

MAINTENANCE       → monitoring_and_alerting.md, troubleshooting_guide.md
                    JOB_STS audit table, CloudWatch alarms

ENHANCEMENTS      → enhancements.md, review_findings.md
                    3-tier change model (config / code / infrastructure)
```

---

## Interview Tips

| Topic | What Interviewers Test |
|-------|----------------------|
| **Why config-driven design** | Separates data concerns from code — new tables don't require a deployment, reducing risk and time-to-onboard |
| **Why 4 environments** | Each promotion gate catches a different class of problem: dev (developer bugs), qa (business logic), staging (load/performance), prod (live) |
| **How do you handle breaking changes** | Schema auto-evolution handles additive changes automatically; destructive changes (column drops, type changes) require a migration plan and manual Redshift DDL |
| **What happens when a job fails at 2am** | CloudWatch alarm fires → SNS email → on-call checks `JOB_STS` + CloudWatch logs → re-runs via CLI → verifies record counts |
| **How do you prioritise enhancements** | Business impact first (data quality bugs over cosmetic), then risk reduction (security fixes), then operational improvements |
| **SDLC model choice** | Iterative was chosen over waterfall because requirements evolved as the business domain was understood — the star schema job was added in phase 2 after the simple job proved the pattern |
| **IaC from the start vs added later** | review_findings.md shows IaC was added retrospectively — the lesson: starting with Terraform from day one prevents environment drift and manual state accumulation |
