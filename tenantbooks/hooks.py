"""Frappe hooks for the tenantbooks app.

A "hook" is just a module-level variable with a name Frappe recognises.  Frappe
reads this file at startup and wires the values into its own behaviour.
"""

from . import __version__ as app_version  # noqa: F401

app_name = "tenantbooks"
app_title = "Billing"
app_publisher = "TenantBooks Demo"
app_description = "Multi-tenant order and invoicing platform"
app_email = "dev@example.com"
app_license = "MIT"

# Roles are created once, when the app is installed on a site.
after_install = "tenantbooks.install.after_install"

# Scheduled jobs.  Frappe's `bench schedule` process (one container/pod) runs
# these; the dotted string is resolved with frappe.call at execution time.
# Valid keys are the frequencies Frappe knows: hourly, daily, weekly, monthly,
# cron (and the *_long variants that run on the long queue).  There is no
# "nightly", so "every night at 00:30" is written as a cron expression.
scheduler_events = {
    # One bulk UPDATE: Unpaid invoices past their due date become Overdue.
    "cron": {
        "30 0 * * *": [
            "tenantbooks.billing.tasks.mark_overdue_invoices",
        ],
        # Retry hook: frappe.enqueue has no delay/retry argument in v15, so a
        # scheduler sweep re-enqueues jobs whose backoff window has elapsed.
        "*/5 * * * *": [
            "tenantbooks.billing.tasks.retry_pending_webhooks",
        ],
    },
    # Drop finished Webhook Delivery Log rows older than LOG_RETENTION_DAYS.
    "daily": [
        "tenantbooks.billing.tasks.purge_old_logs",
    ],
}
