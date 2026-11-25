from datetime import datetime, timedelta, timezone
import os
import secrets
import time
import uuid
import boto3

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ["TABLE_NAME"]
SENDER_EMAIL = os.environ["SENDER_EMAIL"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]
# Example: https://abc123xyz.execute-api.ap-southeast-2.amazonaws.com
API_BASE_URL = os.environ.get("API_BASE_URL", "https://example.com").rstrip("/")
SNAPSHOT_MAX_AGE_DAYS = int(os.environ.get("SNAPSHOT_MAX_AGE_DAYS", "60"))
TTL_SECONDS = int(os.environ.get("TTL_SECONDS", "86400"))

ec2 = boto3.client("ec2", region_name=REGION)
ses = boto3.client("ses", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)


def is_protected(tags: list[dict] | None) -> bool:
    tag_map = {t["Key"].strip().lower(): t["Value"].strip().lower() for t in (tags or [])}
    return tag_map.get("keep") == "true" or tag_map.get("protect") == "true"


def scan_stopped_instances() -> list[dict]:
    resp = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}])
    items = []
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            if is_protected(inst.get("Tags")):
                continue
            items.append({
                "resource_type": "ec2_stopped",
                "resource_id": inst["InstanceId"],
                "detail": f"Type={inst.get('InstanceType')}, State=stopped",
            })
    return items


def scan_available_volumes() -> list[dict]:
    resp = ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])
    items = []
    for vol in resp.get("Volumes", []):
        if is_protected(vol.get("Tags")):
            continue
        items.append({
            "resource_type": "ebs_volume",
            "resource_id": vol["VolumeId"],
            "detail": f"Size={vol.get('Size')}GB, Type={vol.get('VolumeType')}",
        })
    return items


def scan_unattached_eips() -> list[dict]:
    resp = ec2.describe_addresses()
    items = []
    for addr in resp.get("Addresses", []):
        if "AssociationId" in addr or is_protected(addr.get("Tags")):
            continue
        items.append({
            "resource_type": "eip",
            "resource_id": addr["AllocationId"],
            "detail": f"PublicIP={addr.get('PublicIp')}",
        })
    return items


def scan_old_snapshots() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_MAX_AGE_DAYS)
    images = ec2.describe_images(Owners=["self"]).get("Images", [])
    ami_snaps = {
        bdm["Ebs"]["SnapshotId"]
        for img in images
        for bdm in img.get("BlockDeviceMappings", [])
        if "Ebs" in bdm and "SnapshotId" in bdm["Ebs"]
    }
    snaps = ec2.describe_snapshots(OwnerIds=["self"]).get("Snapshots", [])
    items = []
    for snap in snaps:
        snap_id = snap["SnapshotId"]
        if snap_id in ami_snaps or is_protected(snap.get("Tags")):
            continue
        if snap["StartTime"] < cutoff:
            items.append({
                "resource_type": "ebs_snapshot",
                "resource_id": snap_id,
                "detail": f"Size={snap.get('VolumeSize')}GB, Started={snap['StartTime'].date()}",
            })
    return items


def scan_nat_gateways() -> list[dict]:
    resp = ec2.describe_nat_gateways()
    items = []
    for nat in resp.get("NatGateways", []):
        if nat.get("State") != "available" or is_protected(nat.get("Tags")):
            continue
        items.append({
            "resource_type": "nat_gateway",
            "resource_id": nat["NatGatewayId"],
            "detail": f"VpcId={nat.get('VpcId')}, State=available",
        })
    return items


def send_batch_email(batch_id: str, batch_token: str, records: list[dict]) -> None:
    review_url = f"{API_BASE_URL}/review?batch={batch_id}&token={batch_token}"
    summary_lines = [
        f"- [{r['resource_type']}] {r['resource_id']} ({r['detail']})"
        for r in records
    ]
    body_text = (
        f"Cloud Waste Cleaner found {len(records)} item(s) in {REGION}.\n\n"
        f"Select which ones to clean up here:\n{review_url}\n\n"
        f"Items in this scan:\n" + "\n".join(summary_lines)
    )
    ses.send_email(
        Source=SENDER_EMAIL,
        Destination={"ToAddresses": [RECIPIENT_EMAIL]},
        Message={
            "Subject": {"Data": f"Cloud Waste Selection ({REGION}): {len(records)} items"},
            "Body": {"Text": {"Data": body_text}},
        },
    )


def lambda_handler(event, context):
    findings = (
        scan_stopped_instances()
        + scan_available_volumes()
        + scan_unattached_eips()
        + scan_old_snapshots()
        + scan_nat_gateways()
    )
    now = int(time.time())
    expires_at = now + TTL_SECONDS
    batch_id = str(uuid.uuid4())
    batch_token = secrets.token_urlsafe(24)
    created_records = []

    for item in findings:
        record = {
            "cleanup_id": str(uuid.uuid4()),
            "batch_id": batch_id,
            "batch_token": batch_token,
            "resource_type": item["resource_type"],
            "resource_id": item["resource_id"],
            "detail": item["detail"],
            "region": REGION,
            "status": "PENDING_APPROVAL",
            "created_at": now,
            "expires_at": expires_at,
        }
        table.put_item(Item=record)
        created_records.append(record)

    if created_records:
        send_batch_email(batch_id, batch_token, created_records)

    return {
        "statusCode": 200,
        "region": REGION,
        "batch_id": batch_id,
        "findings_count": len(created_records),
    }