# AWS Account & CLI Setup Guide

This guide walks through everything needed to set up AWS access, configure the CLI, create IAM users with the right permissions, and deploy this ETL pipeline from scratch.

---

## Table of Contents

1. [AWS Account Setup](#1-aws-account-setup)
2. [Secure the Root Account](#2-secure-the-root-account)
3. [Create an IAM User for Deployment](#3-create-an-iam-user-for-deployment)
4. [Generate Access Keys](#4-generate-access-keys)
5. [Install the AWS CLI](#5-install-the-aws-cli)
6. [Configure AWS CLI Profiles](#6-configure-aws-cli-profiles)
7. [Verify Your Setup](#7-verify-your-setup)
8. [Install Terraform](#8-install-terraform)
9. [Bootstrap Terraform State Backend](#9-bootstrap-terraform-state-backend)
10. [Deploy the Pipeline](#10-deploy-the-pipeline)
11. [Environment Variables Reference](#11-environment-variables-reference)
12. [Credentials Best Practices](#12-credentials-best-practices)
13. [Field Reference — Key Concepts](#13-field-reference--key-concepts)
14. [Interview Tips](#14-interview-tips)

---

## 1. AWS Account Setup

### Create a New AWS Account

1. Go to [https://aws.amazon.com](https://aws.amazon.com) and click **Create an AWS Account**
2. Provide an email address, account name, and password — this becomes the **root account**
3. Enter billing information (a credit card is required even for free-tier accounts)
4. Choose a support plan (Basic is free)
5. Verify your phone number

> **Root account warning:** The root account has unrestricted access to everything in your AWS account including billing. It cannot be restricted by IAM policies. Never use it for day-to-day operations — create an IAM user instead.

### Recommended Account Structure for This Pipeline

| Environment | AWS Account | Purpose |
|-------------|------------|---------|
| Development | Separate account (or same account with isolation) | Safe to experiment, destroy, redeploy |
| QA | Separate account | Integration testing |
| Staging | Separate account | Pre-production validation |
| Production | Separate account | Live workloads, tightest access controls |

> Using separate AWS accounts per environment is the AWS-recommended multi-account strategy (AWS Organizations). It provides the strongest blast radius isolation — a misconfigured dev resource cannot affect prod.

---

## 2. Secure the Root Account

Do these immediately after creating the account, before creating any other resources.

### Enable MFA on Root

1. Sign in as root → top-right menu → **Security credentials**
2. Under **Multi-factor authentication (MFA)** → **Assign MFA device**
3. Choose **Authenticator app** (e.g. Google Authenticator, Authy)
4. Scan the QR code and enter two consecutive codes to confirm
5. Click **Add MFA**

### Set Account Alias

An account alias replaces your 12-digit account ID in the sign-in URL.

```bash
aws iam create-account-alias --account-alias etl-orchestrator-dev
# Sign-in URL becomes: https://etl-orchestrator-dev.signin.aws.amazon.com/console
```

### Enable CloudTrail (Audit Logging)

```bash
aws cloudtrail create-trail \
  --name etl-orchestrator-audit \
  --s3-bucket-name etl-orchestrator-cloudtrail-logs \
  --is-multi-region-trail \
  --enable-log-file-validation
```

---

## 3. Create an IAM User for Deployment

Never use the root account for Terraform deployments. Create a dedicated IAM user (or use IAM roles with a CI/CD system in production).

### Via AWS Console

1. IAM → **Users** → **Create user**
2. Username: `etl-orchestrator-deployer`
3. Check **Provide user access to the AWS Management Console** only if needed
4. Permissions → **Attach policies directly** → attach the policies below
5. Click **Create user**

### Via AWS CLI (run as root or an admin user)

```bash
# Create the user
aws iam create-user --user-name etl-orchestrator-deployer

# Create a deployment policy (save as deployer-policy.json first — see below)
aws iam create-policy \
  --policy-name etl-orchestrator-deployer-policy \
  --policy-document file://deployer-policy.json

# Attach the policy to the user
aws iam attach-user-policy \
  --user-name etl-orchestrator-deployer \
  --policy-arn arn:aws:iam::{account-id}:policy/etl-orchestrator-deployer-policy
```

### Deployer Policy (`deployer-policy.json`)

This policy grants the permissions needed to deploy all resources in this pipeline via Terraform.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3DeployAccess",
      "Effect": "Allow",
      "Action": ["s3:*"],
      "Resource": [
        "arn:aws:s3:::etl-orchestrator-*",
        "arn:aws:s3:::etl-orchestrator-*/*"
      ]
    },
    {
      "Sid": "LambdaDeployAccess",
      "Effect": "Allow",
      "Action": ["lambda:*"],
      "Resource": "arn:aws:lambda:us-east-1:{account-id}:function:etl-orchestrator-*"
    },
    {
      "Sid": "GlueDeployAccess",
      "Effect": "Allow",
      "Action": ["glue:*"],
      "Resource": "*"
    },
    {
      "Sid": "StepFunctionsDeployAccess",
      "Effect": "Allow",
      "Action": ["states:*"],
      "Resource": "arn:aws:states:us-east-1:{account-id}:stateMachine:etl-orchestrator-*"
    },
    {
      "Sid": "IAMDeployAccess",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:AttachRolePolicy",
        "iam:DetachRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy",
        "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies", "iam:PassRole", "iam:TagRole",
        "iam:CreatePolicy", "iam:DeletePolicy", "iam:GetPolicy",
        "iam:GetPolicyVersion", "iam:ListPolicyVersions"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RedshiftDeployAccess",
      "Effect": "Allow",
      "Action": ["redshift-serverless:*", "redshift-data:*"],
      "Resource": "*"
    },
    {
      "Sid": "SecretsManagerDeployAccess",
      "Effect": "Allow",
      "Action": ["secretsmanager:*"],
      "Resource": "arn:aws:secretsmanager:us-east-1:{account-id}:secret:*etl*"
    },
    {
      "Sid": "SNSSQSEventBridgeAccess",
      "Effect": "Allow",
      "Action": ["sns:*", "sqs:*", "events:*"],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchAccess",
      "Effect": "Allow",
      "Action": ["logs:*", "cloudwatch:*"],
      "Resource": "*"
    },
    {
      "Sid": "TerraformStateAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::etl-orchestrator-tfstate-*",
        "arn:aws:s3:::etl-orchestrator-tfstate-*/*"
      ]
    },
    {
      "Sid": "TerraformLockAccess",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem", "dynamodb:PutItem",
        "dynamodb:DeleteItem", "dynamodb:DescribeTable"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:{account-id}:table/etl-orchestrator-tflock-*"
    }
  ]
}
```

---

## 4. Generate Access Keys

Access keys are the credentials the AWS CLI and Terraform use to authenticate as your IAM user.

### Via AWS Console

1. IAM → Users → `etl-orchestrator-deployer` → **Security credentials** tab
2. Under **Access keys** → **Create access key**
3. Use case: **Command Line Interface (CLI)**
4. Download the `.csv` file or copy the keys — **the secret key is shown only once**

### Via AWS CLI

```bash
aws iam create-access-key --user-name etl-orchestrator-deployer
```

Output:
```json
{
  "AccessKey": {
    "UserName": "etl-orchestrator-deployer",
    "AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
    "Status": "Active",
    "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "CreateDate": "2024-01-15T00:00:00Z"
  }
}
```

> **Never share or commit access keys.** Treat them like passwords. Rotate them every 90 days. Delete keys that are no longer used.

---

## 5. Install the AWS CLI

### macOS

```bash
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "AWSCLIV2.pkg"
sudo installer -pkg AWSCLIV2.pkg -target /
aws --version
# aws-cli/2.x.x Python/3.x.x Darwin/...
```

### Linux

```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
aws --version
```

### Windows

Download and run the MSI installer from the AWS documentation, or use:
```powershell
msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2.msi
```

---

## 6. Configure AWS CLI Profiles

This project uses four environments: **dev, qa, staging, prod**. Configure a named profile for each so you never accidentally deploy to the wrong environment.

### Configure a Profile

```bash
aws configure --profile etl-dev
# AWS Access Key ID: AKIAIOSFODNN7EXAMPLE
# AWS Secret Access Key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
# Default region name: us-east-1
# Default output format: json
```

Repeat for each environment:
```bash
aws configure --profile etl-qa
aws configure --profile etl-staging
aws configure --profile etl-prod
```

### What Gets Written

The CLI writes to two files in your home directory:

**`~/.aws/credentials`** — stores the actual keys:
```ini
[etl-dev]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY

[etl-prod]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE2
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY2
```

**`~/.aws/config`** — stores region and output settings:
```ini
[profile etl-dev]
region = us-east-1
output = json

[profile etl-prod]
region = us-east-1
output = json
```

### Use a Profile

```bash
# One-off command
aws s3 ls --profile etl-dev

# Set for the current terminal session
export AWS_PROFILE=etl-dev

# Tell Terraform which profile to use
export AWS_PROFILE=etl-dev
terraform plan
```

---

## 7. Verify Your Setup

Run these checks before attempting a Terraform deployment.

```bash
# Who am I?
aws sts get-caller-identity --profile etl-dev
# Returns: Account ID, User ID, ARN of the authenticated user

# Can I list S3 buckets?
aws s3 ls --profile etl-dev

# Can I describe IAM roles?
aws iam list-roles --profile etl-dev --query 'Roles[?contains(RoleName, `etl`)].RoleName'

# Check the region is correct
aws configure get region --profile etl-dev
# Expected: us-east-1
```

Expected output from `get-caller-identity`:
```json
{
  "UserId": "AIDAIOSFODNN7EXAMPLE",
  "Account": "123456789012",
  "Arn": "arn:aws:iam::123456789012:user/etl-orchestrator-deployer"
}
```

---

## 8. Install Terraform

This project requires Terraform `>= 1.5` and AWS provider `~> 5.0`.

### macOS (Homebrew)

```bash
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
terraform --version
# Terraform v1.x.x
```

### Linux

```bash
wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor | \
  sudo tee /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
  https://apt.releases.hashicorp.com $(lsb_release -cs) main" | \
  sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install terraform
terraform --version
```

### Windows

```powershell
winget install HashiCorp.Terraform
```

---

## 9. Bootstrap Terraform State Backend

Before running `terraform init`, the S3 bucket and DynamoDB table for remote state must exist. These cannot be managed by Terraform itself (chicken-and-egg problem) — create them manually once per environment.

### What the Backend Does

| Resource | Purpose |
|----------|---------|
| S3 bucket | Stores the `terraform.tfstate` file — the source of truth for what Terraform has deployed |
| DynamoDB table | Provides state locking — prevents two people from running `terraform apply` simultaneously and corrupting state |

### Create Backend Resources (Dev Example)

```bash
export AWS_PROFILE=etl-dev
export AWS_REGION=us-east-1

# S3 bucket for state (versioning + encryption required)
aws s3api create-bucket \
  --bucket etl-orchestrator-tfstate-dev \
  --region $AWS_REGION

aws s3api put-bucket-versioning \
  --bucket etl-orchestrator-tfstate-dev \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket etl-orchestrator-tfstate-dev \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "AES256"
      }
    }]
  }'

# Block all public access
aws s3api put-public-access-block \
  --bucket etl-orchestrator-tfstate-dev \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# DynamoDB table for state locking (PAY_PER_REQUEST = no capacity planning needed)
aws dynamodb create-table \
  --table-name etl-orchestrator-tflock-dev \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region $AWS_REGION
```

Repeat with `-qa`, `-staging`, `-prod` suffixes for other environments.

### Backend Config (Already in This Repo)

`terraform/environments/dev/backend.tf`:
```hcl
terraform {
  backend "s3" {
    bucket         = "etl-orchestrator-tfstate-dev"
    key            = "etl-orchestrator/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "etl-orchestrator-tflock-dev"
    encrypt        = true
  }
}
```

---

## 10. Deploy the Pipeline

### First-Time Deployment (Dev)

```bash
cd terraform/environments/dev
export AWS_PROFILE=etl-dev

# Download providers and initialise the backend
terraform init

# Preview what will be created (no changes made)
terraform plan

# Deploy — type 'yes' when prompted
terraform apply
```

### Switching Environments

```bash
# QA
cd terraform/environments/qa
export AWS_PROFILE=etl-qa
terraform init && terraform plan && terraform apply

# Production (always plan and review before applying)
cd terraform/environments/prod
export AWS_PROFILE=etl-prod
terraform init
terraform plan -out=prod.tfplan   # save the plan
terraform apply prod.tfplan        # apply only the reviewed plan
```

### Destroy Resources

```bash
# Dev only — never run this in prod without approval
cd terraform/environments/dev
export AWS_PROFILE=etl-dev
terraform destroy
```

---

## 11. Environment Variables Reference

These variables are used by the AWS CLI, Terraform, and the application.

| Variable | What It Does | Example |
|----------|-------------|---------|
| `AWS_PROFILE` | Which named CLI profile to use | `etl-dev` |
| `AWS_ACCESS_KEY_ID` | Access key (overrides profile) | `AKIAIOSFODNN7EXAMPLE` |
| `AWS_SECRET_ACCESS_KEY` | Secret key (overrides profile) | `wJalrXUtn...` |
| `AWS_DEFAULT_REGION` | Default region (overrides profile config) | `us-east-1` |
| `AWS_SESSION_TOKEN` | Required when using temporary credentials (STS / assumed role) | `FwoGZXIv...` |
| `TF_VAR_*` | Pass any Terraform variable from the shell | `TF_VAR_environment=dev` |
| `TF_LOG` | Terraform debug logging level | `DEBUG`, `INFO`, `WARN`, `ERROR` |

> **Priority order for AWS credentials:**
> `AWS_ACCESS_KEY_ID` env var → `AWS_PROFILE` env var → `~/.aws/credentials` named profile → `default` profile → EC2/ECS instance role

---

## 12. Credentials Best Practices

| Practice | Why |
|----------|-----|
| **Never use root account keys** | Root has unrestricted access; a leaked root key compromises everything |
| **Never commit credentials to Git** | Add `*.tfvars`, `.env`, `~/.aws` to `.gitignore`; use `git-secrets` or `truffleHog` to scan |
| **Rotate access keys every 90 days** | Limits the window of exposure if a key is leaked |
| **Delete unused keys** | Each IAM user can have max 2 active keys; delete old ones immediately |
| **Use IAM roles instead of keys in CI/CD** | GitHub Actions, Jenkins, etc. can assume an IAM role via OIDC — no long-lived keys needed |
| **Enable MFA for console access** | Requires a second factor even if password is compromised |
| **Use least-privilege policies** | The deployer user should only have permissions to deploy this pipeline — not admin |
| **Never set `AWS_ACCESS_KEY_ID` in shell permanently** | Only set it for the duration of a session; prefer named profiles |
| **Use AWS Secrets Manager for app secrets** | Never hardcode Redshift passwords, SFTP credentials, or API keys in code |

---

## 13. Field Reference — Key Concepts

### IAM User vs IAM Role

| | IAM User | IAM Role |
|-|----------|---------|
| **What it is** | A permanent identity with long-lived credentials (username/password, access keys) | A temporary identity assumed by a service, user, or application |
| **Credentials** | Access key ID + Secret access key (permanent) | Temporary credentials via STS (expire in 15 min – 12 hours) |
| **Best for** | Human operators needing console/CLI access | AWS services (Lambda, Glue), CI/CD systems, cross-account access |
| **When to use** | Developer or admin needing to run CLI commands locally | Any service that needs AWS permissions (always prefer roles over keys for services) |

### Access Key vs Session Token

| | Access Key | Session Token (STS) |
|-|-----------|-------------------|
| **Issued by** | IAM (permanent) | AWS STS — Security Token Service (temporary) |
| **Expiry** | Never (until manually deleted or rotated) | 15 minutes to 36 hours |
| **Use case** | IAM user CLI access | Assumed role, MFA-protected API calls, cross-account access |
| **Components** | Access Key ID + Secret Access Key | Access Key ID + Secret Access Key + Session Token |

### `aws configure` vs `aws configure --profile`

```bash
# Writes to [default] profile — used when no profile is specified
aws configure

# Writes to a named profile — use this to avoid accidentally using the wrong account
aws configure --profile etl-prod
```

### Terraform State — Key Terms

| Term | What It Means |
|------|--------------|
| `terraform.tfstate` | JSON file tracking every resource Terraform manages. Treat it as the source of truth. |
| Remote state | Storing `tfstate` in S3 instead of locally — enables team collaboration and prevents state loss |
| State locking | DynamoDB table prevents concurrent `terraform apply` runs from corrupting state |
| `terraform init` | Downloads providers, configures the backend, and prepares the working directory |
| `terraform plan` | Shows what will be created, modified, or destroyed — always review before `apply` |
| `terraform apply` | Makes the changes described by the plan |
| `terraform destroy` | Destroys all resources managed by this state file |

---

## 14. Interview Tips

| Topic | What Interviewers Test |
|-------|----------------------|
| **Root vs IAM user** | Root cannot be restricted by IAM policies; always use IAM users or roles for day-to-day work |
| **Access keys vs IAM roles** | Access keys are for human CLI use; IAM roles are for services. Never put access keys inside Lambda, Glue, or EC2 — use roles |
| **MFA enforcement** | You can require MFA for sensitive API calls using an IAM policy `Condition` with `aws:MultiFactorAuthPresent: true` |
| **Terraform state locking** | Without DynamoDB locking, two concurrent `terraform apply` runs can corrupt state — common interview scenario |
| **Remote state vs local state** | Local state is lost if your laptop dies; remote state in S3 is shared and versioned — always use remote for team environments |
| **Credential priority order** | Env vars → named profile → default profile → instance role — knowing this helps debug auth issues |
| **`iam:PassRole`** | Required whenever one service needs to assign a role to another (e.g. Terraform assigning the Glue role to a Glue job). Missing this is the most common Terraform deployment error |
| **Least-privilege for deployer** | A CI/CD deployer should not have `AdministratorAccess` — scope it to the services it deploys |
| **OIDC for CI/CD** | GitHub Actions can assume an IAM role via OIDC without storing any access keys as secrets — the modern best practice |
| **State file sensitivity** | `terraform.tfstate` contains resource IDs, IP addresses, and sometimes plaintext secrets — always encrypt the S3 bucket and restrict access to it |
