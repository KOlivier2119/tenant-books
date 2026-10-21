# TenantBooks

A small multi-tenant order & invoicing platform on Frappe v15, packaged for Docker
and Kubernetes. Built as an interview demo: every claim in the docs below has been
run on a laptop, and anything unverified is labelled.

Concepts, the role matrix, the API contract and the EXPLAIN evidence are in
[docs/part1-frappe-app.md](docs/part1-frappe-app.md).

## Part 1 status: the Frappe app (complete and tested)

```
tenant-books/
├── pyproject.toml                  # app package metadata (poetry-core backend)
├── docker/compose.local.yaml       # dev stack: MariaDB + 2 Redis + Frappe + mock receiver
├── mock-receiver/receiver.py       # stdlib-only HMAC verifier
└── tenantbooks/                    # the Frappe app's python package
    ├── hooks.py                    # scheduler + install hooks
    ├── install.py                  # creates the Billing roles
    └── billing/                    # module "Billing"
        ├── doctype/                # 6 DocTypes (JSON = schema, .py = controller)
        ├── api.py                  # whitelisted REST methods
        ├── jobs.py                 # background jobs (PDF, webhook enqueue)
        ├── webhooks.py             # HMAC signing, delivery, retry bookkeeping
        ├── tasks.py                # scheduler: overdue sweep, log purge, retry sweep
        ├── seed.py                 # demo customers/products
        ├── report/unpaid_invoice_ledger/   # script report + EXPLAIN demo
        └── tests/                  # 18 tests: unit (mocked) + integration
```

## Quickstart (verified on Docker 29, Fedora 44)

```bash
# 1. data services
docker compose -f docker/compose.local.yaml up -d db redis-cache redis-queue mock-receiver

# 2. one-time: write site config, create site, install app, set webhook config
docker compose -f docker/compose.local.yaml run --rm setup

# 3. the app: nginx, gunicorn, socketio, scheduler, two workers
docker compose -f docker/compose.local.yaml up -d
curl -s http://localhost:8080/api/method/ping -H 'Host: tenantbooks.localhost'   # {"message":"pong"}
```

UI: <http://localhost:8080> with `Host: tenantbooks.localhost` in your browser, or add
`127.0.0.1 tenantbooks.localhost` to `/etc/hosts`. Login: `Administrator` / `admin`.

### Make some data

```bash
docker compose -f docker/compose.local.yaml exec backend \
  bench --site tenantbooks.localhost execute tenantbooks.billing.seed.seed
```

### REST call (token auth)

Create a key pair for a user that holds the `Billing Manager` role:

```bash
docker compose -f docker/compose.local.yaml exec backend \
  bench --site tenantbooks.localhost execute \
  frappe.core.doctype.user.user.generate_keys --args '["Administrator"]'
```

```bash
curl -s -X POST http://localhost:8080/api/v2/method/tenantbooks.billing.api.create_order \
  -H 'Host: tenantbooks.localhost' \
  -H 'Authorization: token <API_KEY>:<API_SECRET>' \
  -H 'Content-Type: application/json' \
  -d '{"customer":"Northwind Books","items":[{"product":"BOOK-101","qty":2,"rate":25.0}]}'
# {"data":{"name":"TB-ORD-2026-0017","net_total":37.5,"invoice":"TB-INV-2026-00017"}}
```

Submitting the Order also: decrements stock, creates+submits the Invoice, and queues two
jobs on the `long` queue (PDF render, signed webhook).

### Run the tests

```bash
docker compose -f docker/compose.local.yaml exec backend \
  bench --site tenantbooks.localhost run-tests --app tenantbooks
# Ran 18 tests in 2.157s ... OK
```

`allow_tests true` is set by the `setup` job; without it Frappe refuses to run tests.

## What was verified by running it

| Claim | Evidence |
| --- | --- |
| 6 DocTypes sync into MariaDB | `bench migrate` output: `Updating DocTypes for tenantbooks [...100%]` |
| Order validate: totals recomputed | `test_totals_are_computed_not_trusted` (client-sent `amount` ignored) |
| Order validate: stock | `test_over_stock_is_rejected_before_submit`; `FOR UPDATE` lock in `decrease_stock` |
| Order submit -> Invoice + naming series | live REST call returned `TB-ORD-2026-0017` / `TB-INV-2026-00017` |
| Background PDF job | `worker-long`: `Job OK (...tenantbooks-pdf-TB-INV-2026-00017)`; `invoice_pdf` = `/files/TB-INV-2026-00017.pdf` |
| Webhook + HMAC | `mock-receiver` logged `ACCEPTED invoice.submitted invoice=TB-INV-2026-00017`; tampered-body test fails verification |
| Retry with backoff | `?fail=1` -> row `Retrying`, `http_code=500`, `next_retry_at` = 69s later; sweep -> `Sent`, `attempts=2` |
| Scheduler registration | `tabScheduled Job Type`: mark_overdue=Cron, purge=Daily, retry=Cron |
| Overdue sweep / purge | live runs returned `MARKED: 1` (status -> Overdue) and `PURGED: 1` |
| Composite indexes | `information_schema.statistics`: `idx_status_due(status,due_date)`, `idx_customer_date(customer,order_date)`, recreated by `bench migrate` after being dropped |
| Slow query -> fix | `compare_plans()`: slow = `type=ALL, key=NULL, Using filesort`; fast = `type=range, key=idx_status_due` |
| RBAC | `test_clerk_can_create_but_not_delete`, `test_manager_can_delete`, `test_anonymous_cannot_read` |
| Lint | `ruff check` / `ruff format` clean |

## Bugs the running system caught (worth telling an interviewer)

1. `scheduler_events` key `nightly` does not exist in v15 - valid keys are
   `hourly/daily/weekly/monthly/cron` (+ `*_long`). Fixed with cron `30 0 * * *`.
2. `on_doctype_update` must be a **module-level function**, not a `Document` method
   (Frappe calls `run_module_method`, see `frappe/core/doctype/doctype.py:549`). The
   indexes silently did not exist until this was fixed.
3. `Document.cancel()` takes no `ignore_permissions` kwarg; set
   `doc.flags.ignore_permissions` instead.
4. YAML merge keys (`<<`) do not deep-merge: redeclaring `environment:` on a service
   dropped `PYTHONPATH` and the app became unimportable (`ModuleNotFoundError`).
5. `bench execute generate_keys` did not persist: the console has no auto-commit, so
   the API key/secret pair was never written. Fixed with `frappe.db.commit()`.
6. wkhtmltopdf refused to build the PDF until the worker could resolve the site URL -
   hence the `tenantbooks.localhost` network alias on the frontend plus
   `host_name` / `webserver_port` in site config.
7. `get_datetime("today")` raises; use `nowdate()`.

## Not verified yet

- **2FA for admins**: not exercised end-to-end. The v15 mechanism (checked in the
  source, `frappe/twofactor.py:36-46`) is per-role, not per-site-config: turn on
  *Setup -> System Settings -> Enable Two Factor Auth* and tick
  `two_factor_auth` on the Role (e.g. System Manager), or call
  `frappe.twofactor.toggle_two_factor_auth(1, ["System Manager"])`. Login then requires
  an authenticator app (TOTP); recovery keys are generated on first enable.
- **Multi-tenancy**: each tenant = one Frappe *site* (its own database). This demo runs a
  single site. Per-request tenant routing, shared-schema row isolation, and rate-limiting
  per tenant are not implemented.
- **Rate limiting / idempotency keys** on `create_order`: absent. A duplicate POST with
  the same items creates a second Order.
- **Credit limit** field exists but is not enforced anywhere.
- **PDF print format**: the default Standard format is used; no custom Print Format file.
- Data volumes are tiny (20 rows), so `EXPLAIN rows=` numbers are not meaningful as
  performance evidence - only the plan *shape* (`ALL` vs `range`) is.
