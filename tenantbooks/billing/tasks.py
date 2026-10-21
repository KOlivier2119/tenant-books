"""Scheduled tasks.  These are the dotted strings listed in hooks.py."""

import frappe
from frappe.utils import add_days, get_datetime, now_datetime, nowdate
from frappe.utils.background_jobs import enqueue

LOG_RETENTION_DAYS = 30


def mark_overdue_invoices() -> int:
    """Nightly: flip submitted, still-unpaid invoices past their due date.

    bulk_update writes straight to the columns in batches of 100 instead of
    loading each document: no controller code runs, which is acceptable because
    only `status` changes, and it stays fast on large tables.
    """
    overdue = frappe.get_all(
        "Invoice",
        filters={"docstatus": 1, "status": "Unpaid", "due_date": ("<", nowdate())},
        pluck="name",
        limit=5000,
    )
    if not overdue:
        return 0

    frappe.db.bulk_update(
        "Invoice",
        {name: {"status": "Overdue"} for name in overdue},
        modified=now_datetime(),
    )
    frappe.db.commit()
    return len(overdue)


def purge_old_logs(days: int = LOG_RETENTION_DAYS) -> int:
    """Daily: drop finished Webhook Delivery Log rows older than `days`.

    Rows still in "Retrying" are kept - deleting one would silently drop a
    pending delivery.
    """
    cutoff = add_days(get_datetime(now_datetime()), -days)
    names = frappe.get_all(
        "Webhook Delivery Log",
        filters={"modified": ("<", cutoff), "status": ("!=", "Retrying")},
        pluck="name",
    )
    if not names:
        return 0

    frappe.db.delete("Webhook Delivery Log", {"name": ("in", names)})
    frappe.db.commit()
    return len(names)


def retry_pending_webhooks() -> int:
    """Every 5 minutes: re-enqueue webhook deliveries whose backoff has elapsed.

    This is the retry mechanism, because frappe.enqueue cannot schedule a delayed
    job in Frappe v15.
    """
    due = frappe.get_all(
        "Webhook Delivery Log",
        filters={"status": "Retrying", "next_retry_at": ("<=", now_datetime())},
        pluck="invoice",
    )
    for invoice_name in due:
        enqueue(
            "tenantbooks.billing.webhooks.deliver_webhook",
            queue="long",
            job_id=f"tenantbooks-webhook-{invoice_name}",
            deduplicate=True,
            invoice_name=invoice_name,
        )
    return len(due)
