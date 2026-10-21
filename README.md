# TenantBooks

A small multi-tenant order & invoicing platform on Frappe v15, packaged for Docker
and Kubernetes. Built as an interview demo: every claim in the docs below has been
run on a laptop, and anything unverified is labelled.

Concepts, the role matrix, the API contract and the EXPLAIN evidence are in
[docs/part1-frappe-app.md](docs/part1-frappe-app.md). Packaging, the multi-stage
image and the prod-shaped compose stack are in
[docs/part2-docker.md](docs/part2-docker.md).

Progress: **Part 1 (Frappe app) and Part 2 (Docker) are done and were run.**
Part 3 Kubernetes, Part 4 CI/CD, Part 5 observability, Part 6 DB ops/security
and Part 7 Linux/runbooks follow.

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

## Part 2 status: Docker (complete and verified)

```
apps.json                           # bench manifest: which apps go in the image
.dockerignore                       # what can never reach a layer
docker/custom/Containerfile         # 2 stages: frappe/build (toolchain) -> frappe/base (runtime)
docker/custom/start.sh              # gunicorn launcher, sized from env not from the image
docker/compose.prod.yaml            # same 11-process topology, built from the image
docker/prod.env.example             # secrets + sizing, copy to docker/prod.env
mock-receiver/Dockerfile            # the peer system as its own pinned non-root image
```

```bash
# build (≈7 min cold: bench init + asset bundle + export)
docker buildx build --secret id=apps_json,src=apps.json \
    -t tenantbooks:v15.103.0 -f docker/custom/Containerfile .

# run (8081/8072, so the dev stack can stay put)
cp docker/prod.env.example docker/prod.env   # then set the 3 secrets
docker compose --env-file docker/prod.env -f docker/compose.prod.yaml up -d --build
curl -s http://localhost:8081/api/method/ping -H 'Host: tenantbooks.localhost'
```

Image facts, all measured (details and commands in `docs/part2-docker.md` §9):

| Claim | Evidence |
| --- | --- |
| app baked in, no path hacks | `bench version` -> `frappe 15.103.0 / tenantbooks 0.0.1`; `$PYTHONPATH` unset in the container; `import tenantbooks` resolves to `apps/tenantbooks/` |
| no build toolchain in runtime | `gcc -> none`, `make -> none` inside the final image (they exist only in the `builder` stage) |
| non-root | `id` -> `uid=1000(frappe) gid=1000`, files group-writable for gid 0 |
| 3.07 GB, i.e. smaller than ERPNext (3.21 GB) while carrying our app | `docker images` |
| `apps.json` (BuildKit secret) is not in any layer | `docker history --no-trunc \| grep -c '"url"'` -> 0; `/opt/frappe/apps.json` absent |
| nothing mounted from the host | `docker inspect` shows only `tenantbooks-prod_sites` / `_logs` volumes |
| the whole Part 1 flow still works from the image | REST `create_order` -> `TB-ORD-2026-0001` / `TB-INV-2026-00001`; worker-long `Job OK` for `tenantbooks-pdf-…` and `tenantbooks-webhook-…`; receiver logged `ACCEPTED invoice.submitted`; delivery log `Sent / attempts 1 / 200`; `/files/TB-INV-2026-00001.pdf` -> 200 `application/pdf` 16,917 B |
| prod refuses to run tests | `bench run-tests` -> "You can enable tests by ... set-config allow_tests true" |

## Part 3 status: Kubernetes (manifests verified on k3s; app tier pending one root command)

```
k8s/base/kustomization.yaml     # 37 objects, one apply
k8s/base/configmap.yaml         # non-secret env for all 7 frappe roles
k8s/base/secret.example.yaml    # -> secret.yaml (git-ignored; kustomize fails without it)
k8s/base/pvc.yaml               # sites + logs, the shared Frappe filesystem
k8s/base/mariadb.yaml           # headless Service + StatefulSet (or delete for an external DB)
k8s/base/redis.yaml             # redis-cache (Deployment) + redis-queue (StatefulSet, AOF)
k8s/base/jobs.yaml              # create-site, migrate  (+ the PVC-seeding initContainer)
k8s/base/{backend,frontend,websocket,scheduler,workers}.yaml   # the 7 roles, 1 image
k8s/base/hpa.yaml               # autoscaling/v2, CPU 70%, 2-6 replicas, asymmetric behavior
k8s/base/networkpolicy.yaml     # default-deny + 10 allows (both directions, see docs)
k8s/base/ingress.yaml           # traefik now, cert-manager TLS block commented
k8s/tls/issuer.yaml             # ClusterIssuers, kept out of base on purpose
k8s/overlay-registry/           # same manifests, image from ghcr.io (the CI path)
```

```bash
cp k8s/base/secret.example.yaml k8s/base/secret.yaml    # 3 values; kustomize refuses without it
kubectl apply -f k8s/base/namespace.yaml                # Namespace first (see docs §2)
kubectl apply -k k8s/base
kubectl -n tenantbooks wait --for=condition=Complete --timeout=15m job/create-site
```

Measured on this machine (k3s 1.36.5, single node, full transcript in
[docs/part3-kubernetes.md](docs/part3-kubernetes.md)):

| Claim | Evidence |
| --- | --- |
| every manifest is schema- and admission-valid | `kubectl kustomize k8s/base` -> 37 docs; `kubectl apply -k k8s/base --dry-run=server` -> 37 x `created (server dry run)`, zero errors |
| the data tier really runs | `db-0 1/1`, `redis-cache 1/1`, `redis-queue-0 1/1` with exec/tcpSocket probes passing |
| non-root can write the shared volume | probe pod with `runAsUser: 1000, runAsGroup: 0, fsGroup: 0`: `drwxrwsrwx /sites`, `WRITE_OK` |
| NetworkPolicy blocks, not just parses | `tier=frappe` pod: `db (10.42.0.18:3306) open` in 0.2 s; unlabelled pod: no response (dropped) on `db`, `redis-cache`, `redis-queue` |
| HPA accepted with its behavior section | `describe hpa backend` echoes 30 s/300 s windows and `0 current / 0 desired` |
| the image is the only missing piece for the app tier | k3s runs its own containerd: `docker save -o /tmp/tenantbooks.tar` done (692 MB); `sudo k3s ctr images import /tmp/tenantbooks.tar` needs root |

Four real bugs were caught by running the manifests rather than writing them (docs §15):
a Kubernetes PVC is **not** pre-populated from the image the way a Docker named volume is
(so `apps.txt` and `/sites/assets` were missing -> seeding initContainer); a NetworkPolicy
egress allow with no matching ingress allow is a silent timeout; `mysqladmin` does not
exist in `mariadb:11.4` so the DB probe died on `command not found`; and a headless
Service publishes no DNS record while its pod is NotReady, which makes a broken probe look
like a DNS failure from every client.

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
8. (Part 2) `git clone` of the read-only build-context mount fails with
   *dubious ownership* - and the fix only works with the **`.git`** path:
   `git config --global --add safe.directory /opt/src/tenantbooks/.git`.
9. (Part 2) because the builder *clones* the mount, the image is built from
   `HEAD`, not from the working tree: uncommitted edits are silently absent while
   still busting the layer cache. Commit, then build.
10. (Part 2) `ARG` does not cross a `FROM`; a stage-2 `LABEL` using
    `${FRAPPE_REPO}` rendered an empty value until the ARG was redeclared inside
    the stage. BuildKit's `UndefinedVar` warning was the only clue.
11. (Part 2) a healthcheck that probes the app server **without a site Host
    header** sees 404, because Frappe resolves the site per request - the
    container looks unhealthy while serving traffic fine. Same trap in
    Kubernetes: `httpGet` probes need `httpHeaders: [{name: Host, ...}]`.
12. (Part 3) **a Kubernetes PVC is born empty.** A Docker named volume copies the
    image's content at that path on first use, which is how the compose stack got
    `sites/apps.txt` and the built `sites/assets` for free. On the cluster every pod
    failed on a hidden `sites/` until `jobs.yaml` grew a `seed-sites-volume`
    initContainer that mounts the PVC at a *different* path and copies once.
13. (Part 3) a NetworkPolicy **egress** allow with no **ingress** allow on the
    target is a silent timeout: `default-deny` is evaluated per pod per direction,
    so my first version blocked every client from MariaDB and Redis while looking
    correct. The `db-ingress` / `redis-ingress` / `receiver-ingress` documents exist
    because of that bug, not for symmetry.
14. (Part 3) `mysqladmin` is **not in `mariadb:11.4`** (the image ships
    `/usr/sbin/mariadbd` plus the renamed `mariadb` / `mariadb-admin` clients), so the
    DB startup probe died with `command not found` and left the pod
    Running-but-Never-Ready. Our app image *does* have `/usr/bin/mysqladmin`, so the
    two `wait-for-db` initContainers are fine - two images, both "MariaDB", different
    tools.
15. (Part 3) a **headless Service publishes DNS only for Ready endpoints**: while
    `db-0` failed its probe, every client saw `nc: bad address 'db'`, i.e. a storage-
    looking error for a probe problem. Related: a StatefulSet whose new template fails
    its probe deadlocks the rollout (`Waiting for 1 pods to be ready` forever) until
    `kubectl delete pod db-0`.

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
- (Part 2) **registry push**: the image was built and run locally only; no
  `docker push`, no `ghcr.io` login (that is Part 4).
- (Part 2) **multi-arch**: everything is native `linux/amd64` on x86_64. Cross
  building for arm64 was not attempted and frappe's native wheels make it slow.
- (Part 2) **arbitrary uid at runtime**: the image is group-0 writable so a
  kubelet-assigned uid *should* work, but only uid 1000 was actually exercised
  here; `runAsUser`/`fsGroup` are tested in Part 3.
- (Part 3) **the seven app pods on the cluster.** Manifests are accepted by the real
  API server and the data tier runs, but k3s has its own containerd and importing the
  local image needs root: `sudo k3s ctr images import /tmp/tenantbooks.tar` (the tar is
  already saved, 692 MB). After that one command the remaining verification is listed in
  `docs/part3-kubernetes.md` §13.
- (Part 3) **cert-manager TLS**: the `tls:` block and `k8s/tls/issuer.yaml` are written
  but no certificate was issued here - HTTP-01 needs a publicly resolvable hostname.
- (Part 3) **multi-node / RWX storage** and **HPA actually scaling**: both reasoned
  about, neither observed (one node, one RWO provisioner, no backend pods running).
