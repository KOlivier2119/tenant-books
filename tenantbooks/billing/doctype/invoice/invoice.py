import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class Invoice(Document):
    def validate(self):
        if flt(self.grand_total) < 0:
            frappe.throw(_("Grand Total cannot be negative"))

    def on_submit(self):
        # Imported here rather than at module level: jobs.py imports nothing from
        # this module today, but a top-level import both ways would create a
        # circular import that Python raises at startup.
        from tenantbooks.billing.jobs import enqueue_invoice_jobs

        enqueue_invoice_jobs(self.name)

    def before_cancel(self):
        if self.status == "Paid":
            frappe.throw(_("A Paid invoice cannot be cancelled. Reopen it first."))


def on_doctype_update():
    # Module-level, per Frappe's convention (see frappe/core/doctype/doctype.py:549).
    # Serves the overdue sweep and the ledger report: filter on status, then a
    # range scan on due_date.
    frappe.db.add_index("Invoice", ["status", "due_date"], "idx_status_due")
