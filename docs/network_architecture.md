# Network Architecture

This document describes how AWS services in the ETL orchestration pipeline communicate, what network components are (and are not) used today, and what a production-hardened network design would look like.

---

## Current State: No Customer-Managed VPC

The current Terraform configuration deploys **zero VPC resources**. All services rely on AWS-managed networking or public service endpoints.

| Service | Current Network Mode | Why No VPC Needed Today |
|---------|---------------------|------------------------|
| Lambda (Orchestrator) | Public — no VPC config | Calls AWS service APIs over HTTPS (SQS, SNS, Step Functions, S3) |
| Lambda (SFTP) | Public — no VPC config | Calls S3 and Secrets Manager over HTTPS |
| Glue (ETL Jobs) | Public — no VPC config | Reads S3 and calls Redshift Data API over HTTPS |
| Redshift Serverless | AWS-managed private VPC | `publicly_accessible = false`; AWS manages the underlying VPC |
| Step Functions | AWS-managed (serverless) | Fully managed — no network config required |
| S3, SQS, SNS, EventBridge | AWS-managed (serverless) | Regional service endpoints — no VPC needed |

> **Key point:** AWS managed services (S3, SQS, SNS, EventBridge, Step Functions) are accessed via regional HTTPS endpoints. They do not live inside any VPC — they are outside the VPC by nature.

---

## How Services Connect Today (No VPC)

```
Internet / AWS Backbone
        │
        ├── S3 (regional endpoint)
        │       │
        │   EventBridge (S3 ObjectCreated event)
        │       │
        │      SQS FIFO Queue (resource-based policy)
        │       │
        ├── Lambda Orchestrator (public, no VPC)
        │       │
        │   ├── SNS Topic ──► Email / Alerts
        │   │
        │   └── Step Functions State Machine
        │               │
        │           Glue ETL Job (public, no VPC)
        │               │
        │           ├── S3 (read source / write output)
        │           │
        │           └── Redshift Serverless
        │               (AWS-managed private VPC, publicly_accessible=false)
        │
        └── Lambda SFTP (public, no VPC)
                │
            ├── S3 logs/ prefix
            └── Secrets Manager (SFTP credentials)
```

---

## Network Components — Field Reference

Understanding what each component does is essential for interviews.

### VPC (Virtual Private Cloud)

| Field | What It Means |
|-------|--------------|
| **VPC** | A logically isolated virtual network inside AWS. You define the IP range (CIDR block), subnets, routing, and gateways. Everything inside a VPC is private by default. |
| **CIDR block** | The IP address range for the VPC, e.g. `10.0.0.0/16` (65,536 addresses). Subnets carve out ranges within this, e.g. `10.0.1.0/24` (256 addresses). |
| **Default VPC** | AWS creates one per region automatically. Suitable for development; avoid for production ETL pipelines handling sensitive data. |

### Subnets

| Type | What It Means |
|------|--------------|
| **Public subnet** | Has a route to an Internet Gateway. Resources here can have public IPs and receive inbound internet traffic. Used for NAT Gateways, load balancers. |
| **Private subnet** | No direct internet route. Resources here cannot be reached from the internet. Used for Lambda, Glue, Redshift when inside a VPC. Internet-bound traffic routes through a NAT Gateway. |
| **Multi-AZ** | Best practice: deploy subnets across at least 2 Availability Zones for high availability. |

### Internet Gateway (IGW)

| Field | What It Means |
|-------|--------------|
| **Internet Gateway** | Attached to the VPC; allows resources in public subnets to send and receive traffic from the internet. One per VPC. |
| **Route** | Public subnet route table has `0.0.0.0/0 → IGW`, enabling outbound internet access. |

### NAT Gateway

| Field | What It Means |
|-------|--------------|
| **NAT Gateway** | Lives in a **public** subnet; allows resources in **private** subnets to initiate outbound internet connections (e.g. download libraries, call external APIs) without being reachable from the internet. |
| **Route** | Private subnet route table has `0.0.0.0/0 → NAT Gateway`. |
| **Cost note** | NAT Gateways are charged per hour + per GB of data processed. VPC Endpoints eliminate this cost for AWS service traffic. |

### Security Groups

| Field | What It Means |
|-------|--------------|
| **Security group** | A stateful virtual firewall attached to an ENI (network interface). Controls inbound and outbound traffic at the resource level. |
| **Stateful** | If inbound traffic is allowed, the response is automatically allowed outbound (no explicit outbound rule needed). |
| **Inbound rule** | Allows traffic coming *into* the resource. Specify: protocol, port range, source (CIDR or another security group). |
| **Outbound rule** | Allows traffic going *out* from the resource. Default: allow all. |
| **Reference by SG ID** | Security groups can reference each other as source/destination — tighter than CIDR ranges. |

### Route Tables

| Field | What It Means |
|-------|--------------|
| **Route table** | A set of rules (routes) that determine where network traffic is directed. Each subnet is associated with one route table. |
| **Local route** | `10.0.0.0/16 → local` — always present; allows all resources within the VPC to communicate with each other. |
| **Default route** | `0.0.0.0/0 → target` — where to send traffic that doesn't match any other route. Target is IGW (public) or NAT (private). |

### VPC Endpoints

| Type | What It Means |
|------|--------------|
| **Gateway endpoint** | Free. Supported only for S3 and DynamoDB. Adds an entry to the route table — traffic stays entirely on the AWS network, never leaves via the internet or NAT Gateway. |
| **Interface endpoint** | Paid (per-hour + per-GB). Creates a private IP (ENI) inside your subnet for the target service (e.g. Secrets Manager, Glue, Step Functions, SQS, SNS). Traffic never leaves the AWS network. |
| **Why use them** | Security: traffic stays private. Cost: eliminates NAT Gateway data charges for AWS service calls. Compliance: required for environments that must not route traffic over the internet. |

---

## Recommended Production Network Design

For a production ETL pipeline handling financial or sensitive data, the architecture should move into a VPC with private subnets and VPC endpoints. This eliminates public exposure and NAT Gateway costs for AWS service traffic.

### Target Architecture Diagram

```
VPC: 10.0.0.0/16
│
├── Availability Zone A
│   ├── Public Subnet 10.0.1.0/24
│   │   └── NAT Gateway A
│   │
│   └── Private Subnet 10.0.2.0/24
│       ├── Lambda (Orchestrator)  ─┐
│       ├── Lambda (SFTP)          ─┤── Security Group: lambda-sg
│       └── Glue ETL Jobs          ─┘
│
├── Availability Zone B
│   ├── Public Subnet 10.0.3.0/24
│   │   └── NAT Gateway B (standby)
│   │
│   └── Private Subnet 10.0.4.0/24
│       └── Redshift Serverless ─── Security Group: redshift-sg
│
└── VPC Endpoints (interface, in private subnets)
    ├── com.amazonaws.{region}.s3              (Gateway — free)
    ├── com.amazonaws.{region}.secretsmanager  (Interface)
    ├── com.amazonaws.{region}.sqs             (Interface)
    ├── com.amazonaws.{region}.sns             (Interface)
    ├── com.amazonaws.{region}.states          (Interface — Step Functions)
    ├── com.amazonaws.{region}.glue            (Interface)
    └── com.amazonaws.{region}.logs            (Interface — CloudWatch Logs)
```

---

### Recommended Security Group Rules

#### `lambda-sg` (Lambda Orchestrator + SFTP)

| Direction | Protocol | Port | Source / Destination | Reason |
|-----------|----------|------|---------------------|--------|
| Outbound | TCP | 443 | VPC Endpoint SGs | Call AWS APIs (S3, SQS, SNS, Step Functions, Secrets Manager) |
| Outbound | TCP | 5439 | `redshift-sg` | Redshift Data API (if using direct JDBC instead of Data API) |
| Inbound | — | — | — | No inbound needed (Lambda is not a server) |

#### `glue-sg` (Glue ETL Jobs)

| Direction | Protocol | Port | Source / Destination | Reason |
|-----------|----------|------|---------------------|--------|
| Outbound | TCP | 443 | VPC Endpoint SGs | Call S3, Secrets Manager, Redshift Data API, CloudWatch |
| Outbound | TCP | 5439 | `redshift-sg` | Direct Redshift connection if needed |
| Outbound | TCP | Self | `glue-sg` | Glue requires self-referencing rule for Spark driver/executor communication |
| Inbound | TCP | Self | `glue-sg` | Same self-referencing rule (required by Glue) |

> **Glue self-referencing rule:** Glue Spark jobs use multiple workers that communicate with each other. AWS requires a security group rule that allows all inbound/outbound traffic from the same security group.

#### `redshift-sg` (Redshift Serverless)

| Direction | Protocol | Port | Source / Destination | Reason |
|-----------|----------|------|---------------------|--------|
| Inbound | TCP | 5439 | `glue-sg` | Accept connections from Glue jobs |
| Inbound | TCP | 5439 | `lambda-sg` | Accept connections from Lambda (if using direct connection) |
| Outbound | TCP | 443 | VPC Endpoint SGs | Read from S3 for COPY operations |

---

### VPC Endpoints Explained for This Architecture

| Endpoint | Type | Service It Covers | Why Needed |
|----------|------|------------------|-----------|
| S3 Gateway | Gateway (free) | `s3:GetObject`, `s3:PutObject` | Lambda and Glue read/write S3 without NAT Gateway |
| Secrets Manager | Interface | `secretsmanager:GetSecretValue` | Lambda SFTP and Glue fetch credentials privately |
| SQS | Interface | `sqs:ReceiveMessage`, `sqs:DeleteMessage` | Lambda Orchestrator consumes queue privately |
| SNS | Interface | `sns:Publish` | Lambda Orchestrator sends alerts privately |
| Step Functions | Interface | `states:StartExecution` | Lambda triggers state machine privately |
| Glue | Interface | Glue job APIs | Step Functions calls Glue privately |
| CloudWatch Logs | Interface | `logs:PutLogEvents` | All services write logs privately |

---

### Route Table Design

#### Public Subnet Route Table

| Destination | Target | Purpose |
|-------------|--------|---------|
| `10.0.0.0/16` | local | VPC-internal traffic |
| `0.0.0.0/0` | Internet Gateway | Outbound internet (for NAT Gateway itself) |

#### Private Subnet Route Table

| Destination | Target | Purpose |
|-------------|--------|---------|
| `10.0.0.0/16` | local | VPC-internal traffic |
| `pl-XXXXXXXX` (S3 prefix list) | S3 Gateway Endpoint | S3 traffic stays on AWS network |
| `0.0.0.0/0` | NAT Gateway | Any remaining outbound internet traffic |

> **Prefix list:** AWS maintains managed prefix lists for S3 and DynamoDB gateway endpoints. Using these in route tables automatically routes traffic to the endpoint without specifying IPs.

---

## Current vs Recommended Comparison

| Component | Current State | Recommended Production |
|-----------|--------------|----------------------|
| VPC | None — public endpoints | Custom VPC `10.0.0.0/16` |
| Subnets | None | 2 public + 2 private across 2 AZs |
| Lambda network | Public (no VPC) | Private subnet, no public IP |
| Glue network | Public (no VPC) | Private subnet, self-referencing SG |
| Redshift | `publicly_accessible=false`, AWS-managed VPC | Private subnet, customer-managed SG |
| Internet Gateway | None | 1 per VPC (for NAT Gateways) |
| NAT Gateway | None | 1 per AZ for high availability |
| S3 VPC Endpoint | None | Gateway endpoint (free, eliminates NAT cost) |
| Service VPC Endpoints | None | Interface endpoints for Secrets Manager, SQS, SNS, Step Functions, Glue, CloudWatch |
| Security Groups | None | Separate SGs per service with least-privilege rules |
| Route Tables | None | Public + private route tables per AZ |

---

## Interview Tips

| Topic | What Interviewers Test |
|-------|----------------------|
| **Public vs private subnet** | Public has IGW route; private routes through NAT. Resources in private subnets can initiate outbound but cannot receive inbound from internet. |
| **NAT Gateway vs Internet Gateway** | IGW is for two-way internet access (public subnet). NAT is for outbound-only from private subnets. NAT lives in a public subnet. |
| **Gateway vs Interface VPC Endpoint** | Gateway: free, only S3 and DynamoDB, modifies route table. Interface: paid, any AWS service, creates an ENI with a private IP in your subnet. |
| **Glue self-referencing security group** | Glue Spark workers communicate peer-to-peer; the SG must allow traffic from itself. Missing this rule is the most common Glue networking mistake. |
| **`publicly_accessible = false` on Redshift** | Prevents internet access to Redshift but does NOT put it in your VPC — Redshift Serverless manages its own internal VPC. You still need VPC config for your tools (Glue, Lambda) to reach it. |
| **Why VPC Endpoints save money** | Without them, Lambda/Glue in a private subnet must route AWS API calls through NAT Gateway — charged per GB. VPC endpoints keep that traffic on the AWS backbone for free (Gateway) or a flat hourly rate (Interface) that is usually cheaper than NAT data charges at scale. |
| **Security group statefulness** | If you allow inbound TCP 443, the response traffic is automatically allowed outbound — you do not need a separate outbound rule for it. NACLs are stateless and do require both directions. |
| **AZ-redundant NAT** | One NAT Gateway per AZ — if you use a single NAT and that AZ goes down, all private subnet resources lose internet access. |
