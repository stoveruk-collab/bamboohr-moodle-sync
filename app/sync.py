import json
import os
import re
import secrets
import string
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import boto3
import requests

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def env_int(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


BATCH_SIZE = env_int("BATCH_SIZE", 100)
INITIAL_LOOKBACK_DAYS = env_int("INITIAL_LOOKBACK_DAYS", 14)
HTTP_TIMEOUT_SECONDS = env_int("HTTP_TIMEOUT_SECONDS", 30)

DDB_TABLE = os.getenv("DDB_TABLE", "bamboohr-moodle-sync-state")
STATE_ID = os.getenv("STATE_ID", "default")
BAMBOO_COMPANY_DOMAIN = os.getenv("BAMBOO_COMPANY_DOMAIN", "")
MOODLE_BASE_URL = os.getenv("MOODLE_BASE_URL", "")
BAMBOO_SECRET_ARN = os.getenv("BAMBOO_SECRET_ARN", "")
MOODLE_SECRET_ARN = os.getenv("MOODLE_SECRET_ARN", "")
MOODLE_AUTH = os.getenv("MOODLE_AUTH", "oidc")
MOODLE_DEFAULT_INSTITUTION = os.getenv("MOODLE_DEFAULT_INSTITUTION", "")


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_since_iso():
    ts = datetime.now(timezone.utc) - timedelta(days=max(0, INITIAL_LOOKBACK_DAYS))
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        return {"since": default_since_iso(), "offset": 0}
    since = item.get("since", {}).get("S", default_since_iso())
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


def first_non_empty(*values):
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return ""


def split_name(display_name):
    dn = str(display_name or "").strip()
    if not dn:
        return "Unknown", "User"
    parts = dn.split()
    if len(parts) == 1:
        return parts[0], "User"
    return parts[0], parts[-1]


def gen_password(length=20):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    pwd = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*()-_=+"),
    ]
    pwd += [secrets.choice(alphabet) for _ in range(max(4, length) - 4)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


def safe_username_from_empid(emp_id):
    return f"bamboo_{emp_id}"


def fetch_bamboo_changes(since, api_key):
    url = f"https://{BAMBOO_COMPANY_DOMAIN}.bamboohr.com/api/v1/employees/changed"
    resp = requests.get(
        url,
        params={"since": since},
        headers={"Accept": "application/xml"},
        auth=(api_key, "x"),
        timeout=HTTP_TIMEOUT_SECONDS,
    )

    content_type = resp.headers.get("content-type", "")
    print("BAMBOO status:", resp.status_code, "content-type:", content_type)
    head = (resp.text or "")[:300].replace("\n", " ")
    print("BAMBOO body head:", head)

    resp.raise_for_status()
    text = resp.text or ""

    if "xml" in content_type.lower() or text.lstrip().startswith("<?xml"):
        root = ET.fromstring(text)
        latest = root.attrib.get("latest") or root.attrib.get("lastChanged") or since
        employees = []
        for employee in root.findall(".//employee"):
            employee_id = (employee.attrib.get("id") or "").strip()
            if not employee_id:
                continue
            employees.append(
                {
                    "id": employee_id,
                    "action": employee.attrib.get("action") or "Updated",
                    "lastChanged": employee.attrib.get("lastChanged") or "",
                }
            )
        return {"employees": employees, "latest": latest}

    if "json" in content_type.lower():
        payload = resp.json()
        employees = payload.get("employees") or payload.get("changes") or []
        latest = payload.get("latest") or payload.get("lastChanged") or since
        return {"employees": employees, "latest": str(latest)}

    raise ValueError(f"Unexpected content-type from Bamboo changed endpoint: {content_type}")


def bamboo_directory(api_key):
    url = f"https://api.bamboohr.com/api/gateway.php/{BAMBOO_COMPANY_DOMAIN}/v1/employees/directory"
    resp = requests.get(
        url,
        headers={"Accept": "application/xml"},
        auth=(api_key, "x"),
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.text or "")

    fid_to_name = {}
    for field in root.findall(".//fieldset//field"):
        field_id = field.attrib.get("id")
        field_name = field.attrib.get("name") or field.attrib.get("title")
        if field_id and field_name:
            fid_to_name[field_id] = field_name.strip().lower()

    directory_map = {}
    for employee in root.findall(".//employee"):
        employee_id = (employee.attrib.get("id") or "").strip()
        if not employee_id:
            continue

        record = {}
        for field_node in employee.findall(".//field"):
            field_id = field_node.attrib.get("id")
            value = (field_node.text or "").strip()
            if field_id:
                key = fid_to_name.get(field_id, field_id).strip().lower()
                record[key] = value

        directory_map[employee_id] = record

    return directory_map


def moodle_endpoint():
    return f"{MOODLE_BASE_URL.rstrip('/')}/webservice/rest/server.php"


def flatten_form_field(prefix, value):
    flattened = []
    if isinstance(value, dict):
        for key, nested_value in value.items():
            flattened.extend(flatten_form_field(f"{prefix}[{key}]", nested_value))
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            flattened.extend(flatten_form_field(f"{prefix}[{index}]", nested_value))
    else:
        if isinstance(value, bool):
            scalar = "1" if value else "0"
        elif value is None:
            scalar = ""
        else:
            scalar = str(value)
        flattened.append((prefix, scalar))
    return flattened


def moodle_call(token, function_name, params):
    if not MOODLE_BASE_URL or not token:
        raise ValueError("Missing Moodle base URL or token")

    payload = [
        ("wstoken", token),
        ("wsfunction", function_name),
        ("moodlewsrestformat", "json"),
    ]

    for key, value in params.items():
        payload.extend(flatten_form_field(key, value))

    resp = requests.post(moodle_endpoint(), data=payload, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()

    try:
        parsed = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Moodle {function_name} returned non-JSON response: {resp.text[:500]}"
        ) from exc

    if isinstance(parsed, dict) and parsed.get("exception"):
        raise RuntimeError(
            "Moodle %s failed: %s | %s"
            % (
                function_name,
                parsed.get("errorcode", "unknown"),
                parsed.get("message", "no message"),
            )
        )

    return parsed


def moodle_get_user_by_field(token, field, value):
    if not value:
        return None

    # Prefer core_user_get_users because many Moodle tokens allow it while
    # core_user_get_users_by_field is often restricted.
    should_try_fallback = False
    try:
        result = moodle_call(
            token,
            "core_user_get_users",
            {"criteria": [{"key": field, "value": value}]},
        )
        users = result.get("users", []) if isinstance(result, dict) else []
        if users:
            return users[0]
        return None
    except RuntimeError as exc:
        error_text = str(exc).lower()
        if "accessexception" not in error_text:
            raise
        should_try_fallback = True

    # Fallback for installations that expose only core_user_get_users_by_field.
    if should_try_fallback:
        results = moodle_call(
            token,
            "core_user_get_users_by_field",
            {"field": field, "values": [value]},
        )
        if isinstance(results, list) and results:
            return results[0]
    return None


def moodle_create_user(token, user_payload):
    results = moodle_call(token, "core_user_create_users", {"users": [user_payload]})
    if isinstance(results, list) and results and "id" in results[0]:
        return int(results[0]["id"])
    raise RuntimeError(f"Unexpected create response payload: {results}")


def moodle_update_user(token, user_payload):
    result = moodle_call(token, "core_user_update_users", {"users": [user_payload]})
    warnings = result.get("warnings", []) if isinstance(result, dict) else []
    if warnings:
        raise RuntimeError(f"Moodle update returned warnings: {warnings}")


def is_inactive_bamboo_user(action, directory_record):
    if str(action).lower() == "deleted":
        return True

    status = first_non_empty(
        directory_record.get("employmenthistorystatus"),
        directory_record.get("status"),
        directory_record.get("employmentstatus"),
    ).lower()

    for marker in ("terminated", "inactive", "disabled", "deceased"):
        if marker in status:
            return True
    return False


def parse_directory_identity(employee_id, directory_record):
    first_name = first_non_empty(
        directory_record.get("preferredname"),
        directory_record.get("firstname"),
    )
    last_name = first_non_empty(directory_record.get("lastname"))
    display_name = first_non_empty(directory_record.get("displayname"))

    if not first_name or not last_name:
        fallback_first, fallback_last = split_name(display_name)
        first_name = first_name or fallback_first
        last_name = last_name or fallback_last

    email = first_non_empty(
        directory_record.get("workemail"),
        directory_record.get("email"),
        directory_record.get("homeemail"),
    ).lower()

    department = first_non_empty(directory_record.get("department"))
    username = safe_username_from_empid(employee_id)

    return {
        "username": username,
        "firstname": first_name,
        "lastname": last_name,
        "email": email,
        "department": department,
    }


def process_moodle_record(record, directory_record, moodle_token):
    employee_id = str(record.get("id") or "").strip()
    if not employee_id:
        raise ValueError(f"Changed record missing employee id: {record}")

    action = str(record.get("action") or "Updated")
    directory_record = directory_record or {}
    suspended = is_inactive_bamboo_user(action, directory_record)
    identity = parse_directory_identity(employee_id, directory_record)

    existing_user = moodle_get_user_by_field(moodle_token, "idnumber", employee_id)

    if existing_user is None and identity["email"] and EMAIL_RE.match(identity["email"]):
        existing_user = moodle_get_user_by_field(moodle_token, "email", identity["email"])

    if existing_user is not None:
        update_payload = {
            "id": int(existing_user["id"]),
            "idnumber": employee_id,
            "suspended": 1 if suspended else 0,
        }
        if identity["firstname"]:
            update_payload["firstname"] = identity["firstname"]
        if identity["lastname"]:
            update_payload["lastname"] = identity["lastname"]
        if identity["email"] and EMAIL_RE.match(identity["email"]):
            update_payload["email"] = identity["email"]
        if identity["department"]:
            update_payload["department"] = identity["department"]
        if MOODLE_DEFAULT_INSTITUTION:
            update_payload["institution"] = MOODLE_DEFAULT_INSTITUTION

        moodle_update_user(moodle_token, update_payload)
        return "suspended" if suspended else "updated"

    if suspended:
        return "skipped_deleted"

    if not identity["email"]:
        return "skipped_no_email"

    if not EMAIL_RE.match(identity["email"]):
        return "skipped_invalid_email"

    create_payload = {
        "username": identity["username"],
        "auth": MOODLE_AUTH,
        "password": gen_password(),
        "firstname": identity["firstname"],
        "lastname": identity["lastname"],
        "email": identity["email"],
        "idnumber": employee_id,
        "suspended": 1 if suspended else 0,
    }
    if identity["department"]:
        create_payload["department"] = identity["department"]
    if MOODLE_DEFAULT_INSTITUTION:
        create_payload["institution"] = MOODLE_DEFAULT_INSTITUTION

    moodle_create_user(moodle_token, create_payload)
    return "created"


def main():
    started_at = utc_now_iso()
    errors = 0

    ddb = boto3.client("dynamodb")
    secrets_client = boto3.client("secretsmanager")

    bamboo_secret = get_secret_json(secrets_client, BAMBOO_SECRET_ARN)
    moodle_secret = get_secret_json(secrets_client, MOODLE_SECRET_ARN)

    bamboo_api_key = (
        bamboo_secret.get("bamboohr_api_key")
        or bamboo_secret.get("api_key")
        or bamboo_secret.get("token")
        or ""
    )
    moodle_token = (
        moodle_secret.get("moodle_token")
        or moodle_secret.get("token")
        or moodle_secret.get("api_token")
        or ""
    )

    if not bamboo_api_key:
        print("ERROR: Bamboo API key not found in secret")
        sys.exit(1)

    if not moodle_token:
        print("ERROR: Moodle token not found in secret")
        sys.exit(1)

    state = get_state(ddb)
    since = state["since"]
    offset = state["offset"]

    total_changed = 0
    processed = 0
    created = 0
    updated = 0
    suspended = 0
    skipped_no_email = 0
    skipped_invalid_email = 0
    skipped_deleted = 0

    next_since = since
    next_offset = offset
    latest = since

    try:
        payload = fetch_bamboo_changes(since, bamboo_api_key)
        changes = payload.get("employees", [])
        latest = str(payload.get("latest") or since)
        total_changed = len(changes)

        directory_map = bamboo_directory(bamboo_api_key)

        if total_changed == 0 or offset >= total_changed:
            next_since = latest
            next_offset = 0
        else:
            batch = changes[offset:] if BATCH_SIZE <= 0 else changes[offset : offset + BATCH_SIZE]

            for record in batch:
                try:
                    employee_id = str(record.get("id") or "").strip()
                    directory_record = directory_map.get(employee_id, {})
                    outcome = process_moodle_record(record, directory_record, moodle_token)

                    if outcome == "created":
                        created += 1
                        processed += 1
                    elif outcome == "updated":
                        updated += 1
                        processed += 1
                    elif outcome == "suspended":
                        suspended += 1
                        processed += 1
                    elif outcome == "skipped_no_email":
                        skipped_no_email += 1
                    elif outcome == "skipped_invalid_email":
                        skipped_invalid_email += 1
                    elif outcome == "skipped_deleted":
                        skipped_deleted += 1
                except Exception as record_error:
                    errors += 1
                    print(
                        "ERROR: record processing failed",
                        json.dumps({"record": record, "error": repr(record_error)}),
                    )

            consumed = len(batch)

            if errors == 0:
                if offset + consumed < total_changed:
                    next_since = since
                    next_offset = offset + consumed
                else:
                    next_since = latest
                    next_offset = 0
            else:
                # Keep state pinned to retry this window on next run.
                next_since = since
                next_offset = offset

        put_state(ddb, next_since, next_offset)

    except Exception as run_error:
        errors += 1
        import traceback

        print("ERROR: run failed", repr(run_error))
        traceback.print_exc()

    summary = {
        "started_at": started_at,
        "since": since,
        "offset": offset,
        "batch_size": BATCH_SIZE,
        "total_changed": total_changed,
        "processed": processed,
        "created": created,
        "updated": updated,
        "suspended": suspended,
        "skipped_no_email": skipped_no_email,
        "skipped_invalid_email": skipped_invalid_email,
        "skipped_deleted": skipped_deleted,
        "errors": errors,
        "latest": latest,
        "next_since": next_since,
        "next_offset": next_offset,
    }

    print(json.dumps(summary, sort_keys=True))
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
