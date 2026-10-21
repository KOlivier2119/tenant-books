"""Shared test data builders.

Frappe tests run inside a real database transaction, so helpers create actual
documents rather than fakes.  Names are prefixed with "TEST " so a leftover row
is easy to spot and delete.
"""

import frappe
from frappe.utils import random_string


def make_customer(prefix: str = "Acme") -> str:
    name = f"TEST {prefix} {random_string(5)}"
    return (
        frappe.get_doc({"doctype": "Customer Account", "customer_name": name, "credit_limit": 10_000})
        .insert(ignore_permissions=True)
        .name
    )


def make_product(stock_qty: int = 10, unit_price: float = 25.0) -> str:
    code = f"TEST-SKU-{random_string(5)}"
    return (
        frappe.get_doc(
            {
                "doctype": "Product",
                "item_code": code,
                "item_name": f"Test product {code}",
                "unit_price": unit_price,
                "stock_qty": stock_qty,
            }
        )
        .insert(ignore_permissions=True)
        .name
    )


def draft_order(customer: str, product: str, qty: int = 2, rate: float | None = None) -> "frappe.Document":
    """An unsaved-but-inserted Order (docstatus 0).  Caller submits it."""
    if rate is None:
        rate = frappe.db.get_value("Product", product, "unit_price")
    order = frappe.new_doc("Order")
    order.customer = customer
    order.append("items", {"product": product, "qty": qty, "rate": rate})
    return order.insert(ignore_permissions=True)
