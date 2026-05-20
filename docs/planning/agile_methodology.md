# Agile Methodology

This document explains how Agile principles were applied to this ETL orchestration project — how work was organised into sprints, how ceremonies were run, how user stories were written, and how the team continuously improved after each iteration.

---

## Why Agile Over Waterfall

| Waterfall | Agile (chosen) |
|-----------|---------------|
| Define all requirements upfront | Requirements evolved as domain was understood |
| Deliver everything at the end | Delivered working pipeline in phases |
| Change is expensive | Config-driven design makes change cheap |
| Long feedback loop | Each sprint produced a testable increment |

**Key reason:** The investment management domain (AUM, ZAUM, revenue plan vs actual) was not fully understood at the start. Agile allowed the team to learn the domain while building — the star schema Glue job was only scoped once the simple ETL proved the pattern worked.

---

## Scrum Framework Applied

```
Product Owner     → Data Engineering Lead (prioritises backlog)
Scrum Master      → Senior Engineer (removes blockers, runs ceremonies)
Development Team  → Engineers building Lambda, Glue, Terraform
Stakeholders      → Investment management analysts, operations team
```

---

## Sprint Structure

**Sprint length: 2 weeks**

```
Week 1, Day 1   → Sprint Planning
Daily           → Standup (15 minutes)
Week 2, Day 9   → Sprint Review (demo to stakeholders)
Week 2, Day 10  → Sprint Retrospective
Week 2, Day 10  → Next Sprint Planning begins
```

---

## Ceremonies

### Sprint Planning

**Purpose:** Decide what to build in the next 2 weeks.

**Inputs:**
- Product Backlog (`docs/planning/enhancements.md` + `docs/planning/review_findings.md`)
- Team capacity (how many engineers, any leave)
- Priority from Product Owner

**Output:** Sprint Backlog — a committed list of stories for the sprint

**Example Sprint 1 Planning:**
```
Goal: Deliver a working end-to-end pipeline for a single CSV → Redshift load

Selected from backlog:
  ✅ S3 bucket + EventBridge rule (Terraform)
  ✅ SQS FIFO queue with DLQ (Terraform)
  ✅ Lambda orchestrator — config matching + Step Functions trigger
  ✅ Step Functions state machine — single Glue job execution
  ✅ Glue simple ETL — CSV read, schema infer, Redshift COPY + UPSERT
  ✅ IAM roles for all services
  ✅ JOB_STS audit table
```

---

### Daily Standup

**Format (3 questions, 15 minutes max):**

1. What did I complete yesterday?
2. What will I complete today?
3. Is anything blocking me?

**Example standup during Glue job development:**
```
Engineer A:
  Yesterday: Completed Redshift COPY command with staging table pattern
  Today:     Working on MERGE/UPSERT logic using upsert_keys from config
  Blocker:   None

Engineer B:
  Yesterday: Finished Lambda config matching logic and SNS alert for inactive jobs
  Today:     Writing Step Functions state machine definition in Terraform
  Blocker:   Circular dependency between IAM module and Step Functions ARN
              → workaround: set step_function_arn = "*" in IAM, tighten post-deploy
```

> The circular dependency blocker above is the exact issue documented in `docs/security/iam_roles.md` — standups surface these before they become blockers.

---

### Sprint Review (Demo)

**Purpose:** Show stakeholders what was built. Get feedback. Adjust priorities.

**Format:**
- Live demo against the dev environment
- Walk through actual file delivery → Redshift load
- Show `JOB_STS` table with record counts
- Show CloudWatch logs
- Collect feedback on what is missing or wrong

**Example feedback received after Sprint 1 demo:**
```
Stakeholder: "We need the star schema model — flat tables aren't enough for
             the investment management reports."

Outcome:     data model Glue job added to Sprint 3 backlog
             dimensional_mappings.json config added to design
             SCD Type 1 scoped (Type 2 deferred to enhancement backlog)
```

This feedback directly produced `glue_jobs/with_data_model/glue_job_with_data_model.py`.

---

### Sprint Retrospective

**Purpose:** Improve how the team works — not what they build.

**Format (3 columns):**

| What went well | What didn't go well | What to try next sprint |
|---------------|--------------------|-----------------------|
| Config-driven design made onboarding new tables fast | Glue job grew to 946 lines — hard to review | Split into modules (schema, staging, merge, audit) |
| Terraform modules kept environments consistent | No tests — bugs only found in dev | Add scratch scripts before full integration |
| Structured JSON logging made debugging fast | Fixed retry delay (300s) wasted time on transient errors | Implement exponential backoff |
| Exponential backoff added quickly once pattern was clear | review_findings showed IaC was missing early on | Start with Terraform on day 1 next project |

> Many items from retrospectives are captured in `docs/planning/review_findings.md` and `docs/planning/enhancements.md` — these documents are the artefact of retrospective outputs.

---

## Product Backlog

The Product Backlog is the **master list of everything the product could do** — ordered by priority. In this project it lives across two files:

| File | What It Contains |
|------|-----------------|
| `docs/planning/enhancements.md` | New features and add-ons requested post-release |
| `docs/planning/review_findings.md` | Bugs, security issues, and architecture gaps found during review |

### Backlog Item States

```
Identified → Refined → Sprint Backlog → In Progress → Done
```

| State | Meaning |
|-------|---------|
| **Identified** | Logged but not yet sized or prioritised |
| **Refined** | Discussed, acceptance criteria written, estimated |
| **Sprint Backlog** | Committed for the current sprint |
| **In Progress** | Being developed on a feature branch |
| **Done** | Merged, deployed to dev, acceptance criteria met |

---

## User Stories

User stories capture requirements from the perspective of the person who benefits.

**Format:** `As a [role], I want [feature] so that [benefit].`

### Core Pipeline Stories

```
As a data engineer,
I want CSV files dropped in S3 to trigger an automated Redshift load,
so that I don't have to manually run SQL import scripts.

Acceptance criteria:
  ✅ File lands in data/in/ → Lambda triggers within 60 seconds
  ✅ Matching config entry found → Step Functions execution starts
  ✅ Glue job completes → target table updated in Redshift
  ✅ JOB_STS table updated with record counts and status
  ✅ Source file archived to data/archive/YYYY/MM/
```

```
As a data analyst,
I want to receive an SNS alert when a new unconfigured file arrives,
so that I can add it to config.json without losing the file.

Acceptance criteria:
  ✅ File not found in config.json → Lambda sends SNS email
  ✅ File moved to data/new_files/ (not deleted)
  ✅ SNS subject clearly states "New / Not Configured File"
  ✅ Email body includes original S3 path and moved-to path
```

```
As a data engineer,
I want Glue to automatically handle new columns in the source file,
so that schema changes don't require manual DDL or pipeline restarts.

Acceptance criteria:
  ✅ New column in CSV → ALTER TABLE ADD COLUMN executed automatically
  ✅ Missing column in CSV → NULL filled for that column in the load
  ✅ VARCHAR column too narrow → column widened automatically
  ✅ No data loss from existing rows during schema change
```

```
As an operations manager,
I want every pipeline run logged to a JOB_STS table,
so that I can audit what was loaded, when, and how many records.

Acceptance criteria:
  ✅ Every run writes one row to JOB_STS regardless of success/failure
  ✅ Row includes: job_id, source file, start/end time, records read/inserted/updated
  ✅ Failed runs write status = FAILED and include the error message
  ✅ JOB_STS is queryable via Redshift Data API
```

### Enhancement Stories (Post-Release Backlog)

```
As a data engineer,
I want the pipeline to raise an exception when a nullable column
is part of the composite primary key,
so that UPSERT logic doesn't silently produce wrong results.

Acceptance criteria:
  □ Glue job checks upsert_keys against DataFrame null counts before load
  □ If any upsert_key column has nulls → job fails with clear error message
  □ JOB_STS updated with FAILED status and null key column name
```

```
As a data engineer,
I want to apply column-level transformations based on the source file name pattern,
so that files from different sources can be normalised before loading.

Acceptance criteria:
  □ Transformation rules defined in config.json per job
  □ Glue job applies transformations before schema reconciliation
  □ Transformations include: rename, cast, derive new column, filter rows
```

---

## Definition of Done

A story is **Done** only when all of the following are true:

| Criterion | Check |
|-----------|-------|
| Code reviewed by at least one other engineer | PR approval required |
| `terraform plan` shows no unintended changes | CI validates on every PR |
| Deployed to dev and manually verified end-to-end | File delivered, Redshift checked |
| `JOB_STS` record written correctly | Queried after test run |
| CloudWatch logs show no errors | Log group checked post-run |
| Acceptance criteria from the user story all pass | Checked against story |
| No new items added to `review_findings.md` | Peer review clean |
| Merged to main and promoted through dev → qa | CI pipeline green |

---

## Agile Artefacts in This Repository

| Agile Artefact | Where It Lives in This Project |
|---------------|-------------------------------|
| Product Backlog | `docs/planning/enhancements.md` + `docs/planning/review_findings.md` |
| Sprint Goal | Git commit message on each feature branch merge |
| Definition of Done | This document (above) |
| Sprint Retrospective output | `docs/planning/review_findings.md` (issues found = retro actions) |
| User Stories | This document + acceptance criteria in PR descriptions |
| Increment (working software) | Each merged PR deployed to dev via GitHub Actions |
| Burndown | Tracked via GitHub Issues / PR count against sprint milestone |

---

## Continuous Improvement Examples

Agile is not just about ceremonies — it is about improving the product and the process every sprint. Here are concrete examples from this project:

### Improvement 1 — Fixed Retry Delay → Exponential Backoff

**Identified in:** `docs/planning/review_findings.md` (item #22)
**Problem:** 300-second fixed retry delay wasted time on transient Redshift errors
**Action:** Replaced with `@retry_on_exception` decorator — 5s, 10s, 20s backoff
**Result:** Average retry recovery time dropped significantly

### Improvement 2 — No IaC → Full Terraform

**Identified in:** `docs/planning/review_findings.md` (item #15)
**Problem:** No Infrastructure as Code meant environments could drift and rebuilding was manual
**Action:** All 42 AWS resources defined in 10 Terraform modules
**Result:** `terraform apply` reproduces any environment from scratch in under 10 minutes

### Improvement 3 — No Monitoring → CloudWatch Alarms + Dashboard

**Identified in:** `docs/planning/review_findings.md` (item #21)
**Problem:** Logs emitted but no alerting — failures only discovered by users
**Action:** 6 CloudWatch alarms + dashboard defined in `docs/operations/monitoring_and_alerting.md`
**Result:** Failures detected within 60 seconds via SNS email

### Improvement 4 — No Audit → JOB_STS Table

**Identified in:** Sprint 1 stakeholder demo feedback
**Problem:** No visibility into what loaded, how many records, or when
**Action:** `update_job_sts_table()` added to both Glue jobs — writes on success and failure
**Result:** Operations team can self-serve audit queries without asking engineering

---

## How New Feature Requests Enter the Process

```
Stakeholder raises request
        │
        ▼
Product Owner logs it in enhancements.md with:
  - Description
  - Business value (why it matters)
  - Rough size (small / medium / large)
        │
        ▼
Backlog Refinement session (mid-sprint)
  - Team discusses feasibility
  - Acceptance criteria written
  - Story pointed (1, 2, 3, 5, 8)
        │
        ▼
Prioritised in next Sprint Planning
        │
        ▼
Developed on feature branch → PR → CI → merged
        │
        ▼
Demoed in Sprint Review → stakeholder confirms Done
```

---

## Story Points Reference

| Points | Meaning | Example |
|--------|---------|---------|
| 1 | Trivial — less than 2 hours | Add a new field to `config.json` |
| 2 | Small — half a day | Add a new CloudWatch alarm |
| 3 | Medium — 1 day | Add nullable key validation to Glue job |
| 5 | Large — 2-3 days | New Terraform module (e.g. VPC endpoints) |
| 8 | Very large — full sprint | New Glue job variant (e.g. data model job) |
| 13 | Too large — needs splitting | VPC network redesign |

---

## Interview Tips

| Topic | What Interviewers Test |
|-------|----------------------|
| **Why Agile over Waterfall for ETL** | Data pipelines have evolving schemas and business rules — waiting to define everything upfront leads to rework. Agile allows delivering a working pipeline early and refining based on real data |
| **How you handle scope creep** | Anything new goes to the backlog — it does not enter the current sprint without the Product Owner reprioritising and something else dropping out |
| **User story vs task** | A user story describes business value ("as a data analyst, I want..."). A task is a technical step to deliver it ("create staging table in Redshift"). Stories are in the backlog; tasks are on the sprint board |
| **Definition of Done importance** | Prevents "it works on my machine" — without a shared DoD, different engineers have different standards for what complete means |
| **How review_findings.md fits Agile** | It is the output of a code review sprint (or retrospective) — each finding is a backlog item with a severity that drives prioritisation |
| **Velocity** | Number of story points completed per sprint. Used to forecast how many sprints a backlog will take. Not used to compare engineers — used to plan |
| **Retrospective vs Review** | Review = what did we build (demo to stakeholders). Retrospective = how did we work (internal team process improvement). Common exam/interview confusion |
| **Config-driven design and Agile** | The config.json pattern reduces the cost of change — new table onboarding is a 5-minute config update, not a sprint. This is Agile thinking applied to architecture |
