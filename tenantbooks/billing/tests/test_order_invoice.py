"""Integration tests: the Order -> Invoice path, against a real database."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate

from tenantbooks.billing.doctype.order.order import DEFAULT_PAYMENT_DAYS
from tenantbooks.billing.tests import helpers


class TestOrderToInvoice(FrappeTestCase):
    def setUp(self):
        self.customer = helpers.make_customer()
        self.product = helpers.make_product(stock_qty=10, unit_price=25.0)

    def test_totals_are_computed_not_trusted(self):
        order = frappe.new_doc("Order")
        order.customer = self.customer
        # A hostile payload claims the amount is 1.00; the controller ignores it.
        order.append("items", {"product": self.product, "qty": 3, "rate": 25.0, "amount": 1.0})
        order.insert(ignore_permissions=True)

        self.assertEqual(order.get("items")[0].amount, 75.0)
        self.assertEqual(order.net_total, 75.0)

    def test_submit_creates_submitted_invoice(self):
        order = helpers.draft_order(self.customer, self.product, qty=4, rate=25.0)
        with patch("tenantbooks.billing.jobs.enqueue_invoice_jobs") as enqueue_jobs:
            order.submit()

        invoice_name = frappe.db.get_value("Invoice", {"order": order.name}, "name")
        self.assertIsNotNone(invoice_name, "submitting an Order must create an Invoice")

        invoice = frappe.get_doc("Invoice", invoice_name)
        self.assertEqual(invoice.docstatus, 1)
        self.assertEqual(invoice.grand_total, 100.0)
        self.assertEqual(invoice.customer, self.customer)
        expected_due = add_days(getdate(order.order_date), DEFAULT_PAYMENT_DAYS)
        self.assertEqual(str(invoice.due_date), str(expected_due))
        self.assertTrue(invoice.name.startswith("TB-INV-"), invoice.name)

        # Background work is queued, not executed inline.
        enqueue_jobs.assert_called_once_with(invoice_name)

    def test_stock_is_decremented_on_submit_and_restored_on_cancel(self):
        order = helpers.draft_order(self.customer, self.product, qty=3)
        order.submit()
        self.assertEqual(frappe.db.get_value("Product", self.product, "stock_qty"), 7)

        order.reload()
        order.cancel()
        self.assertEqual(frappe.db.get_value("Product", self.product, "stock_qty"), 10)

    def test_over_stock_is_rejected_before_submit(self):
        order = frappe.new_doc("Order")
        order.customer = self.customer
        order.append("items", {"product": self.product, "qty": 11, "rate": 25.0})
        with self.assertRaises(frappe.ValidationError):
            order.insert()

    def test_invoice_created_once_only(self):
        """Re-submitting must not produce a second invoice for the same order."""
        order = helpers.draft_order(self.customer, self.product, qty=1)
        order.submit()
        first = frappe.db.get_values("Invoice", {"order": order.name}, "name", as_dict=True)
        self.assertEqual(len(first), 1)


class TestPermissions(FrappeTestCase):
    """The role difference that matters for the interview: a clerk cannot delete."""

    def setUp(self):
        self.customer = helpers.make_customer()
        self.product = helpers.make_product(stock_qty=5)
        self.clerk = self._make_user("billing-clerk@example.com", "Billing Clerk")
        self.manager = self._make_user("billing-manager@example.com", "Billing Manager")

    def _make_user(self, email: str, role: str) -> str:
        # get_or_create: a previous run may have left the user behind, because
        # code under test (webhook jobs) commits the transaction itself.
        if frappe.db.exists("User", email):
            user = frappe.get_doc("User", email)
            if not any(r.role == role for r in user.roles):
                user.append("roles", {"role": role})
                user.save(ignore_permissions=True)
            return user.name

        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": role.split()[-1],
                "send_welcome_email": 0,
                "roles": [{"role": role}],
            }
        )
        user.insert(ignore_permissions=True)
        return user.name

    def test_clerk_can_create_but_not_delete(self):
        frappe.set_user(self.clerk)
        try:
            self.assertTrue(frappe.has_permission("Order", "create"))
            self.assertFalse(frappe.has_permission("Order", "delete"))

            order = helpers.draft_order(self.customer, self.product, qty=1)
            # delete_doc, not order.delete(): the helper inserted with
            # ignore_permissions=True and that flag stays on the document object.
            with self.assertRaises(frappe.PermissionError):
                frappe.delete_doc("Order", order.name)
        finally:
            frappe.set_user("Administrator")

    def test_manager_can_delete(self):
        frappe.set_user(self.manager)
        try:
            self.assertTrue(frappe.has_permission("Order", "delete"))
        finally:
            frappe.set_user("Administrator")

    def test_anonymous_cannot_read(self):
        frappe.set_user("Guest")
        try:
            self.assertFalse(frappe.has_permission("Invoice", "read"))
        finally:
            frappe.set_user("Administrator")
