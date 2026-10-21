"""Demo data, so the interview walkthrough starts with a populated site.

bench --site tenantbooks.localhost execute tenantbooks.billing.seed.seed
"""

import frappe

CUSTOMERS = [
    ("Northwind Books", "accounts@northwind.example", 50_000),
    ("Apress Print", "billing@apress.example", 20_000),
]

PRODUCTS = [
    ("BOOK-101", "Hardcover Notebook", 25.0, 40),
    ("BOOK-202", "Paperback Anthology", 12.5, 100),
    ("BOOK-303", "Collector Edition", 90.0, 5),
]


def seed() -> dict:
    created = {"customers": 0, "products": 0}
    for name, email, limit in CUSTOMERS:
        if not frappe.db.exists("Customer Account", name):
            frappe.get_doc(
                {
                    "doctype": "Customer Account",
                    "customer_name": name,
                    "email": email,
                    "credit_limit": limit,
                }
            ).insert(ignore_permissions=True)
            created["customers"] += 1

    for code, item_name, price, stock in PRODUCTS:
        if not frappe.db.exists("Product", code):
            frappe.get_doc(
                {
                    "doctype": "Product",
                    "item_code": code,
                    "item_name": item_name,
                    "unit_price": price,
                    "stock_qty": stock,
                }
            ).insert(ignore_permissions=True)
            created["products"] += 1

    frappe.db.commit()
    return created
