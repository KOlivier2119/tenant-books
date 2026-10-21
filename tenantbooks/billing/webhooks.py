"""Outgoing webhook delivery with HMAC signatures and manual retries.

Frappe v15's `frappe.enqueue` does not forward RQ's `delay` or `retry` options
(see frappe/utils/background_jobs.py:155-168 - only a fixed set of arguments
reaches `enqueue_call`), so retry/backoff is implemented here as data: a failed
attempt stores `next_retry_at`, and a scheduler job re-enqueues due rows.
"""

import hashlib
import hmac
import json
import random

import frappe
import requests
from frappe.utils import add_to_date, now_datetime

MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 10


# --------------------------------------------------------------------- payload


def build_payload(invoice) -> dict:
    """The exact bytes we sign are the exact bytes we send, so build once."""
    return {
        "event": "invoice.submitted",
        "invoice": invoice.name,
        "customer": invoice.customer,
        "order": invoice.order,
        "grand_total": float(invoice.grand_total or 0),
        "due_date": str(invoice.due_date),
        "status": invoice.status,
        "site": frappe.local.site,
    }


def canonical_json(payload: dict) -> bytes:
    """Deterministic JSON: sorted keys, no spaces.

    The receiver must recompute the signature over these same bytes, so any
    difference in key order or whitespace would break verification.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header_value: str) -> bool:
    """Constant-time comparison, so an attacker cannot probe the signature byte by byte."""
    if not header_value or not header_value.startswith("sha256="):
        return False
    expected = sign(secret, body)
    return hmac.compare_digest(expected, header_value)


# ------------------------------------------------------------------------ logs


def get_or_create_log(invoice_name: str):
    name = frappe.db.get_value("Webhook Delivery Log", {"invoice": invoice_name}, "name")
    if name:
        return frappe.get_doc("Webhook Delivery Log", name)
    return frappe.get_doc(
        {
            "doctype": "Webhook Delivery Log",
            "invoice": invoice_name,
            "event": "invoice.submitted",
            "status": "Retrying",
            "attempts": 0,
        }
    ).insert(ignore_permissions=True)


def append_attempt(log, line: str) -> None:
    """One line per attempt, oldest first - the audit trail for the delivery."""
    stamp = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
    previous = (log.get("response_body") or "").strip()
    log.response_body = f"{previous}\n[{stamp}] {line}".strip()


def backoff_seconds(attempt: int) -> int:
    """60s, 120s, 240s ... capped, plus up to 25% jitter so a fleet does not
    retry in lockstep and hammer the receiver."""
    raw = min(BASE_BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
    return int(raw + random.uniform(0, raw * 0.25))


# ------------------------------------------------------------------------ send


def get_webhook_config() -> tuple:
    """(url, secret) from site_config.json.

    Kept in one small function so tests can patch it instead of poking at
    Frappe's config internals.
    """
    conf = frappe.get_conf()
    return conf.get("webhook_url"), conf.get("webhook_secret")


def deliver_webhook(invoice_name: str) -> dict:
    """Background job: POST the invoice to the configured receiver once.

    Reads config from site_config.json:
      "webhook_url": "http://mock-receiver:8071/hook",
      "webhook_secret": "changeme"
    """
    url, secret = get_webhook_config()
    result = {"invoice": invoice_name, "status": "Failed", "http_code": None, "error": None}

    log = get_or_create_log(invoice_name)

    if not url or not secret:
        log.status = "Failed"
        log.error = "webhook_url / webhook_secret missing in site_config.json"
        append_attempt(log, f"SKIPPED: {log.error}")
        log.save(ignore_permissions=True)
        frappe.db.commit()
        return result

    invoice = frappe.get_doc("Invoice", invoice_name)
    body = canonical_json(build_payload(invoice))
    signature = sign(secret, body)
    log.url = url
    log.signature = signature
    log.attempts = int(log.attempts or 0) + 1

    try:
        response = requests.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-TenantBooks-Signature": signature,
                "X-TenantBooks-Event": "invoice.submitted",
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        result["http_code"] = response.status_code
        ok = 200 <= response.status_code < 300
        append_attempt(log, f"attempt {log.attempts}: HTTP {response.status_code} {response.text[:200]}")
    except requests.RequestException as exc:
        ok = False
        result["error"] = str(exc)
        append_attempt(log, f"attempt {log.attempts}: {type(exc).__name__} {exc}")

    if ok:
        log.status = "Sent"
        log.next_retry_at = None
        result["status"] = "Sent"
    elif log.attempts >= MAX_ATTEMPTS:
        log.status = "Failed"
        log.next_retry_at = None
        result["status"] = "Failed"
    else:
        log.status = "Retrying"
        log.next_retry_at = add_to_date(now_datetime(), seconds=backoff_seconds(log.attempts))
        result["status"] = "Retrying"

    log.http_code = result["http_code"]
    log.error = result["error"]
    log.save(ignore_permissions=True)
    frappe.db.commit()
    return result
