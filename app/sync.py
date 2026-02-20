import json
import os
import sys
import time
from datetime import datetime, timezone

import boto3
import requests

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "100"))
DDB_TABLE = os.getenv("DDB_TABLE", "bamboohr-moodle-sync-state")
STATE_ID = os.getenv("STATE_ID", "default")
BAMBOO_COMPANY_DOMAIN = os.getenv("BAMBOO_COMPANY_DOMAIN", "")
MOODLE_BASE_URL = os.getenv("MOODLE_BASE_URL", "")
BAMBOO_SECRET_ARN = os.getenv("BAMBOO_SECRET_ARN", "")
MOODLE_SECRET_ARN = os.getenv("MOODLE_SECRET_ARN", "")

DEFAULT_SINCE = "1970-01-01T00:00:00Z"


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_secret_json(client, arn):
    if not arn:
        return {}
    resp = client.get_secret_value(SecretId=arn)
    secret_string = resp.get("SecretString") or "{}"
    try:
        return json.loads(secret_string)
    except json.JSONDecodeError:
        return {}


def get_state(ddb):
    resp = ddb.get_item(TableName=DDB_TABLE, Key={"StateId": {"S": STATE_ID}})
    item = resp.get("Item")
    if not item:
        return {"since": DEFAULT_SINCE, "offset": 0}
    since = item.get("since", {}).get("S", DEFAULT_SINCE)
    offset = int(item.get("offset", {}).get("N", "0"))
    return {"since": since, "offset": offset}


def put_state(ddb, since, offset):
    ddb.put_item(
        TableName=DDB_TABLE,
        Item={
            "StateId": {"S": STATE_ID},
            "since": {"S": since},
            "offset": {"N": str(offset)},
            "updatedAt": {"S": utc_now_iso()},
        },
    )


def fetch_bamboo_changes(since, api_key):
    # BambooHR /employees/changed commonly returns XML.
    url = f"https://{BAMBOO_COMPANY_DOMAIN}.bamboohr.com/api/v1/employees/changed/"
    resp = requests.get(url, params={"since": since}, auth=(api_key, "x"), timeout=30)

    ct = resp.headers.get("content-type", "")
    print("BAMBOO status:", resp.status_code, "content-type:", ct)
    head = (resp.text or "")[:300].replace("\n", " ")
    print("BAMBOO body head:", head)

    resp.raise_for_status()
    body = resp.text or ""

    # XML path
    if "xml" in ct.lower() or body.lstrip().startswith("<?xml"):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(body)
        latest = root.attrib.get("latest") or root.attrib.get("lastChanged") or ""
        employees = []
        for e in root.findall(".//employee"):
            employees.append({
                "id": e.attrib.get("id"),
                "action": e.attrib.get("action"),
                "lastChanged": e.attrib.get("lastChanged")
            })
        return {"employees": employees, "lastChanged": latest}

    # JSON path (if Bamboo ever returns JSON here)
    if "json" in ct.lower():
        return resp.json()

    raise ValueError(f"Unexpected content-type: {ct}")




def extract_changes(payload):
    # BambooHR commonly returns "employees" as an array. This is intentionally defensive.
    employees = payload.get("employees") or payload.get("changes") or []
    if not isinstance(employees, list):
        employees = []
    return employees


def extract_latest(payload, fallback_since):
    for key in ("lastChanged", "last_changed", "latest", "timestamp"):
        if key in payload:
            return str(payload[key])
    return fallback_since


def process_moodle_record(record, moodle_token):
    # Placeholder integration. Replace with actual Moodle API call(s).
    # Return True on success, False on error.
    if not MOODLE_BASE_URL or not moodle_token:
        return False
    # Example no-op: simulate success without network calls.
    return True


def main():
    started_at = utc_now_iso()
    errors = 0

    ddb = boto3.client("dynamodb")
    secrets = boto3.client("secretsmanager")

    bamboo_secret = get_secret_json(secrets, BAMBOO_SECRET_ARN)
    moodle_secret = get_secret_json(secrets, MOODLE_SECRET_ARN)

    bamboo_api_key = bamboo_secret.get("api_key") or bamboo_secret.get("token") or ""
    moodle_token = moodle_secret.get("token") or moodle_secret.get("api_token") or ""

    state = get_state(ddb)
    since = state["since"]
    offset = state["offset"]

    total_changed = 0
    processed = 0
    next_since = since
    next_offset = offset

    try:
        payload = fetch_bamboo_changes(since, bamboo_api_key)
        changes = extract_changes(payload)
        latest = extract_latest(payload, since)
        total_changed = len(changes)

        if total_changed == 0:
            next_since = latest
            next_offset = 0
        else:
            batch = changes[offset : offset + BATCH_SIZE]
            for record in batch:
                ok = process_moodle_record(record, moodle_token)
                if ok:
                    processed += 1
                else:
                    errors += 1
            if offset + BATCH_SIZE < total_changed:
                next_since = since
                next_offset = offset + BATCH_SIZE
            else:
                next_since = latest
                next_offset = 0

        put_state(ddb, next_since, next_offset)

    except Exception as e:
          errors += 1
          import traceback
          print("ERROR:", repr(e))
          traceback.print_exc()

    summary = {
        "started_at": started_at,
        "since": since,
        "offset": offset,
        "batch_size": BATCH_SIZE,
        "total_changed": total_changed,
        "processed": processed,
        "errors": errors,
        "next_since": next_since,
        "next_offset": next_offset,
    }

    print(json.dumps(summary, sort_keys=True))
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
