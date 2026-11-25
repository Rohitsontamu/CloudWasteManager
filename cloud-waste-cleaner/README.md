# Cloud Waste Cleaner

A serverless AWS FinOps automation tool built in `ap-southeast-2` (Sydney) that scans idle and orphaned cloud resources on a schedule, sends a single batch review link via email, and lets you select specific resources via checkboxes before cleaning them up.

---

## Why This Project Exists

Idle and unattached AWS resources quietly drain budget—especially unattached EBS volumes, unattached Elastic IPs, old EBS snapshots, available NAT Gateways, and EC2 instances left running with minimal CPU usage. **Cloud Waste Cleaner** automates discovery without automating blind deletion: a human reviews the findings in a browser and explicitly selects what gets cleaned up.

---

## What It Scans & How It Cleans Up

| Resource | Detection Logic | Cleanup Action |
| :--- | :--- | :--- |
| **Stopped EC2 Instances** | Instances in `stopped` state (unless protected by tags) | Terminates (`terminate_instances`) when approved |
| **Idle Running EC2 Instances** | Instances in `running` state whose CloudWatch `CPUUtilization` average over `IDLE_LOOKBACK_HOURS` is below `IDLE_CPU_THRESHOLD` | Terminates (`terminate_instances`) when approved |
| **Unattached EBS Volumes** | Volumes in `available` state | Deletes (`delete_volume`) when approved |
| **Unattached Elastic IPs** | Allocated addresses without an `AssociationId` | Releases (`release_address`) when approved |
| **Old EBS Snapshots** | Snapshots older than `SNAPSHOT_MAX_AGE_DAYS` that are not referenced by owned AMIs | Deletes (`delete_snapshot`) when approved |
| **Available NAT Gateways** | NAT Gateways in `available` state | Deletes (`delete_nat_gateway`) when approved |

---

## How the Workflow Operates

1. **Scheduled or Manual Trigger:** EventBridge (`WeeklyMondayCloudWasteScan`) invokes `CloudWasteScanner` on schedule (or you can invoke it on demand for testing).
2. **Discovery & Tag Guard:** `CloudWasteScanner` queries EC2 and CloudWatch in `ap-southeast-2` and skips any resource tagged with `Keep=true` or `Protect=true` (case-insensitive).
3. **Batch State & Email Approval:** Each scan generates a `batch_id` and `batch_token`, stores individual finding records in DynamoDB (`CloudWasteCleanup`) with a TTL (`expires_at`), and emails a single `/review?batch=...&token=...` link via Amazon SES.
4. **Multi-Select Review (`GET /review`):** `CloudWasteExecutor` validates the batch token, filters for `PENDING_APPROVAL` non-expired items, and renders an HTML checkbox list.
5. **Selective Execution (`POST /clean`):** Only checked `cleanup_id`s are processed; each item's token, status, TTL, and live protection tags are re-checked before execution, and completed items are marked `EXECUTED` in DynamoDB.

---

## AWS Components Used

- **Amazon EventBridge:** Schedule trigger (`WeeklyMondayCloudWasteScan`)
- **AWS Lambda (`Python 3.12`):** `CloudWasteScanner` and `CloudWasteExecutor`
- **Amazon DynamoDB:** `CloudWasteCleanup` state table (`cleanup_id` partition key, TTL on `expires_at`)
- **Amazon API Gateway (HTTP API):** `CloudWasteApprovalAPI` exposing `GET /review` and `POST /clean`
- **Amazon SES:** Sends scan summary and review link emails
- **Amazon CloudWatch:** Provides `AWS/EC2` `CPUUtilization` metrics and Lambda execution logs
- **AWS IAM:** Least-privilege roles (`CloudWasteScannerRole`, `CloudWasteExecutorRole`) separating discovery/reporting from cleanup execution

---

## Safety & Cost Design

- **Human-in-the-loop approval:** Nothing is terminated, deleted, or released automatically by the scanner; approval links expire via DynamoDB TTL and cannot be re-executed once `EXECUTED`.
- **Tag protection:** Tagging resources with `Keep=true` or `Protect=true` prevents accidental inclusion or cleanup.
- **Low/serverless footprint:** Uses pay-per-request / free-tier-friendly serverless services (Lambda, DynamoDB, API Gateway, EventBridge, SES) with no always-on server required for the cleaner itself.
- **Destructive action warning:** Terminating EC2 instances or deleting EBS volumes, snapshots, or NAT Gateways is irreversible—always verify checkboxes before submitting.

---

## Repository Layout

```text
cloud-waste-cleaner/
├── README.md
├── .gitignore
├── iam/
│   ├── scanner-policy.json
│   └── executor-policy.json
└── lambdas/
    ├── scanner/
    │   └── lambda_function.py
    └── executor/
        └── lambda_function.py
```

---

## Configuration Reference

### `CloudWasteScanner` Environment Variables
- `TABLE_NAME` — DynamoDB table name (`CloudWasteCleanup`)
- `SENDER_EMAIL` — Verified SES sender address
- `RECIPIENT_EMAIL` — Destination email for scan reports
- `API_BASE_URL` — Base Invoke URL of `CloudWasteApprovalAPI` (without trailing slash)
- `SNAPSHOT_MAX_AGE_DAYS` — Age cutoff in days for old snapshots (default: `60`)
- `IDLE_CPU_THRESHOLD` — Average CPU % threshold for idle running EC2 instances (default: `5.0`)
- `IDLE_LOOKBACK_HOURS` — Lookback window in hours for CloudWatch CPU checks (default: `24`)
- `TTL_SECONDS` — Approval link lifetime in seconds (default: `86400`)

### `CloudWasteExecutor` Environment Variables
- `TABLE_NAME` — DynamoDB table name (`CloudWasteCleanup`)