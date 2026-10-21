"""Unpaid Invoice Ledger - a Script Report.

A Script Report is a folder holding a `.json` (metadata, allowed roles) and a
`.py` with `execute(filters)` returning `(columns, data)`.  Frappe calls it from
/api/v2/method/frappe.desk.query_report.run.

This report also carries the slow-query demo: see SLOW_SQL / FAST_SQL below and
`compare_plans()`, which prints both EXPLAIN plans.
"""

import json

import frappe
from frappe import _
from frappe.utils import getdate

# The first draft an engineer typically writes: "this month" expressed with
# date_format() on the column.  Wrapping a column in a function makes any index
# on it unusable, so MariaDB reads every submitted invoice row.
SLOW_SQL = """
select `tabInvoice`.`name`        as invoice,
       `tabInvoice`.`customer`    as customer,
       `tabInvoice`.`grand_total` as grand_total,
       `tabInvoice`.`due_date`    as due_date
from `tabInvoice`
where `tabInvoice`.`docstatus` = 1
  and `tabInvoice`.`status` in ('Unpaid', 'Overdue')
  and date_format(`tabInvoice`.`due_date`, '%%Y-%%m') = %(month)s
order by `tabInvoice`.`customer` asc, `tabInvoice`.`due_date` asc
"""

# The fix: a half-open range on the raw column so the composite index
# (status, due_date), added in Invoice.on_doctype_update, can be used, and an
# ORDER BY that follows that same index order.
FAST_SQL = """
select `tabInvoice`.`name`        as invoice,
       `tabInvoice`.`customer`    as customer,
       `tabInvoice`.`grand_total` as grand_total,
       `tabInvoice`.`due_date`    as due_date
from `tabInvoice`
where `tabInvoice`.`docstatus` = 1
  and `tabInvoice`.`status` in ('Unpaid', 'Overdue')
  and `tabInvoice`.`due_date` >= %(from_date)s
  and `tabInvoice`.`due_date` < %(to_date)s
order by `tabInvoice`.`status` asc, `tabInvoice`.`due_date` asc
"""

COLUMNS = [
    {"label": _("Invoice"), "fieldname": "invoice", "fieldtype": "Link", "options": "Invoice", "width": 160},
    {
        "label": _("Customer"),
        "fieldname": "customer",
        "fieldtype": "Link",
        "options": "Customer Account",
        "width": 220,
    },
    {"label": _("Grand Total"), "fieldname": "grand_total", "fieldtype": "Currency", "width": 140},
    {"label": _("Due Date"), "fieldname": "due_date", "fieldtype": "Date", "width": 120},
]


def month_bounds(month: str) -> tuple:
    """'2026-01' -> ('2026-01-01', '2026-02-01').  Half-open, so no off-by-one."""
    first = getdate(f"{month}-01")
    if first.month == 12:
        following = getdate(f"{first.year + 1}-01-01")
    else:
        following = getdate(f"{first.year}-{first.month + 1:02d}-01")
    return str(first), str(following)


def execute(filters: dict | None = None) -> tuple:
    filters = filters or {}
    month = filters.get("month") or getdate().strftime("%Y-%m")
    from_date, to_date = month_bounds(month)

    data = frappe.db.sql(FAST_SQL, {"from_date": from_date, "to_date": to_date}, as_dict=True)
    total = sum(row.grand_total or 0 for row in data)
    return COLUMNS, data, None, {"total_row": {"grand_total": total}}


def compare_plans() -> str:
    """EXPLAIN the slow and the fixed query, so the difference is visible.

    Run it without the UI:
        bench --site <site> execute \\
          tenantbooks.billing.report.unpaid_invoice_ledger.unpaid_invoice_ledger.compare_plans
    """
    today = getdate()
    month = today.strftime("%Y-%m")
    from_date, to_date = month_bounds(month)

    cases = (
        ("SLOW  date_format() on the column", SLOW_SQL, {"month": month}),
        ("FAST  range + idx_status_due", FAST_SQL, {"from_date": from_date, "to_date": to_date}),
    )

    lines = []
    for title, sql, params in cases:
        rows = frappe.db.sql("explain " + sql, params, as_dict=True)
        lines.append(f"\n=== {title} ===")
        for row in rows:
            lines.append(json.dumps(row, default=str, indent=1, sort_keys=True))

    output = "\n".join(lines)
    print(output)
    return output
