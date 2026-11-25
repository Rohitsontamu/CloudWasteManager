import base64
import hmac
import html
import os
import time
from urllib.parse import parse_qs
import boto3
from boto3.dynamodb.conditions import Attr

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.environ["TABLE_NAME"]

ec2 = boto3.client("ec2", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)


def _html_page(status_code: int, title: str, body_html: str) -> dict:
    doc = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 720px; margin: 32px auto; padding: 0 16px; }}
    .item {{ border: 1px solid #ddd; border-radius: 8px; padding: 12px; margin-bottom: 10px; }}
    label {{ display: flex; gap: 10px; align-items: flex-start; cursor: pointer; }}
    button {{ background: #d9381e; color: #fff; border: 0; padding: 12px 20px; border-radius: 6px; font-size: 15px; cursor: pointer; }}
    .meta {{ color: #555; font-size: 13px; margin-top: 4px; }}
  </style>
</head>
<body>
  <h2>{html.escape(title)}</h2>
  {body_html}
</body>
</html>"""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": doc,
    }


def _has_keep_tag(tags: list[dict] | None) -> bool:
    tag_map = {t["Key"].strip().lower(): t["Value"].strip().lower() for t in (tags or [])}
    return tag_map.get("keep") == "true" or tag_map.get("protect") == "true"


def _cleanup_single(resource_type: str, resource_id: str) -> str:
    if resource_type in ("ec2_stopped", "ec2_instance"):
        resp = ec2.describe_instances(InstanceIds=[resource_id])
        reservations = resp.get("Reservations", [])
        if reservations and reservations[0].get("Instances"):
            inst = reservations[0]["Instances"][0]
            if _has_keep_tag(inst.get("Tags")):
                raise RuntimeError("Protected by Keep=true / Protect=true tag.")
        ec2.terminate_instances(InstanceIds=[resource_id])
        return f"Terminated (deleted) EC2 instance {resource_id}"

    if resource_type == "ebs_volume":
        resp = ec2.describe_volumes(VolumeIds=[resource_id])
        vols = resp.get("Volumes", [])
        if vols and _has_keep_tag(vols[0].get("Tags")):
            raise RuntimeError("Protected by Keep=true / Protect=true tag.")
        ec2.delete_volume(VolumeId=resource_id)
        return f"Deleted EBS volume {resource_id}"

    if resource_type == "eip":
        resp = ec2.describe_addresses(AllocationIds=[resource_id])
        addrs = resp.get("Addresses", [])
        if addrs and _has_keep_tag(addrs[0].get("Tags")):
            raise RuntimeError("Protected by Keep=true / Protect=true tag.")
        ec2.release_address(AllocationId=resource_id)
        return f"Released EIP allocation {resource_id}"

    if resource_type == "ebs_snapshot":
        resp = ec2.describe_snapshots(SnapshotIds=[resource_id])
        snaps = resp.get("Snapshots", [])
        if snaps and _has_keep_tag(snaps[0].get("Tags")):
            raise RuntimeError("Protected by Keep=true / Protect=true tag.")
        ec2.delete_snapshot(SnapshotId=resource_id)
        return f"Deleted EBS snapshot {resource_id}"

    if resource_type == "nat_gateway":
        resp = ec2.describe_nat_gateways(NatGatewayIds=[resource_id])
        nats = resp.get("NatGateways", [])
        if nats and _has_keep_tag(nats[0].get("Tags")):
            raise RuntimeError("Protected by Keep=true / Protect=true tag.")
        ec2.delete_nat_gateway(NatGatewayId=resource_id)
        return f"Deleted NAT Gateway {resource_id}"

    raise ValueError(f"Unsupported resource_type: {resource_type}")

def _get_batch_items(batch_id: str) -> list[dict]:
    items = []
    scan_kwargs = {"FilterExpression": Attr("batch_id").eq(batch_id)}
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def _parse_form_body(event: dict) -> dict[str, list[str]]:
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8", errors="replace")
    return parse_qs(raw_body, keep_blank_values=False)


def _handle_review(event: dict) -> dict:
    qs = event.get("queryStringParameters") or {}
    batch_id = (qs.get("batch") or "").strip()
    token = (qs.get("token") or "").strip()
    if not batch_id or not token:
        return _html_page(400, "Invalid Link", "<p>Missing batch or token parameter.</p>")

    items = _get_batch_items(batch_id)
    if not items:
        return _html_page(404, "Not Found", "<p>No cleanup batch found or batch expired.</p>")

    expected_token = items[0].get("batch_token", "")
    if not hmac.compare_digest(token, expected_token):
        return _html_page(403, "Forbidden", "<p>Invalid batch token.</p>")

    now = int(time.time())
    pending = [
        it for it in items
        if it.get("status") == "PENDING_APPROVAL" and int(it.get("expires_at", 0)) >= now
    ]
    if not pending:
        return _html_page(200, "Nothing Pending", "<p>All items in this batch were already processed or expired.</p>")

    rows = []
    for it in pending:
        cid = html.escape(it["cleanup_id"])
        rtype = html.escape(it["resource_type"])
        rid = html.escape(it["resource_id"])
        detail = html.escape(it.get("detail", ""))
        rows.append(
            f'<div class="item"><label>'
            f'<input type="checkbox" name="selected_id" value="{cid}">'
            f'<div><strong>[{rtype}] {rid}</strong><div class="meta">{detail}</div></div>'
            f'</label></div>'
        )

    form_html = (
        f'<form method="POST" action="../clean">'
        f'<input type="hidden" name="batch" value="{html.escape(batch_id)}">'
        f'<input type="hidden" name="token" value="{html.escape(token)}">'
        f'{"".join(rows)}'
        f'<p><button type="submit">Clean Selected Resources</button></p>'
        f'</form>'
    )
    return _html_page(200, f"Select Resources to Clean ({REGION})", form_html)


def _handle_clean(event: dict) -> dict:
    form = _parse_form_body(event)
    batch_id = (form.get("batch", [""])[0] or "").strip()
    token = (form.get("token", [""])[0] or "").strip()
    selected_ids = list(dict.fromkeys(form.get("selected_id", [])))

    if not batch_id or not token:
        return _html_page(400, "Invalid Submission", "<p>Missing batch or token.</p>")
    if not selected_ids:
        return _html_page(200, "No Resources Selected", "<p>Nothing was selected—no resources were modified.</p>")

    items = _get_batch_items(batch_id)
    if not items:
        return _html_page(404, "Not Found", "<p>Batch not found or expired.</p>")

    expected_token = items[0].get("batch_token", "")
    if not hmac.compare_digest(token, expected_token):
        return _html_page(403, "Forbidden", "<p>Invalid batch token.</p>")

    by_id = {it["cleanup_id"]: it for it in items}
    now = int(time.time())
    results = []

    for cid in selected_ids:
        it = by_id.get(cid)
        if not it:
            results.append(f"<li>{html.escape(cid)}: skipped (not in batch)</li>")
            continue
        if it.get("status") != "PENDING_APPROVAL":
            results.append(f"<li>{html.escape(it['resource_id'])}: skipped (status {html.escape(it.get('status', ''))})</li>")
            continue
        if int(it.get("expires_at", 0)) < now:
            results.append(f"<li>{html.escape(it['resource_id'])}: skipped (expired)</li>")
            continue

        try:
            msg = _cleanup_single(it["resource_type"], it["resource_id"])
            table.update_item(
                Key={"cleanup_id": cid},
                UpdateExpression="SET #st = :st, executed_at = :ts, result_detail = :msg",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":st": "EXECUTED", ":ts": now, ":msg": msg},
            )
            results.append(f"<li><strong>Success:</strong> {html.escape(msg)}</li>")
        except Exception as exc:
            results.append(f"<li><strong>Failed:</strong> {html.escape(it['resource_id'])} — {html.escape(str(exc))}</li>")

    return _html_page(200, "Cleanup Results", f"<ul>{''.join(results)}</ul>")


def lambda_handler(event, context):
    raw_path = event.get("rawPath") or event.get("path") or ""
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()

    if method == "POST" or raw_path.endswith("/clean"):
        return _handle_clean(event)
    return _handle_review(event)