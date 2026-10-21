import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt

# Net payment term used for every auto-created invoice in the demo.
DEFAULT_PAYMENT_DAYS = 30


def on_doctype_update():
    # Frappe convention: a module-level function (not a Document method), run by
    # `bench migrate` after the DocType's table is synced.  This composite index
    # serves the two hottest reads: "orders of this customer, newest first" (the
    # list view) and the ledger report's join on customer.
    frappe.db.add_index("Order", ["customer", "order_date"], "idx_customer_date")


class Order(Document):
    # ------------------------------------------------------------------ helpers

    def required_quantities(self) -> dict:
        """Return {product: total_qty}, merging repeated rows for the same product."""
        required: dict = {}
        for row in self.get("items"):
            required[row.product] = required.get(row.product, 0) + cint(row.qty)
        return required

    # ------------------------------------------------------------------ lifecycle

    def validate(self):
        # `validate` runs on insert and on every save of a draft.
        self.calculate_totals()
        self.validate_stock()

    def calculate_totals(self):
        # Never trust a total that came from the browser: qty and rate are the
        # inputs, everything else is derived here.
        self.net_total = 0.0
        for row in self.get("items"):
            row.amount = flt(flt(row.qty) * flt(row.rate), 2)
            self.net_total = flt(self.net_total + row.amount, 2)

    def validate_stock(self):
        # Friendly early check.  The authoritative one is in decrease_stock(),
        # because between save and submit somebody else may have sold the stock.
        for product, qty in self.required_quantities().items():
            if qty <= 0:
                frappe.throw(_("{0}: Qty must be greater than 0").format(product))
            available = frappe.db.get_value("Product", product, "stock_qty") or 0
            if qty > available:
                frappe.throw(
                    _("Row for {0}: only {1} in stock, {2} requested").format(product, available, qty)
                )

    def on_submit(self):
        # Runs after docstatus flips to 1, inside the same transaction: raising
        # here rolls the whole submit back, which keeps stock and orders aligned.
        self.decrease_stock()
        self.create_invoice()

    def on_cancel(self):
        for product, qty in self.required_quantities().items():
            current = frappe.db.get_value("Product", product, "stock_qty") or 0
            frappe.db.set_value("Product", product, "stock_qty", current + qty, update_modified=False)

        invoice_name = frappe.db.get_value("Invoice", {"order": self.name}, "name")
        if invoice_name and frappe.db.get_value("Invoice", invoice_name, "status") != "Paid":
            invoice = frappe.get_doc("Invoice", invoice_name)
            # Document.cancel() takes no arguments; permissions are read from
            # the document's flags instead.
            invoice.flags.ignore_permissions = True
            invoice.cancel()

    # ------------------------------------------------------------------ actions

    def decrease_stock(self):
        # `SELECT ... FOR UPDATE` locks the product row until this transaction
        # ends, so two clerks selling the last unit cannot both succeed.
        for product, qty in self.required_quantities().items():
            locked = frappe.db.sql(
                "select stock_qty from `tabProduct` where name = %s for update",
                (product,),
                as_dict=True,
            )
            if not locked:
                frappe.throw(_("Product {0} does not exist").format(product))
            available = locked[0]["stock_qty"] or 0
            if qty > available:
                frappe.throw(_("{0}: stock is {1}, {2} requested").format(product, available, qty))
            frappe.db.set_value("Product", product, "stock_qty", available - qty, update_modified=False)

    def create_invoice(self):
        """Auto-create and submit the Invoice for this order."""
        if frappe.db.exists("Invoice", {"order": self.name}):
            return None  # idempotent: never invoice the same order twice

        invoice = frappe.new_doc("Invoice")
        invoice.order = self.name
        invoice.customer = self.customer
        invoice.grand_total = self.net_total
        invoice.due_date = add_days(self.order_date, DEFAULT_PAYMENT_DAYS)
        invoice.insert(ignore_permissions=True)
        invoice.submit()

        # db_set writes the column directly without re-running validations.
        self.db_set("invoice", invoice.name, update_modified=False)
        return invoice.name
