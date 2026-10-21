# TenantBooks

A small multi-tenant order & invoicing platform on Frappe v15, packaged for Docker
and Kubernetes. Built as an interview demo: every claim in the docs below has been
run on a laptop, and anything unverified is labelled.

Concepts, the role matrix, the API contract and the EXPLAIN evidence are in
[docs/part1-frappe-app.md](docs/part1-frappe-app.md). Packaging, the multi-stage
image and the prod-shaped compose stack are in
[docs/part2-docker.md](docs/part2-docker.md). Cluster walkthrough, probes, HPA and
the defect list are in [docs/part3-kubernetes.md](docs/part3-kubernetes.md).
The machine-readable API reference (OpenAPI 3.1, validated and rendered) is
[docs/openapi.yaml](docs/openapi.yaml) - serve it with
`docker run -p 8085:8080 -e SWAGGER_JSON=/specs/openapi.yaml -v $PWD/docs:/specs swaggerapi/swagger-ui`.

Progress: **Parts 1 (Frappe app), 2 (Docker) and 3 (Kubernetes) are done and were
run on this machine.** Part 4 CI/CD, Part 5 observability, Part 6 DB ops/security
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

## Part 3 status: Kubernetes — the whole stack runs on k3s

```
k8s/base/kustomization.yaml     # 40 objects, one apply
k8s/base/configmap.yaml         # non-secret env for all 7 frappe roles
k8s/base/secret.example.yaml    # -> secret.yaml (git-ignored; kustomize fails without it)
k8s/base/pvc.yaml               # sites + logs, the shared Frappe filesystem
k8s/base/mariadb.yaml           # headless Service + StatefulSet (or delete for an external DB)
k8s/base/redis.yaml             # redis-cache (Deployment) + redis-queue (StatefulSet, AOF)
k8s/base/jobs.yaml              # create-site, migrate  (+ the PVC-seeding initContainer)
k8s/base/{backend,frontend,websocket,scheduler,workers}.yaml   # the 7 roles, 1 image
k8s/base/hpa.yaml               # autoscaling/v2, CPU 70%, 2-6 replicas, asymmetric behavior
k8s/base/networkpolicy.yaml     # default-deny + 13 allows (both directions, see docs)
k8s/base/ingress.yaml           # traefik now, cert-manager TLS block commented
k8s/tls/issuer.yaml             # ClusterIssuers, kept out of base on purpose
k8s/overlay-local-registry/     # same manifests + hostAliases, image from localhost:5000
k8s/overlay-registry/           # same manifests, image from ghcr.io (the CI path)
```

```bash
cp k8s/base/secret.example.yaml k8s/base/secret.yaml    # 3 values; kustomize refuses without it
kubectl apply -f k8s/base/namespace.yaml                # Namespace first (see docs §2)

# get the image into k3s without root (docs §11): registry on localhost + overlay
docker run -d --name tb-registry -p 5000:5000 registry:2
docker tag tenantbooks:v15.103.0 localhost:5000/tenantbooks:v15.103.0 && docker push localhost:5000/tenantbooks:v15.103.0
kubectl apply -k k8s/overlay-local-registry
kubectl -n tenantbooks wait --for=condition=Complete --timeout=15m job/create-site
# Kubernetes has no Job DAG: `migrate` races `create-site` and fails first. Re-apply the
# same overlay (via kustomize, so the image name is rewritten) and only the missing Job is
# created:
kubectl -n tenantbooks delete job --ignore-not-found migrate
kubectl kustomize k8s/overlay-local-registry | kubectl apply -f -
kubectl -n tenantbooks wait --for=condition=Complete --timeout=10m job/migrate
```

Measured on this machine (k3s 1.36.5, single node, full transcript in
[docs/part3-kubernetes.md](docs/part3-kubernetes.md)):

| Claim | Evidence |
| --- | --- |
| every manifest is schema- and admission-valid, and really runs | `kubectl apply -k` -> every object `created/configured`; all 10 workloads `1/1` |
| image loads into k3s with no root | `localhost:5000` push/pull over plain HTTP: `Successfully pulled ... in 1m1.775s (725 MB)` |
| the bootstrap Jobs work against the in-cluster DB | `job/create-site Completed` (`create-site done`), `job/migrate Completed`, seed -> `{"customers": 2,"products": 3}` |
| REST through the Ingress creates real documents | `POST .../create_order` -> `HTTP 200` in 0.99 s, `TB-ORD-2026-0002` + `TB-INV-2026-00002` |
| the long queue serves site-scoped jobs | `home-frappe-frappe-bench:long: Job OK (tenantbooks.localhost::tenantbooks-pdf-TB-INV-2026-00002)` |
| PDF lands on the shared volume and is served back | 16,974 B in `sites/tenantbooks.localhost/public/files/`, `GET /files/...pdf` -> `200`, `PDF document, version 1.4, 1 page(s)` |
| HMAC webhook delivered and verified across the namespace | receiver `ACCEPTED invoice.submitted invoice=TB-INV-2026-00002` from the worker pod IP |
| failed delivery retried **by the scheduler pod** | `?fail=1` -> `REJECTED forced failure` + row `Retrying / 500 / attempts=1 / next_retry_at +69 s`; URL restored; the `*/5` sweep re-sent it -> `ACCEPTED`, row now `Sent / 200 / attempts=2` |
| the scheduler pod runs cron events | `tabScheduled Job Type`: `*/5 * * * *` row with `last_execution` today, `get_scheduler_status` -> `active` |
| HPA really moves replicas | target lowered -> `SuccessfulRescale New size: 4`, then `6`; restored -> walked back down |
| NetworkPolicy blocks *and* the app still works | 14 policies; each denial became a measured fix: 502 -> 200 (missing `backend-ingress`), `HostNotFoundError` -> `Job OK` (missing `frappe-to-ingress`) |
| rolling update with `maxUnavailable: 0` | every template change: `deployment "backend" successfully rolled out`, no window with `available < desired` |

Thirteen real bugs were caught by running the manifests rather than writing them (the numbered
list at the bottom of this file, items 12-24; docs §15 has the same set with the commands):
a Kubernetes PVC is **not** pre-populated from the image the way a Docker named volume is
(so `apps.txt` and `/sites/assets` were missing -> seeding initContainer, which itself needs
`cp -R` because uid 1000 cannot preserve mtimes); a NetworkPolicy egress allow with no
matching ingress allow is a silent timeout; `mysqladmin` does not exist in `mariadb:11.4` so
the DB probe died on `command not found`; a headless Service publishes no DNS record while its
pod is NotReady, which makes a broken probe look like a DNS failure; `volumes:` without
`volumeMounts:` leaves a worker `1/1 Running` while it talks to the image's empty `sites/`;
deleting the PVC does not delete the site, because the database and its generated user live in
MariaDB; the site's own `host_name` must resolve *inside* the cluster, and to the Ingress
controller's ClusterIP rather than to the LoadBalancer address; `bench console` piped from a
heredoc silently executes nothing.

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
5. `bench console` has no auto-commit, so the API key/secret pair generated there was
   never written; `frappe.db.commit()` fixed it. (`bench execute` *does* commit - it ends
   with `if frappe.db: frappe.db.commit()` - which is why this bit us only once we moved
   the same code into a piped console session.)
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
16. (Part 3) `volumes:` without `volumeMounts:` is **valid YAML that does nothing**: the
    scheduler and both workers kept `1/1 Running` while reading the image's empty `sites/`
    (`cat sites/common_site_config.json` -> `{}`) and died on `Error 111 connecting to
    127.0.0.1:11311`. Nothing about the pod looks wrong; you have to look at the filesystem.
17. (Part 3) **`cp -a` fails on a PVC as uid 1000** - `preserving times for '/seed/':
    Operation not permitted`, exit 1, *after* copying nearly everything. `cp -R` + `sync` +
    an assertion on a marker file, so a partial copy cannot pass as seeded.
18. (Part 3) **deleting the volume does not delete the site.** Frappe's state has two homes:
    `sites/<name>/` on the PVC *and* a real database plus a generated MariaDB user. After a
    wipe: `Database _f471a2d288e811b8 already exists`, and once that is dropped,
    `(1045, "Access denied for user '_f471a2d288e811b8'@'10.42.0.33'")`.
19. (Part 3) default-deny **ingress** starved the app tier while it looked healthy: nginx
    logged `connect() failed (111: Connection refused) while connecting to upstream` and
    users got 502, with `backend 2/2` and populated Endpoints. `backend-ingress` and
    `websocket-ingress` were the missing pair.
20. (Part 3) the site's `host_name` has to work **from inside a pod**, and the details are
    not intuitive: the port is the Ingress's (80), not gunicorn's (8000) or nginx's (8080);
    the name needs `hostAliases` because `.localhost` is not DNS; and the alias must point at
    the Ingress controller's **ClusterIP**, because the LoadBalancer address is a hostPort hop
    into k3s's svclb pod that no `podSelector` rule can match. Measured from a worker pod with
    the same Host header: ClusterIP -> 200, LB address -> refused. `curl` in the pod also
    lies, resolving `*.localhost` to `127.0.0.1` regardless of `/etc/hosts` - verify with the
    interpreter your app actually uses (`socket.getaddrinfo` -> the right IP, HTTP 200).
21. (Part 3) a kustomize `patches:` entry with `target:`/`labelSelector` still needs
    `apiVersion` + `kind` + a dummy `metadata.name` in the patch file (kubectl 1.36), or it
    fails with `unable to parse SM or JSON patch`.
22. (Part 3) **`bench console` piped from a heredoc runs nothing** - IPython reads stdin but
    answers `Do you really want to exit ([y]/n)?`. `bench execute` is the scriptable door.
    Related: `kubectl wait --for=condition=Ready pod --all` hangs forever in a namespace with
    completed Jobs (a `Completed` pod is never `Ready`).
23. (Part 3) in v15 a `Scheduled Job Type` row is named `tasks.retry_pending_webhooks`, not
    the dotted path, so `where name like '%tenantbooks%'` looks like an unregistered event -
    filter on `method`. The old `create_type` column is gone, and `last_execution` is in the
    site's time zone while pod logs print UTC (six hours apart here).
24. (Part 3) **a `kubectl patch` on a ConfigMap is one `kubectl apply` away from being
    reverted.** Patched `WEBHOOK_URL` to the failing endpoint, then applied the overlay to
    re-run the bootstrap Job - the apply restored the file's value and the test silently
    passed nothing. GitOps truth: the file is the source of state, so an experiment has to
    re-create *only* the object it needs (`kubectl create -f` a single extracted Job), not
    `apply -k` the whole stack.

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
- (Part 3) **cert-manager TLS**: the `tls:` block and `k8s/tls/issuer.yaml` are written
  but no certificate was issued here - HTTP-01 needs a publicly resolvable hostname.
- (Part 3) **multi-node / RWX storage**: reasoned about, not observed - one node, one RWO
  provisioner. Everything in Part 3 runs on a single k3s node with `localhost:5000`.
