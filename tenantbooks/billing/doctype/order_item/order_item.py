import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class OrderItem(Document):
    def validate(self):
        # A child row is a Document too, so it can validate itself.
        # frappe.utils.flt() turns None/"" into a float and rounds to 2 decimals,
        # which is what we want for money coming from the REST API.
        if flt(self.qty) <= 0:
            frappe.throw(_("Row {0}: Qty must be greater than 0").format(self.idx))
        if flt(self.rate) < 0:
            frappe.throw(_("Row {0}: Rate cannot be negative").format(self.idx))
