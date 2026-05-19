# CI/CD Pipeline

This document describes how to automate Terraform deployments for this ETL pipeline using GitHub Actions with OIDC authentication — no long-lived AWS access keys stored in GitHub.

---

## Why OIDC Instead of Access Keys

| | Access Keys (old way) | OIDC (recommended) |
|-|-----------------------|--------------------|
| **Secret storage** | Keys stored in GitHub Secrets — rotate manually every 90 days | No keys stored anywhere — GitHub requests temporary credentials per run |
| **Credential lifetime** | Permanent until deleted | Expires after the workflow job ends (typically 1 hour) |
| **Blast radius if leaked** | Full IAM user access until key is rotated | Nothing — credentials are job-scoped and already expired |
| **Audit trail** | API calls appear as the IAM user | API calls appear with the GitHub Actions workflow identity |

---

## Architecture

```
GitHub PR / Merge
      │
      ▼
GitHub Actions Workflow
      │
      ├── Request temporary credentials from AWS STS via OIDC
      │       (GitHub proves identity using JWT signed by GitHub's OIDC provider)
      │
      ├── terraform init   → downloads providers, configures S3 backend
      ├── terraform plan   → shows what will change (posted as PR comment)
      └── terraform apply  → applies changes (on merge to main only)
```

---

## Step 1: Create the OIDC Identity Provider in AWS

This tells AWS to trust GitHub's OIDC tokens. Do this once per AWS account.

```bash
# Create the OIDC provider
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  --profile etl-dev
```

---

## Step 2: Create the GitHub Actions IAM Role

This role is assumed by GitHub Actions via OIDC. The trust policy restricts it to your specific repository and branch.

### Trust Policy (`github-actions-trust.json`)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::{account-id}:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:greenwichg/aws_etl_orchestratior:*"
        }
      }
    }
  ]
}
```

> **`StringLike` with `*`:** Allows any branch or ref in the repository to assume this role. For production, tighten to `repo:greenwichg/aws_etl_orchestratior:ref:refs/heads/main` to restrict to the main branch only.

### Create the Role

```bash
# Create role with trust policy
aws iam create-role \
  --role-name etl-orchestrator-github-actions \
  --assume-role-policy-document file://github-actions-trust.json \
  --profile etl-dev

# Attach the same deployer policy created in aws_setup_guide.md
aws iam attach-role-policy \
  --role-name etl-orchestrator-github-actions \
  --policy-arn arn:aws:iam::{account-id}:policy/etl-orchestrator-deployer-policy \
  --profile etl-dev
```

---

## Step 3: GitHub Repository Setup

### Add Repository Secrets

In GitHub → Repository → Settings → Secrets and variables → Actions:

| Secret Name | Value |
|-------------|-------|
| `AWS_ROLE_ARN_DEV` | `arn:aws:iam::{account-id}:role/etl-orchestrator-github-actions-dev` |
| `AWS_ROLE_ARN_QA` | `arn:aws:iam::{account-id}:role/etl-orchestrator-github-actions-qa` |
| `AWS_ROLE_ARN_STAGING` | `arn:aws:iam::{account-id}:role/etl-orchestrator-github-actions-staging` |
| `AWS_ROLE_ARN_PROD` | `arn:aws:iam::{account-id}:role/etl-orchestrator-github-actions-prod` |

### Required Repository Permission

In GitHub → Repository → Settings → Actions → General:

- Set **Workflow permissions** to **Read and write permissions**
- Check **Allow GitHub Actions to create and approve pull requests**

---

## Step 4: GitHub Actions Workflows

Create these files in `.github/workflows/`.

### `terraform-plan.yml` — Run on Every PR

Runs `terraform plan` for the target environment and posts the output as a PR comment so reviewers can see exactly what will change.

```yaml
name: Terraform Plan

on:
  pull_request:
    branches: [main]
    paths:
      - 'terraform/**'
      - 'lambda_functions/**'
      - 'glue_jobs/**'

permissions:
  id-token: write       # required for OIDC
  contents: read
  pull-requests: write  # required to post plan as PR comment

jobs:
  plan-dev:
    name: Plan (dev)
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: terraform/environments/dev

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "~> 1.5"

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_DEV }}
          aws-region: us-east-1

      - name: Terraform Init
        run: terraform init

      - name: Terraform Format Check
        run: terraform fmt -check -recursive

      - name: Terraform Validate
        run: terraform validate

      - name: Terraform Plan
        id: plan
        run: terraform plan -no-color -out=tfplan
        continue-on-error: true

      - name: Post Plan to PR
        uses: actions/github-script@v7
        with:
          script: |
            const output = `### Terraform Plan — dev
            \`\`\`
            ${{ steps.plan.outputs.stdout }}
            \`\`\`
            Plan status: ${{ steps.plan.outcome }}`;
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: output
            });

      - name: Fail if plan failed
        if: steps.plan.outcome == 'failure'
        run: exit 1
```

---

### `terraform-apply.yml` — Run on Merge to Main

Applies infrastructure changes after a PR is merged. Each environment requires a manual approval gate before apply runs.

```yaml
name: Terraform Apply

on:
  push:
    branches: [main]
    paths:
      - 'terraform/**'
      - 'lambda_functions/**'
      - 'glue_jobs/**'

permissions:
  id-token: write
  contents: read

jobs:
  apply-dev:
    name: Apply (dev)
    runs-on: ubuntu-latest
    environment: dev           # maps to GitHub Environment with optional protection rules
    defaults:
      run:
        working-directory: terraform/environments/dev

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "~> 1.5"

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_DEV }}
          aws-region: us-east-1

      - name: Terraform Init
        run: terraform init

      - name: Terraform Apply
        run: terraform apply -auto-approve

  apply-qa:
    name: Apply (qa)
    needs: apply-dev           # only runs if dev succeeded
    runs-on: ubuntu-latest
    environment: qa
    defaults:
      run:
        working-directory: terraform/environments/qa

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "~> 1.5"

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_QA }}
          aws-region: us-east-1

      - name: Terraform Init
        run: terraform init

      - name: Terraform Apply
        run: terraform apply -auto-approve

  apply-staging:
    name: Apply (staging)
    needs: apply-qa
    runs-on: ubuntu-latest
    environment: staging
    defaults:
      run:
        working-directory: terraform/environments/staging

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "~> 1.5"

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_STAGING }}
          aws-region: us-east-1

      - name: Terraform Init
        run: terraform init

      - name: Terraform Apply
        run: terraform apply -auto-approve

  apply-prod:
    name: Apply (prod)
    needs: apply-staging
    runs-on: ubuntu-latest
    environment: prod          # configure required reviewers here in GitHub
    defaults:
      run:
        working-directory: terraform/environments/prod

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "~> 1.5"

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_PROD }}
          aws-region: us-east-1

      - name: Terraform Init
        run: terraform init

      - name: Terraform Plan (final review)
        run: terraform plan -out=prod.tfplan

      - name: Terraform Apply
        run: terraform apply prod.tfplan
```

---

## Step 5: GitHub Environment Protection Rules

For `staging` and `prod` environments, add required reviewers so no one can accidentally apply to production.

GitHub → Repository → Settings → Environments → `prod`:

- **Required reviewers:** add senior engineers or team leads
- **Wait timer:** optional delay (e.g. 10 minutes) before apply can proceed
- **Deployment branches:** restrict to `main` branch only

---

## Deployment Flow Summary

```
Developer opens PR
        │
        ▼
terraform-plan.yml runs
  ├── fmt check
  ├── validate
  └── plan → posted as PR comment
        │
  PR reviewed & merged to main
        │
        ▼
terraform-apply.yml runs
  ├── Apply dev   → automatic
  ├── Apply qa    → automatic (after dev succeeds)
  ├── Apply staging → automatic (after qa succeeds)
  └── Apply prod  → requires manual approval from reviewer
```

---

## Interview Tips

| Topic | What Interviewers Test |
|-------|----------------------|
| **OIDC vs access keys in CI/CD** | OIDC is the modern best practice — no stored secrets, credentials expire per-job. Access keys in CI/CD are a security risk because they're long-lived and can be exposed in logs. |
| **`id-token: write` permission** | GitHub Actions needs this permission to request an OIDC token from GitHub's OIDC provider. Without it, `sts:AssumeRoleWithWebIdentity` fails. |
| **`StringLike` vs `StringEquals` in trust policy** | `StringLike` supports wildcards — used when you want any branch to deploy. `StringEquals` is an exact match — use for prod where only `main` should be able to deploy. |
| **`needs` keyword** | Creates a sequential dependency between jobs. `apply-qa: needs: apply-dev` means QA only deploys if dev succeeded — prevents broken code reaching higher environments. |
| **GitHub Environments** | A GitHub feature that allows environment-specific secrets, required reviewers, and wait timers. Maps directly to the `environment:` key in the workflow. |
| **`-auto-approve` risk** | Only safe in CI/CD where the plan was already reviewed on the PR. Never use it locally without reviewing the plan first. |
| **Terraform plan in PR** | Posting the plan output as a PR comment is a best practice — it makes infrastructure changes reviewable alongside code changes, not just after merge. |
| **`terraform fmt -check`** | Enforces consistent formatting. Fails the CI if anyone forgot to run `terraform fmt` locally. |
