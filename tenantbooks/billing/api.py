"""Whitelisted REST endpoints.

Only functions decorated with @frappe.whitelist() are reachable over HTTP, at:
    POST /api/v2/method/tenantbooks.billing.api.<function>

Auth: send the API key/secret pair as HTTP Basic auth, or as headers
    Authorization: token <API_KEY>:<API_SECRET>
The user owning the key must hold the matching role - see docs/RBAC.md.
"""

import json

import frappe
from frappe import _
from frappe.utils import getdate


def _as_list(value):
    """A JSON body may arrive as a list or as a stringified list (form encoding)."""
    if isinstance(value, str):
        return json.loads(value)
    return value or []


@frappe.whitelist()
def create_order(customer: str, items: list | str | None = None, order_date: str | None = None) -> dict:
    """Create and submit an Order, which in turn creates the Invoice."""
    # has_permission(..., throw=True) raises frappe.PermissionDenied -> HTTP 403.
    frappe.has_permission("Order", "create", throw=True)
    frappe.has_permission(
        "Customer Account", "read", doc=frappe.get_doc("Customer Account", customer), throw=True
    )

    order = frappe.new_doc("Order")
    order.customer = customer
    order.order_date = getdate(order_date) if order_date else getdate()
    for row in _as_list(items):
        order.append("items", row)

    if not order.get("items"):
        frappe.throw(_("At least one order item is required"))

    order.insert(ignore_permissions=False)
    order.submit()
    frappe.db.commit()

    return {
        "name": order.name,
        "net_total": order.net_total,
        "invoice": order.invoice,
    }


@frappe.whitelist()
def get_invoice_status(invoice: str) -> dict:
    """Status of one invoice, readable by anyone who can see the document."""
    doc = frappe.get_doc("Invoice", invoice)
    frappe.has_permission("Invoice", "read", doc=doc, throw=True)
    return {
        "name": doc.name,
        "status": doc.status,
        "grand_total": doc.grand_total,
        "due_date": str(doc.due_date),
        "customer": doc.customer,
        "invoice_pdf": doc.invoice_pdf,
    }


@frappe.whitelist()
def list_overdue_invoices(start: int = 0, page_length: int = 50) -> list:
    """Overdue invoices for a dunning/reminder run.

    frappe.get_list applies role permissions and (if configured) user
    permissions automatically, so a clerk only ever sees rows they may read.
    """
    frappe.has_permission("Invoice", "read", throw=True)
    return frappe.get_list(
        "Invoice",
        filters={"docstatus": 1, "status": "Overdue"},
        fields=["name", "customer", "grand_total", "due_date", "order"],
        order_by="due_date asc",
        limit_start=int(start),
        limit_page_length=min(int(page_length), 200),
    )
