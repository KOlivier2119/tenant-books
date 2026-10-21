# Part 1 - the Frappe app, in plain language

Concepts appear in the order you meet them in the code.

## Frappe in one paragraph

Frappe is a "batteries included" Python web framework where your **data model is the
code**. You describe a table as JSON (a *DocType*), Frappe creates the MariaDB table,
generates the CRUD REST API, the admin UI, the permission layer, the audit trail, the
queue integration and the test harness. You write Python only for behaviour.

## DocType = table + form + API

`tenantbooks/billing/doctype/order/order.json` lists fields (`fieldname`, `fieldtype`,
`reqd`, `in_list_view`, `options`). Each field maps to a MariaDB column in the table
`` `tabOrder` `` - Frappe prefixes table names with `tab`.
The same file holds the **permission rows** (role -> create/read/write/delete/submit/cancel).

Kinds used here:

| DocType | Kind | Notes |
| --- | --- | --- |
| Customer Account, Product | plain | `autoname: "field:customer_name"` - the name IS a column value |
| Order Item | child table (`istable: 1`) | lives inside `tabOrder Item`, linked by `parent` |
| Order, Invoice | submittable (`is_submittable: 1`) | `docstatus`: 0 draft, 1 submitted, 2 cancelled |
| Webhook Delivery Log | plain | written by server code, read by humans |

**docstatus** is Frappe's workflow primitive. Draft rows can be edited and deleted;
submitted rows cannot be edited or deleted, only cancelled or amended. That is why the
stock mutation lives in `on_submit`, not in `validate`.

## Controller hooks (order.py, invoice.py)

Python runs *after* the JSON is validated, in a fixed order:

```
validate -> before_insert/before_save -> insert/update -> on_submit | on_cancel
```

* `validate()` runs on every save of a draft. Totals are recomputed here - never trust a
  number that came from the browser.
* `on_submit()` runs inside the same DB transaction as the status flip, so raising
  `frappe.ValidationError` there rolls the whole submit back.
* `on_doctype_update()` is a **module-level function**, not a method. Frappe calls it via
  `run_module_method` (`frappe/core/doctype/doctype.py:549`) when a DocType is saved by
  `bench migrate`. This is where the composite index is declared.

`frappe.throw(msg)` raises an exception whose message becomes the HTTP error body -
that is the whole error-reporting model.

## Naming series

`"autoname": "TB-INV-.YYYY.-.#####"` -> `TB-INV-2026-00029`. The counter lives in the
`Auto Name` table, **outside** the transaction, so a rolled-back or failed insert still
consumes a number: gaps are normal and are not lost invoices. Say that in the interview
before someone asks.

## Background jobs

`frappe.enqueue(method_string, queue="long", ...)` serialises the call into Redis (RQ);
a `bench worker` process pops it, reconnects to the site, and runs it.

```python
enqueue("tenantbooks.billing.webhooks.deliver_webhook",
        queue="long", job_id=f"tenantbooks-webhook-{name}",
        deduplicate=True, enqueue_after_commit=True, invoice_name=name)
```

* `queue="long"` - PDF and network I/O go to `worker-long`, so a slow receiver cannot
  stall short jobs. Queues are `short`, `default`, `long` (v15 timeouts 300s/300s/1500s).
* `enqueue_after_commit=True` - the worker would otherwise be able to start before the
  Invoice row is visible and fail with "Invoice not found".
* `job_id` + `deduplicate=True` - a double-click cannot queue two deliveries.
* `now=True` / `is_async=False` runs the function inline - used by tests.

**Retry:** v15 `frappe.enqueue` does not forward RQ's `delay`/`retry`
(`frappe/utils/background_jobs.py:155-168` builds a fixed argument set). So retries are
data, not queue features: the delivery row stores `attempts` and `next_retry_at =
now + min(60 * 2**(n-1), 3600) + jitter`, and a `*/5 * * * *` scheduler sweep
re-enqueues rows whose window elapsed. Verified live: 500 -> `Retrying` (69 s) -> sweep
-> `Sent`, `attempts=2`.

## Scheduler

`hooks.py` -> `scheduler_events`. Valid keys in v15: `hourly`, `daily`, `weekly`,
`monthly`, `cron`, plus `*_long` variants. **There is no `nightly`** - "30 0 \* \* \*" is
the nightly job. `bench schedule` (one process only!) evaluates the due jobs and enqueues
them; `tabScheduled Job Type` rows are the visible proof.

## REST API and token auth

Any `@frappe.whitelist()` function is callable at
`/api/method/<dotted.path>` (GET or POST). Two versions are mounted:
`/api/method/...` (v1) and `/api/v2/method/...` plus generic CRUD at `/api/v2/resource/`.

Auth with an API key pair, in the `Authorization` header:

```
Authorization: token <api_key>:<api_secret>     # or Basic auth with the same pair
```

`api_secret` is stored encrypted (`get_decrypted_password`) and compared in
`frappe/auth.py:707`. Keys are minted for a *user*, so **the user's roles are the API's
authorization model** - there is no separate scope system.

Generate keys (Administrator only, `frappe.only_for("System Manager")`):

```bash
bench --site tenantbooks.localhost execute frappe.core.doctype.user.user.generate_keys \
  --args '["api-demo@tenantbooks.local"]'
```

Endpoints:

| Method | Purpose | Needs |
| --- | --- | --- |
| `tenantbooks.billing.api.create_order` | insert+submit an Order | `Order:create` |
| `tenantbooks.billing.api.get_invoice_status` | one invoice | `Invoice:read` on that doc |
| `tenantbooks.billing.api.list_overdue_invoices` | dunning list | `Invoice:read` |

Each one calls `frappe.has_permission(..., throw=True)` explicitly, and
`frappe.get_list()` applies role permissions (and user permissions, if configured) on
its own. Never return rows with `frappe.db.sql` for an API - that bypasses the whole
permission layer.

## Roles: Billing Manager vs Billing Clerk

Permission rows live in each DocType JSON and are re-applied by `bench migrate`:

| DocType | Manager | Clerk |
| --- | --- | --- |
| Customer Account, Product | create, read, write, delete | create, read, write - **no delete** |
| Order | + submit, cancel, amend, delete | create, read, write, submit - no cancel/delete/amend |
| Invoice | full | create, read, write, submit - no delete/cancel |
| Webhook Delivery Log | read, write, delete | read only |

Roles themselves are created once in `tenantbooks/install.py` (`after_install`), because a
permission row pointing at a missing role is silently useless.

Live proof (same document, two users):

```bash
# clerk
curl -s -X POST .../api/method/frappe.client.delete -d '{"doctype":"Invoice","name":"TB-INV-2026-00029"}'
-> frappe.exceptions.PermissionError: User not allowed to delete Invoice
# manager
-> frappe.exceptions.ValidationError: Submitted Record cannot be deleted ...
```

The manager passes the permission gate and is stopped by the *business* rule; the clerk
never gets that far. Also covered by `test_clerk_can_create_but_not_delete`,
`test_manager_can_delete`, `test_anonymous_cannot_read`.

### 2FA for admins

Frappe's 2FA is **per role**, not per site config (`frappe/twofactor.py:36-46`):

1. Setup -> System Settings -> tick *Enable Two Factor Auth* (or
   `frappe.twofactor.toggle_two_factor_auth(1, ["System Manager"])`).
2. Open the *System Manager* role and tick *Two Factor Auth* - or any role you like.
3. Next login asks for a TOTP setup (authenticator app) and prints one-time recovery
   keys; they are stored hashed on the User.
4. API keys keep working - **2FA protects the interactive login, not token auth** - so
   keep `System Manager` off integration users and give them the narrow Billing roles.

Not exercised end-to-end in this repo (needs an interactive login and an OTP app); the
source path above is what I verified.

## MariaDB: index, slow query, EXPLAIN

`Invoice.on_doctype_update` adds `idx_status_due (status, due_date)` and
`Order.on_doctype_update` adds `idx_customer_date (customer, order_date)`.

The deliberately slow report query (`.../unpaid_invoice_ledger.py`, `SLOW_SQL`) filters
"this month" with `date_format(due_date, '%Y-%m') = ?`. Wrapping the column in a function
makes any index on it unusable.

```bash
bench --site tenantbooks.localhost execute \
  tenantbooks.billing.report.unpaid_invoice_ledger.unpaid_invoice_ledger.compare_plans
```

Measured on this laptop (20 invoices - tiny data, so read the *plan shape*, not the counts):

| query | type | key | Extra |
| --- | --- | --- | --- |
| `date_format()` on the column | `ALL` | `NULL` | `Using where; Using filesort` |
| half-open range + `ORDER BY status, due_date` | `range` | `idx_status_due` | `Using index condition; Using where` |

`type=ALL` means full table scan; `key=NULL` means no index was picked; `Using filesort`
means the sort could not be satisfied by an index. The fix is the shape of the predicate
(a range on the bare column) plus an `ORDER BY` that follows the index order.

## Tests

```bash
bench --site tenantbooks.localhost run-tests --app tenantbooks     # 18 passed
```

* `tests/test_webhooks.py` - unit: `requests.post` is a `MagicMock`, config is patched,
  so failure paths (500, 503, `ConnectionError`, missing config, give-up after 5
  attempts) are deterministic. Signature tests need no DB (`unittest.TestCase`).
* `tests/test_order_invoice.py` - integration on the real DB: totals recomputed, submit
  creates a submitted Invoice with the right due date and prefix, stock decrements and is
  restored on cancel, over-stock insert is rejected, and the role matrix.
  `enqueue_invoice_jobs` is mocked - the integration test must not depend on Redis.
* FrappeTestCase wraps each test in a transaction and rolls back. Two of our functions
  call `frappe.db.commit()` (correct for jobs, bad for isolation), which is why
  `_make_user` in the test is a get-or-create - a leftover user from a committed run made
  the next run fail with a duplicate-key error. That was a real bug found by running it.
