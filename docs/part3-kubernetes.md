# Part 3 — Kubernetes

Everything needed to run TenantBooks on a cluster: Deployments for the seven Frappe
roles, a MariaDB StatefulSet, two Redis, shared storage, bootstrap Jobs, autoscaling,
network policy and an Ingress. One `kubectl apply -k` and the whole stack exists.

Verified on this machine's single-node **k3s 1.36.5** cluster (namespace `tenantbooks`).
The whole stack runs: both bootstrap Jobs complete, all seven Frappe roles are Ready, an
Order submitted over REST through the Ingress produces a PDF on the shared volume and an
HMAC-signed webhook the receiver accepts. How the image got into a cluster that cannot see
Docker's store, without root, is [here](#11-loading-the-image-into-the-cluster).

```
tenant-books/k8s/
├── base/
│   ├── kustomization.yaml     # the list of everything below + namespace
│   ├── namespace.yaml         # tenantbooks
│   ├── configmap.yaml         # non-secret env for every frappe process
│   ├── secret.example.yaml    # template -> copy to secret.yaml (git-ignored)
│   ├── pvc.yaml               # sites + logs, shared by all pods
│   ├── mariadb.yaml           # headless Service + StatefulSet
│   ├── redis.yaml             # redis-cache (Deployment) + redis-queue (StatefulSet)
│   ├── jobs.yaml              # create-site, migrate  (the one-shot bootstrap)
│   ├── backend.yaml           # gunicorn, + Service            <- HPA targets this
│   ├── frontend.yaml          # nginx,    + Service            <- Ingress points here
│   ├── websocket.yaml         # socket.io, + Service
│   ├── scheduler.yaml         # bench schedule (Recreate, replicas 1)
│   ├── workers.yaml           # worker-short, worker-long
│   ├── hpa.yaml               # autoscaling/v2 on CPU
│   ├── networkpolicy.yaml     # default-deny + 10 targeted allows
│   └── mock-receiver.yaml     # demo webhook sink (delete for a real deployment)
├── tls/issuer.yaml            # cert-manager ClusterIssuers (NOT in base, see below)
└── overlay-registry/          # same manifests, image pulled from ghcr.io
```

---

## 1. The shape of it

```
                 Ingress (traefik / cert-manager)
                          │
                     frontend :8080  nginx ──── serves /assets, /files from the PVC
                       │        │
              backend :8000   websocket :9000        gunicorn × N  ← HPA
                       │        │
        ┌──────────────┴────────┴──────────────┐
        │ db :3306   redis-cache :6379   redis-queue :6379 │
        └──────────────────────────────────────┘
   other consumers of the same three services: scheduler, worker-short, worker-long,
   and the two Jobs — all seven are containers of ONE image with different commands.
```

**Why seven Deployments from one image.** Frappe's processes are all the same Python
program (`bench`) started with a different subcommand: `start.sh` for gunicorn,
`bench worker --queue long` for the PDF/webhook worker, `bench schedule` for cron. The
image from Part 2 therefore contains one binary set and the cluster decides *which role
a copy of it plays* through `command:`. This is the difference between one image and one
*service*: seven pods, one artifact, one CVE scan.

**Why a PVC at all (the part that has no Docker equivalent).** A Frappe *site* is a
directory (`sites/tenantbooks.localhost/`) holding `site_config.json` (that tenant's DB
credentials) plus `private/files/` and `public/files/` (uploads, generated PDFs). Every
role must see the same directory: a worker writes `TB-INV-2026-00001.pdf`, the web pod
must serve it. So `sites` is a shared volume, not per-pod storage — which is exactly why
the access-mode question in §4 matters.

---

## 2. Apply order

```bash
cp k8s/base/secret.example.yaml k8s/base/secret.yaml   # required, see below
$EDITOR k8s/base/secret.yaml                            # DB_ROOT_PASSWORD, ADMIN_PASSWORD, WEBHOOK_SECRET

kubectl apply -f k8s/base/namespace.yaml                # 1. the Namespace, on its own
kubectl apply -k k8s/base                               # 2. everything else
kubectl -n tenantbooks wait --for=condition=Complete --timeout=15m job/create-site
kubectl -n tenantbooks rollout status deploy/backend
```

Three things about this order that are easy to get wrong and are *measured* here:

* **`secret.yaml` is git-ignored, and `kustomize build` fails without it**
  (`error: ... missing file`). That is deliberate: the build refuses to produce a manifest
  set that silently ships placeholder credentials. Part 6 replaces this file with a
  Sealed Secret or an ExternalSecret, so the ciphertext — not the secret — lives in git.
* **The Namespace must be created before a server-side dry-run of the rest.**
  `kubectl apply -k k8s/base --dry-run=server` creates the Namespace only in the dry run,
  so every remaining object fails with `Error from server (NotFound): namespaces
  "tenantbooks" not found` (34 lines in the run below, one per object; the count moves
  with the manifest set, the failure does not). Reproduce:
  ```
  $ kubectl apply -k k8s/base --dry-run=server
  namespace/tenantbooks created (server dry run)
  Error from server (NotFound): error when creating "k8s/base": namespaces "tenantbooks" not found
  ... (x34)
  ```
  After `kubectl apply -f k8s/base/namespace.yaml`, the same command returns
  `created (server dry run)` for every object — 37 at that first measurement, 40 now
  (14 NetworkPolicies among them) — i.e. the real API server, with its real admission
  plugins and CRDs, accepted every document. The count is not decoration: a `kustomization.yaml`
  that omits a file fails silently, so `kubectl kustomize | grep -c '^kind:'` is how you
  notice.
* **Kubernetes has no Job DAG.** `create-site` and `migrate` are two independent Jobs; the
  only ordering guarantee is the one in the commands above. Part 4's pipeline makes it
  explicit with `kubectl wait --for=condition=complete job/create-site` between steps.

---

## 3. Configuration split: ConfigMap vs Secret

| File | Holds | Consumed by |
| --- | --- | --- |
| `configmap.yaml` | `DB_HOST`, `DB_PORT`, `REDIS_CACHE`, `REDIS_QUEUE`, `SOCKETIO_PORT`, `SITE_NAME`, `FRAPPE_HOST_NAME`, `WEBHOOK_URL`, `GUNICORN_*` | every frappe pod via `envFrom` |
| `secret.yaml` | `DB_ROOT_PASSWORD`, `ADMIN_PASSWORD`, `WEBHOOK_SECRET` | the two Jobs (+ receiver) |

The interesting row is who does **not** get the Secret. The `backend` Deployment mounts
only the ConfigMap, because request handlers read the site's own low-privilege DB user
out of `sites/tenantbooks.localhost/site_config.json` — written once by the `create-site`
Job. Handing every internet-facing pod the MariaDB root password would widen the blast
radius of one compromised pod and buy nothing. The same logic is why `WEBHOOK_SECRET`
appears only where it is used to *sign* (the Job that configures the site) and in the
receiver that verifies it.

`SITE_NAME` is the single source of truth for the tenant identity: `bench new-site` uses
it, nginx sends it as `X-Frappe-Site-Name`, and the Ingress rule matches it as `Host`.
One place it is duplicated anyway: probe `httpHeaders` values cannot reference env vars
(`$(SITE_NAME)` is not expanded by Kubernetes), so the backend probe carries the literal
`tenantbooks.localhost`. If you rename the site, change both.

---

## 4. Storage: `pvc.yaml`

Two claims, `sites` (5Gi) and `logs` (2Gi), mounted by every pod that touches the bench.

`ReadWriteMany` is what the design actually wants — web pods, workers and the scheduler
all write into it (uploaded files, generated PDFs, tracebacks). It is `ReadWriteOnce`
here because this cluster's only StorageClass (`local-path`) offers nothing else and the
cluster has one node, so "attach to one node" is satisfied trivially. On a multi-node
cluster the same manifest produces:

```
0/1 nodes are available: pod has unbound immediate PersistentVolumeClaims
... FailedAttachVolume: multi-attach error   # second pod, different node
```

The fix is one line (`accessModes: [ReadWriteMany]`) plus an RWX-capable provisioner
(NFS `nfs-subdir-external-provisioner`, Longhorn, CephFS, EFS, or a managed `ReadWriteMany`
StorageClass). Nothing else in the stack changes. Verified property of the current setup:
`kubectl -n tenantbooks get pv` shows all four volumes as `[ReadWriteOnce]` host-local
paths, and both `sites` and `logs` were bound only after a consumer appeared — the
`WaitForFirstConsumer` mode, which is what makes the topology decision late.

**A PVC is never pre-populated.** This is the biggest silent difference from Docker and it
produced the one Job-only initContainer in `jobs.yaml`:

```bash
if [ ! -f /seed/apps.txt ]; then cp -a /home/frappe/frappe-bench/sites/. /seed/; fi
```

A Docker named volume is copied from the image on first use, which is how the Part 2
compose stack got `apps.txt` and the built `sites/assets/*` for free. A Kubernetes PVC is
born empty, and mounting it at `sites/` *hides* those files, so every pod dies with
`Site "..." does not exist` and nginx serves 404s for CSS. The initContainer mounts the
PVC at `/seed` — a path that is not its final home, so the image's own copy is still
visible — and copies it across once.

Verified that the ownership convention survives the volume (`securityContext:
runAsUser: 1000, runAsGroup: 0, fsGroup: 0`, the same as every app pod):

```
uid=1000 gid=0(root) groups=0(root)
drwxrwsrwx 1 root root 0 /sites
drwxrwsrwx 1 root root 0 /logs
WRITE_OK                       # file created as 1000:root
```

`fsGroup: 0` is what makes that work: without it the provisioned directory belongs to
root and uid 1000 cannot create the site directory. (k3s ran these pods as root by
default; the explicit uid means an image that forgets `USER frappe` fails here loudly
instead of quietly running as root.)

---

## 5. Data services

**MariaDB — `mariadb.yaml`.** StatefulSet (1 replica) + *headless* Service `db`.
Chosen over a Deployment because the database needs a stable identity and its volume
re-attached to the same pod ordinal, not recreated by a ReplicaSet. Probe is `exec`, not
`tcpSocket`: the TCP port opens before InnoDB finishes crash recovery, so "can connect"
≠ "can serve". To run against a managed database instead, delete both documents in this
file and set `DB_HOST`/`DB_PORT` in the ConfigMap + `DB_ROOT_PASSWORD` in the Secret to
the external instance — nothing else changes, which is the point of the ConfigMap split.

**Redis — `redis.yaml`.** Two instances because they fail differently:
* `redis-cache`: `--maxmemory 128mb --maxmemory-policy allkeys-lru`, plain Deployment, no
  volume. Losing it costs recomputed caches only.
* `redis-queue`: `--appendonly yes --maxmemory-policy noeviction`, **StatefulSet** with a
  1Gi volume. It holds the job queue, so eviction would silently delete pending webhook
  and PDF jobs; `noeviction` makes a full queue an error instead.

That pair is also the reason the stack uses two workload kinds: a workload with a
volume-to-pod identity relationship is a StatefulSet, one without is a Deployment.

Verified state on this machine:

```
$ kubectl -n tenantbooks get pods
NAME                           READY   STATUS    RESTARTS
db-0                           1/1     Running   0
redis-cache-645546f9b4-h74kj   1/1     Running   0
redis-queue-0                  1/1     Running   0
```

---

## 6. The seven Frappe roles

Common to all: the same image `tenantbooks:v15.103.0`, `envFrom` the ConfigMap, `sites`
(+ `logs` where a bench process writes them) mounted, `runAsUser: 1000 / runAsGroup: 0 /
fsGroup: 0`, and `terminationGracePeriodSeconds` longer than the upstream timeout so
SIGTERM can drain in-flight requests.

| Workload | Command | Replicas | Strategy | Probes | Requests / Limits |
| --- | --- | --- | --- | --- | --- |
| `backend` | image CMD (`docker/custom/start.sh` → gunicorn) | 2 (HPA 2–6) | RollingUpdate `maxUnavailable: 0, maxSurge: 1` | startup+readiness+liveness `httpGet /api/method/ping` **with `Host` header** | 100m/400Mi, 1 core/1Gi |
| `frontend` | `nginx-entrypoint.sh` | 2 | same, `maxUnavailable: 0` | same path, **no Host header** | 50m/64Mi, 0.5/256Mi |
| `websocket` | `node .../socketio.js` | 1 | same | `tcpSocket :9000` (a bare GET to socket.io returns 400) | 50m/128Mi, 0.5/384Mi |
| `scheduler` | `bench schedule` | **1, must stay 1** | `Recreate` | none (see below) | 50m/256Mi, 0.5/768Mi |
| `worker-short` | `bench worker --queue short,default` | 1 | `maxUnavailable: 0` | none | 100m/300Mi, 1/768Mi |
| `worker-long` | `bench worker --queue long` | 1 | `maxUnavailable: 0` | none | 200m/512Mi, 2/1.5Gi |
| Jobs | `bash -c` bootstrap | 1 each | n/a (`restartPolicy: Never`) | n/a | 200m/512Mi, 2/1.5Gi |

Four decisions in that table worth defending in an interview:

1. **The backend probe must carry a `Host` header.** Frappe resolves the site from the
   Host header, so a probe at the pod IP returns **404 on a perfectly healthy gunicorn**.
   This was chased down in Part 2 (compose showed `Up (unhealthy)` while serving real
   traffic) and it is written into `backend.yaml` as the literal
   `tenantbooks.localhost`. The frontend needs no header because nginx is told the site
   name through `FRAPPE_SITE_NAME_HEADER` and adds it upstream.
2. **No liveness probe on scheduler/workers, on purpose.** `bench schedule` and `bench
   worker` are PID 1; if they die, the container dies and the Deployment replaces it, so a
   probe would only re-ask "is python still running" — and `restartPolicy` already covers
   it. The failure that matters is "jobs stopped being *picked up*", whose only real signal
   is queue backlog / job age. That is Part 5's Prometheus alert, not a kubelet probe. A
   cheap intermediate (broker reachable) is available if you want it:
   `env/bin/python -c "import redis;redis.Redis('$REDIS_QUEUE',6379).ping()"` — redis-py is
   in the image (verified: `redis-py 4.5.5`).
3. **`scheduler` is `Recreate`, not RollingUpdate.** `maxSurge: 1` would run two
   schedulers briefly and double-fire every cron entry (the webhook retry sweep is not
   idempotent — two concurrent sweeps burn the retry budget); `maxUnavailable: 0` on a
   single replica means the same thing. Recreate accepts a few seconds with no scheduler,
   which is honest for a singleton without leader election.
4. **CPU is requested but the limit is deliberately loose, memory is capped tightly.**
   A CPU limit turns into kernel-enforced throttling exactly when the service is busiest,
   which looks like a latency spike with a healthy pod. The memory limit is the one that
   protects the node, and `GUNICORN_MAX_REQUESTS=5000` recycles workers before they grow
   into it. `worker-long`'s limit is the highest in the stack because wkhtmltopdf runs as
   *its* child process — the PDF's memory comes out of that container's cgroup.

`requests` are also what the HPA and the scheduler act on, so they are set to values I
want to be scaled against, not to the minimum that fits.

---

## 7. Autoscaling — `hpa.yaml`

`autoscaling/v2`, targeting `Deployment/backend` only, 2–6 replicas, 70 % average CPU
*utilization* (measured against the 100m **request**, not the 1-core limit), with
asymmetric behavior: scale up in 30 s windows, scale down only after 5 min of calm and at
most one pod every 2 min — a gunicorn pod that is discarded loses its warm Python imports
and asset cache, so flapping is expensive in a way it is not for a stateless Go service.

Only the web tier is autoscaled. Workers scale with queue depth (an external metric in
Part 5), nginx is nearly free, and the scheduler must never be scaled at all.

Verified: the object is accepted and renders exactly as written on this cluster
(metrics-server is present, which is what makes CPU utilization computable at all):

```
$ kubectl -n tenantbooks describe hpa backend
  Scale Up:  Stabilization Window: 30 seconds ... Percent 100 / 30s
  Scale Down: Stabilization Window: 300 seconds ... Pods 1 / 120s
```

If metrics-server were missing the target reads `<unknown>` and the events say `unable to
fetch metrics from resource metrics API` — the single most common reason an HPA "does
nothing". A second failure mode shows up in the events when the HPA is applied *before* its
Deployment: `FailedGetScale  deployments.apps "backend" not found`, repeated until the
target exists (measured: 140 of those events before the web tier came up).

**Scaled in both directions, measured.** Rather than burn a laptop at 1.0 GB free generating
real CPU, the target was lowered so the same controller logic would act:

```bash
# force a scale-up (the metric is utilization *of the request*, so 1 % is reachable)
kubectl -n tenantbooks patch hpa backend --type=merge -p \
  '{"spec":{"metrics":[{"type":"Resource","resource":{"name":"cpu","target":{"type":"Utilization","averageUtilization":1}}}]}}'
kubectl -n tenantbooks get hpa backend -w
# events: New size: 4; reason: cpu resource utilization (percentage of request) above target
#         New size: 6; ...
# then restore 70 and watch it walk back: 6 -> 5 -> 4 -> 3 -> 2, one pod per 120 s
```

Two notes from doing it: `target` requires `type: Utilization` explicitly (a merge patch
without it is rejected with `must specify a metric target type`), and a `--type=merge` patch
on `spec.metrics` *replaces the whole array* — list every metric you want to keep.

---

## 8. Network policy — `networkpolicy.yaml`

Default-deny for the namespace (both directions), then thirteen targeted allows —
fourteen `NetworkPolicy` documents in `kubectl get netpol`.

**Deviation from the brief, deliberate:** the spec asked for "a policy that allows only
the web pods to reach MariaDB". Literally applied, that breaks the application — in
Frappe *every* bench process talks to the database: workers run the ORM to render a PDF,
the scheduler runs the ORM for the overdue sweep, and the bootstrap Jobs are nothing but
ORM calls. The allow-set here is therefore `tier: frappe` (web + websocket + scheduler +
workers + Jobs), which still excludes the receiver, any debug pod you spin up, and every
other namespace. The rules are split one-per-dependency so you can narrow them per role
without rewriting the file.

The set: `default-deny`, `allow-dns-egress`, `allow-ingress-to-frontend`,
`frontend-to-app` (backend:8000, websocket:9000), `frappe-to-db`, `frappe-to-redis`,
`frappe-to-frontend`, `frappe-to-ingress`, `frappe-to-webhook-receiver` (+ the commented
production shape with `ipBlock: 0.0.0.0/0 except 10/8, 100.64/10, 172.16/12,
192.168/16` on 443), and the matching ingress sides `db-ingress`, `redis-ingress`,
`receiver-ingress`, `backend-ingress`, `websocket-ingress`.

Four rules are only obvious if the app has already failed once:

* `backend-ingress` / `websocket-ingress` — with default-deny *ingress*, a Service can be
  full of Ready endpoints and still refuse every connection. nginx reported
  `connect() failed (111: Connection refused) while connecting to upstream` and the browser
  got a 502, while `kubectl get pods` looked perfect.
* `frappe-to-frontend` — the long worker renders a PDF with wkhtmltopdf, which fetches
  `/assets/*` from the site URL (`host_name`). A worker that cannot reach nginx produces a
  truncated or failed PDF. Same failure as Part 1's bug 6, re-derivable from the policy file.
* `frappe-to-ingress` — the app fetches **its own public URL** to print an invoice, so the
  request goes pod → Ingress controller → frontend → gunicorn. Without this allow,
  wkhtmltopdf says `Exit with code 1 due to network error: HostNotFoundError` even though
  the name resolves fine. See §9 for the second half of that story.
* `allow-dns-egress` — without it `db` never resolves and every client fails with a DNS
  timeout that is indistinguishable from a refused connection.

**Enforcement, measured on this cluster** (busybox pods with different labels, `nc -zv -w4`):

| Source pod (label) | Target | Result |
| --- | --- | --- |
| `labelfl` (`tier: frappe`) | `db:3306` | `db (10.42.0.18:3306) open` — 0.2 s |
| `rogue` (`tier: debug`) | `db:3306` | no response (dropped), exit 1 |
| `labelfl` (`tier: frappe`) | `redis-cache:6379` | `redis-cache (10.43.116.29:6379) open` — 0.1 s |
| `rogue` (`tier: debug`) | `redis-cache:6379` | no response (dropped), exit 1 |
| `rogue` (`tier: debug`) | `redis-queue:6379` | no response (dropped), exit 1 |

```bash
kubectl -n tenantbooks run labelfl --image=busybox:1.36 --restart=Never \
  --labels=tier=frappe --command -- sleep 3600
kubectl -n tenantbooks run rogue  --image=busybox:1.36 --restart=Never \
  --labels=tier=debug  --command -- sleep 3600
kubectl -n tenantbooks exec labelfl -- nc -zv -w4 db 3306      # open
kubectl -n tenantbooks exec rogue  -- nc -zv -w4 db 3306       # hangs, then exit 1
kubectl -n tenantbooks delete pod labelfl rogue               # tidy up
```

k3s enforces this with its embedded kube-router selector, so no Cilium is required — but
that is cluster-specific: **on a cluster whose CNI ignores NetworkPolicy (plain Docker,
kind without a policy-enforcing CNI, some managed defaults) every one of these rules is a
no-op that still validates and still prints "applied".** Confirm enforcement with the two
`nc` commands above, never by reading the policy back.

---

## 9. Ingress and TLS — `ingress.yaml`, `k8s/tls/`

Written for this machine (traefik is the installed IngressClass, plain HTTP) with the TLS
lines present but commented, because cert-manager is not installed here.

The Ingress rule's `host:` **is** the multi-tenancy mechanism: Frappe picks the site from
the Host header, so a new tenant means a new site directory and a new rule — no change to
the app, the Deployments, or the ConfigMap. That is the single-site demo's honest limit
(see "Not verified" below), but the routing seam is already in place.

`k8s/tls/issuer.yaml` is deliberately **not** in `kustomization.yaml`: applying a
`cert-manager.io/v1` document on a cluster without cert-manager fails with
`no matches for kind Issuer in version "cert-manager.io/v1"`, which would make the whole
stack look broken when only the optional add-on is missing. Install, then apply, then
uncomment the `tls:` block:

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
kubectl -n cert-manager wait --for=condition=Available deploy --all --timeout=180s
kubectl apply -f k8s/tls/issuer.yaml      # staging issuer first: production rate-limits failed validations
```

Staging before production, because Let's Encrypt limits a domain to five blocked
registrations a week and a typo in an annotation burns that budget silently.

### `host_name` is a routing decision, not a cosmetic one

Frappe builds every absolute URL — print links, email links, the URL wkhtmltopdf is told to
fetch — from the site's `host_name`. Three separate failures hide in that one value, all
measured here:

1. **Port.** `host_name` must carry the port a *client* uses (the Ingress, 80), not the port
   gunicorn listens on (8000) or the one nginx listens on (8080). Changing it moves nothing;
   gunicorn binds `${GUNICORN_BIND}`. The `webserver_port` in `site_config.json` is likewise
   only used to build URLs.
2. **DNS.** `tenantbooks.localhost` is not a name any resolver knows. Inside a pod, the PDF
   job failed with `HostNotFoundError` until the pod could resolve it — in the compose stack
   the frontend's network alias did that for free, in Kubernetes it is `hostAliases`, which
   lives in the laptop overlay only (`k8s/overlay-local-registry/host-aliases.yaml`). On a
   real cluster with a real domain, DNS already does it and the patch should be deleted.
3. **Which address to alias to.** Counter-intuitively, alias the name to the Ingress
   controller's **ClusterIP**, not to the `LoadBalancer` address the browser uses. Measured
   from a `tier: frappe` pod, same `Host` header: `http://10.43.178.99/` → 200,
   `http://192.168.1.157/` → connection refused. The VIP is an ordinary pod-to-pod hop that
   the `frappe-to-ingress` selector can match; the LB address is a hostPort hop into k3s's
   `svclb` pod, whose IP matches no Traefik selector, so default-deny eats it. Widening the
   policy to a whole subnet to "fix" that is the wrong lesson.

And one trap while debugging it: **`curl` inside the pod lies.** Even with the right
`/etc/hosts` line, `curl http://tenantbooks.localhost/` reported `Trying 127.0.0.1:80` (curl
special-cases the `.localhost` suffix), while `getent hosts` and Python's `getaddrinfo` —
what the application actually uses — returned the correct IP and got a 200. Debug name
resolution with the interpreter your app runs on, not with curl.

---

## 10. Rolling updates and rollback

Every long-running workload uses:

```yaml
strategy: {type: RollingUpdate, rollingUpdate: {maxUnavailable: 0, maxSurge: 1}}
```

with the reasoning recorded in `backend.yaml`: during an upgrade the old pod is only torn
down after the new one passes its readiness probe, so the number of serving pods never
drops below the replica count. The cost is one extra pod of headroom on the node during
the rollout — which on this laptop-sized node is exactly why `maxSurge: 1` needs the
requests in §6 to be honest numbers rather than aspirational ones. If the new pods never
become Ready, the rollout stops by itself with the old pods still serving; that is the
whole point.

```bash
# what a rollout is doing right now
kubectl -n tenantbooks rollout status deploy/backend
kubectl -n tenantbooks rollout history deploy/backend
kubectl -n tenantbooks rollout history deploy/backend --revision=3

# rollback: re-applies the previous pod template, image tag included
kubectl -n tenantbooks rollout undo deploy/backend
kubectl -n tenantbooks rollout undo deploy/backend --to-revision=2

# the two things that make `undo` meaningful
kubectl -n tenantbooks get rs -o wide          # which ReplicaSet, which image
kubectl -n tenantbooks describe deploy/backend | grep -A3 Image
```

Three rollback facts that are not obvious until they bite, plus two measured in the drill:

* **`rollout undo` only helps if the previous template points at an image that still
  exists unchanged.** A `:latest` or `:main` tag means "undo" rolls forward into the same
  broken bytes. That is why Part 4 tags images with the git SHA and why
  `k8s/overlay-registry/kustomization.yaml` says so in its comment.
* **Jobs are not rolled back.** A finished Job's `.spec.template` is immutable, so
  `kubectl apply` cannot re-run or change one; delete and create instead. Undoing a
  schema migration is not a `kubectl` operation at all — that is Part 6's
  expand/contract rule (never drop a column in the migration that breaks the previous
  release, so the old code can run against the new schema).
* **`rollout history` is useless until you label the revisions.** Every revision prints
  `CHANGE-CAUSE <none>` unless something writes
  `kubernetes.io/change-cause: "..."` into the pod template annotations at deploy time —
  one line in the CI job, and it is what makes `--to-revision=2` a decision instead of a
  guess.
* **An undo is a new revision, not a time machine.** Measured: rev 4 (`backend-56f47888f9`,
  one annotation added) → `rollout undo` → the *old* ReplicaSet `backend-d5bb4dcf8` came back
  with `desired=2 ready=2` carrying **revision 5**, rev 4 went to 0, and
  `/api/method/ping` through the Ingress returned 200 the whole way. `maxUnavailable: 0`
  held: `rollout status` reported success with no window where available fell below desired.
* **After an undo, re-apply the file.** kubectl warns verbatim: *"Rolling back will not
  update the `kubectl.kubernetes.io/last-applied-configuration` annotation, which may cause
  unexpected behavior on future `kubectl apply` operations."* The live template and the
  recorded "last applied" config now disagree, so the next apply of the *same* manifest can
  be a silent no-op. `kubectl kustomize … | kubectl apply -f -` puts the cluster back under
  file management.

* **A StatefulSet whose new template fails its probe deadlocks the rollout** and prints
  `Waiting for 1 pods to be ready` forever. Measured here: after fixing the broken startup
  probe in `mariadb.yaml`, the StatefulSet spec showed the new probe
  (`kubectl get sts db -o jsonpath='{.spec.containers[0].startupProbe.exec.command}'`)
  while `db-0` kept running the old one, because the controller will not replace a pod
  that never reached Ready. The unstick is `kubectl -n tenantbooks delete pod db-0`
  (the data volume is untouched; the pod comes back on the new revision).

---

## 11. Loading the image into the cluster

k3s runs its own containerd, separate from Docker's, so an image built locally is
invisible to it — the pods fail with `ImagePullBackOff: ... docker.io/library/tenantbooks`
because there is no such public repository. Three ways to fix it, in the order I tried them:

**A. Import the tarball — needs root, was unavailable here:**

```bash
docker save -o /tmp/tenantbooks.tar tenantbooks:v15.103.0     # 692 MB, done on this machine
sudo k3s ctr images import /tmp/tenantbooks.tar
sudo k3s ctr images ls | grep tenantbooks    # expect: docker.io/library/tenantbooks:v15.103.0
```

`sudo: a password is required` on this machine and the containerd socket is root-only, so
this path is documented but not the one used. (Docker Desktop on macOS hides the whole
problem — its Kubernetes shares the image store — and `kind` has `kind load docker-image`.)

**B. A registry on `localhost` — what actually unblocked the deploy, no root needed:**

```bash
docker run -d --name tb-registry -p 5000:5000 registry:2
docker tag  tenantbooks:v15.103.0                 localhost:5000/tenantbooks:v15.103.0
docker tag  tenantbooks-mock-receiver:v15.103.0   localhost:5000/tenantbooks-mock-receiver:v15.103.0
docker push localhost:5000/tenantbooks:v15.103.0
docker push localhost:5000/tenantbooks-mock-receiver:v15.103.0
kubectl apply -k k8s/overlay-local-registry
```

Measured: `Successfully pulled ... in 1m1.775s (725302221 bytes)` and `bench --version`
inside the pod → `frappe 15.103.0, tenantbooks 0.0.1`.

No daemon or kubelet configuration is involved, because both clients make the same exception
for a registry named `localhost`: Docker pushes over plain HTTP and k3s's containerd pulls
over plain HTTP. A registry reachable under any *other* name needs `insecure-registries` in
`/etc/docker/daemon.json` **and** a `registries.yaml` on the k3s node — both root-owned files.
That is the entire reason this overlay exists.

The one trap: `imagePullPolicy: IfNotPresent` means a re-pushed content under the **same tag**
is never re-fetched by a node that already has it, so a `rollout restart` silently runs the old
bytes. Push a new tag every time (in CI, the git SHA).

**C. Push to a real registry and use the other overlay** (the CI path, Part 4):

```bash
docker tag tenantbooks:v15.103.0 ghcr.io/<you>/tenantbooks:v15.103.0
docker push ghcr.io/<you>/tenantbooks:v15.103.0
kubectl -n tenantbooks create secret docker-registry ghcr --docker-server=ghcr.io \
  --docker-username=<you> --docker-password=<token>   # + imagePullSecrets on each pod
kubectl apply -k k8s/overlay-registry
```

Each overlay rewrites the image *name* inside the base manifests at build time, so all seven
workloads change together and no per-environment copy of a Deployment file exists. With a
registry reference, `IfNotPresent` is right only when the image is pinned by **digest**; with a
mutable tag you want `Always`.

**One more laptop-only thing the overlay does:** `host-aliases.yaml` adds an `/etc/hosts` entry
for `tenantbooks.localhost` to every `tier=frappe` pod. See §9 — without it, PDF rendering
fails, because `host_name` is a hostname that only DNS outside the cluster knows.

---

## 12. What was verified by running it

| Claim | Evidence (all from this machine) |
| --- | --- |
| Objects render and the API server accepts them | `kubectl kustomize` → clean build; `kubectl apply` → every object `created/configured`, zero errors (namespace first, see §2) |
| MariaDB Ready with exec probes, survives a template change | `db-0 1/1 Running`; `healthcheck.sh --connect --innodb_initialized` exits 0 in-container; STS re-created the pod and the volume re-attached (`partitioned roll out complete`) |
| Redis both instances Ready | `redis-cache-… 1/1`, `redis-queue-0 1/1` |
| PVCs provision on first consumer | events `WaitForFirstConsumer` → PVs bound `[ReadWriteOnce]` |
| Non-root can write the shared volume | probe pod: `uid=1000 gid=0(root)`, `drwxrwsrwx /sites`, `WRITE_OK` |
| Image loads without root | §11 B, `Successfully pulled ... 1m1.775s` |
| Bootstrap Jobs work against the in-cluster DB | `job/create-site 0/1 Completed`, log ends `create-site done`; `job/migrate Completed` |
| Site is real, data is real | `seed.seed` → `{"customers": 2,"products": 3}` |
| REST call through the **Ingress** creates an Order + Invoice | `POST /api/v2/method/tenantbooks.billing.api.create_order` → `HTTP=200 t=0.99s`, `{"name":"TB-ORD-2026-0002","net_total":42.5,"invoice":"TB-INV-2026-00002"}` |
| The long worker picks up site-scoped jobs | `home-frappe-frappe-bench:long: Job OK (tenantbooks.localhost::tenantbooks-pdf-TB-INV-2026-00002)` — the queue name is bench-scoped, the *job id* carries the site, so one worker set serves every tenant |
| PDF rendered and written to the shared volume | `sites/tenantbooks.localhost/public/files/TB-INV-2026-00002.pdf`, 16974 bytes |
| …and served back over HTTP by a *different* pod | `GET /files/TB-INV-2026-00002.pdf` through the Ingress → `200`, 16974 bytes, `file` → `PDF document, version 1.4, 1 page` |
| HMAC webhook delivered and verified | receiver: `ACCEPTED invoice.submitted invoice=TB-INV-2026-00002` from `10.42.0.70` (the worker pod), and a bad signature is rejected with 401 by the same code path |
| retry + back-off **driven by the scheduler pod** | URL pointed at `?fail=1` -> receiver `REJECTED forced failure`, row `Retrying / 500 / attempts=1 / next_retry_at=+69 s`; URL restored; the `*/5 * * * *` sweep re-enqueued it and the receiver logged `ACCEPTED ... TB-INV-2026-00003`, row now `Sent / 200 / attempts=2 / next_retry_at=null` |
| an unknown tenant key is a clean error, not a crash | `create_order` with `customer: "Riverside Press"` -> `{"errors":[{"type":"DoesNotExistError", ...}]}`, **no Order created** |
| Scheduler pod really runs cron events | `tabScheduled Job Type`: `tasks.retry_pending_webhooks`, `cron */5 * * * *`, `stopped 0`, `last_execution` set today; `get_scheduler_status` → `{"status": "active"}` |
| NetworkPolicy blocks and allows | §8 matrix, 14 policies; the deny rows were each turned into an allow-and-retry (502 → 200 for the app tier, `HostNotFoundError` → `Job OK` for the PDF loop) |
| HPA scales **up** and reads the metric | target lowered -> `SuccessfulRescale  New size: 4`, then `New size: 6`; `kubectl top pods` returns per-pod CPU |
| HPA scales **back down** on its own | target restored to 70 % -> 6 → 5 → 4 → 3 → 2 over ~5 min, obeying `stabilizationWindowSeconds: 300` and `scaleDown: 1 pod per 120 s` |
| Rolling update with `maxUnavailable: 0` | every template change: `deployment "backend" successfully rolled out`, no window with `available < desired` |
| Rollback works and is observable | rev 4 → `rollout undo` → old RS `backend-d5bb4dcf8` back at `desired=2 ready=2` as **rev 5**, rev 4 scaled to 0, `/api/method/ping` 200 throughout (§10) |

## 13. Not verified, and how to verify it

What is left needs a second node, a public hostname, or real load:

```bash
# reproduce the webhook retry path (verified once, §12) - note that a later
# `kubectl apply` of the overlay reverts the ConfigMap, so re-run only the Job:
kubectl -n tenantbooks patch configmap tenantbooks-env --type=merge \
  -p '{"data":{"WEBHOOK_URL":"http://mock-receiver.tenantbooks.svc.cluster.local:8071/hook?fail=1"}}'
kubectl -n tenantbooks delete job create-site --wait=true
kubectl -n tenantbooks create -f /tmp/create-site-job.yaml      # extracted from the overlay
kubectl -n tenantbooks wait --for=condition=Complete --timeout=4m job/create-site
# submit an invoice, watch: receiver logs REJECTED, row goes Retrying/500/attempts=1
# then restore the URL, re-run the Job the same way, and the */5 sweep sends it
kubectl -n tenantbooks logs deploy/mock-receiver | grep -E "ACCEPTED|REJECTED"

# the realistic autoscaling test - generate load instead of lowering the target.
# Not done here: the laptop had ~1.0 GB free, so §12 lowered the target instead.
kubectl -n tenantbooks run load --image=busybox:1.36 --restart=Never -- \
  sh -c 'for i in $(seq 1 300); do wget -q -O /dev/null http://backend:8000/api/method/ping --header="Host: tenantbooks.localhost" || true; done'
```

* **cert-manager TLS** — the manifests are schema-valid (`kubectl --dry-run=server` on a
  cluster where the CRDs exist would be needed to prove even that); no certificate was
  issued here, and HTTP-01 needs a publicly resolvable host, which a laptop does not have.
* **Multi-node RWX behaviour** — the `ReadWriteMany` switch in §4 was reasoned about, not
  observed, because this cluster has one node and one RWO provisioner.
* **Arbitrary-uid execution** (OpenShift-style) — the image is `g=u`-chmodded and group-0
  for that reason, but no `securityContext: {runAsUser: 10001}` pod was started here.
* **True multi-tenancy.** Each tenant is a site with its own database; this stack runs one
  site. The Host-header routing seam is in `ingress.yaml` and the ConfigMap, and the queue
  naming in §12 shows one worker set *can* serve several sites, but per-tenant rate limiting,
  a second site, and shared-schema row isolation are not implemented.
* **Resource sizing.** The requests in §6 are chosen so the stack schedules on a 7.5 GB
  laptop; `kubectl top pods` during the load test above is how you would start.

---

## 14. kubectl debugging cheat sheet

The four states that cover most of real life, with the commands in the order I would type
them. All of them except "Pending" happened while building this part, which is why the
notes are specific.

### CrashLoopBackOff — the process exits, over and over

```bash
kubectl -n tenantbooks get pods                       # RESTARTS climbing, 1/2 READY
kubectl -n tenantbooks logs <pod> --previous          # the crash log, not the new attempt
kubectl -n tenantbooks describe pod <pod> | tail -20  # Last State / Exit Code / Reason
kubectl -n tenantbooks get events --sort-by=.lastTimestamp | tail -20
```

Exit codes: `1` application error (read `--previous`), `137` SIGKILL → OOM, see below,
`126`/`127` command or binary not found (this is what a wrong `command:` looks like),
`143` SIGTERM = a clean shutdown that the controller read as a crash (usually PID 1 is a
shell that did not forward the signal — our image uses `exec gunicorn` to avoid it).
Frappe-specific loop causes in order of likelihood: empty `sites` volume so
`apps.txt does not exist` (§4), `site_config.json` pointing at an unreachable DB, and a
mismatch between `SITE_NAME` and the site directory (`Site "tenantbooks.localhost" does
not exist`).

```bash
kubectl -n tenantbooks exec -it <pod> -- bash           # look around a live pod
kubectl -n tenantbooks exec -it <pod> -- bench --site tenantbooks.localhost show-config
kubectl -n tenantbooks debug <pod> --image=busybox:1.36 --target=<container> \
  --share-processes --copy-to=dbg                        # inspect a pod that keeps dying
```

### ImagePullBackOff / ErrImagePull — the cluster cannot see the image

```bash
kubectl -n tenantbooks describe pod <pod> | grep -A5 Failed
# "pulling user image ... not found" -> typo or unqualified name resolved to docker.io
sudo k3s ctr images ls | grep tenantbooks                # is it in this runtime at all?
kubectl -n tenantbooks get pod <pod> -o jsonpath='{.spec.containers[*].image}{"\n"}'
kubectl -n tenantbooks get events --field-selector reason=Pulling
```

Three different causes, one symptom: the image was never imported (§11A), the reference is
unqualified so containerd prepends `docker.io/library/` (`tenantbooks:v15.103.0` is *not*
`ghcr.io/.../tenantbooks`), or the registry needs credentials (`unauthorized` in the
describe output → `imagePullSecrets`, §11B). Note that `docker images` lying to you is the
single most confusing part: Docker and k3s are two stores on this machine.

### Pending — the scheduler refused the pod

```bash
kubectl -n tenantbooks describe pod <pod> | grep -A10 Events
# 0/1 nodes are available: 1 Insufficient memory   -> requests do not fit
# pod has unbound immediate PersistentVolumeClaims -> storage, see below
kubectl describe node fedora | grep -A6 'Allocated resources'   # what is already promised
```

With `WaitForFirstConsumer` the PVC message is *not* an error, it is the storage class
waiting for the pod's node decision — the real reason shows up in the same Events block
(`node(s) had volume node affinity conflict`, `multi-attach error`, or a provisioner
failure). Also check what you did not expect: `taints` on the node, and
`resourceNames: tenantbooks` quota if a namespace quota exists.

### OOMKilled — the limit was reached

```bash
kubectl -n tenantbooks get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState.terminated}'
# {"reason":"OOMKilled","exitCode":137,...}
kubectl -n tenantbooks top pod --containers            # live memory vs the limit
kubectl top nodes
```

In this stack the two ways to hit it are `worker-long` (a PDF whose HTML pulls a large
report through wkhtmltopdf, charged to the worker's cgroup) and `backend` if
`GUNICORN_WORKERS` is raised without raising the memory limit: N workers × warm Python is
the number the limit must exceed. Raising the limit is the fix only if the node has the
memory; otherwise lower `GUNICORN_WORKERS` or split the load with the HPA, which is what
§7 sizes for.

### Everything else

```bash
kubectl -n tenantbooks get all,ing,netpol,pvc,cm,secret -o wide   # whole-stack snapshot
kubectl -n tenantbooks get events -A --sort-by=.lastTimestamp | tail -30
kubectl -n tenantbooks logs -l tier=frappe --tail=30 --prefix     # all frappe roles at once
kubectl -n tenantbooks port-forward svc/frontend 8080:8080        # reach it without ingress
kubectl -n tenantbooks exec deploy/frontend -- wget -qO- http://backend:8000/api/method/ping \
  --header='Host: tenantbooks.localhost'                          # service->pod, bypassing nginx
kubectl -n tenantbooks auth can-i --list --as=system:serviceaccount:tenantbooks:default
```

---

## 15. Gotchas found while building this part

1. **A Docker named volume is pre-populated from the image; a Kubernetes PVC never is.**
   The compose stack got `apps.txt` and `sites/assets` for free and the cluster did not, so
   the whole web tier would have died on a fresh volume. Fixed with the `seed-sites-volume`
   initContainer (`jobs.yaml`), which is the only non-obvious line in the bootstrap.
2. **A NetworkPolicy egress allow with no matching ingress allow produces a silent
   timeout.** My first version permitted `frappe → redis` and `frappe → db` only; both
   test pods then failed identically and the data services looked broken. `default-deny`
   is bidirectional, so every allow-list needs a rule on the *target* too — the three
   `*-ingress` documents exist because of that bug, not for symmetry.
3. **`mysqladmin` does not exist in `mariadb:11.4`.** The official image ships the server
   (`/usr/sbin/mariadbd`) and the renamed clients (`mariadb`, `mariadb-admin`), so the exec
   probe died with `bash: line 1: mysqladmin: command not found` (verified in the pod:
   `command -v mysqladmin` → nothing; `/usr/local/bin` holds only
   `docker-entrypoint.sh`, `gosu`, `healthcheck.sh`). The image's own `healthcheck.sh` is
   the right probe. Our *app* image does have `/usr/bin/mysqladmin`, so the `wait-for-db`
   initContainer is fine — a difference between two images that both claim MariaDB.
4. **`db` did not resolve while `db-0` was NotReady**, and the error is
   `nc: bad address 'db'` rather than "connection refused". A headless Service publishes A
   records for its *Ready* endpoints only, so a broken startup probe looks like a DNS
   problem to every client. Fixed probes; the symptom is documented in §5 because it is
   genuinely misleading.
5. **A StatefulSet rollout deadlocks when the new template's probe fails** — the
   controller will not replace a pod that never became Ready, so the pod runs the *old*
   spec while `rollout status` prints `Waiting for 1 pods to be ready` forever
   (§10, `delete pod db-0` to unstick).
6. **`--dry-run=server` cannot validate a stack that creates its own Namespace** (§2).
7. **Probe header values are not templated**: `httpHeaders[].value: $(SITE_NAME)` is sent
   literally, so the site name appears twice (ConfigMap and `backend.yaml`) and must be
   kept in step.
8. **`fsGroup` is not a cosmetic field here**: without `fsGroup: 0`, uid 1000 cannot write
   the freshly provisioned `sites` volume, and the failure appears as `PermissionError`
   from inside `bench new-site` rather than as anything about storage.
9. **`volumes:` without `volumeMounts:` is valid YAML that silently does nothing.** The
   scheduler and both workers declared the pod-level volumes but never mounted them, so
   they read the image's empty `sites/` — `cat sites/common_site_config.json` → `{}` — and
   every job died with `redis.exceptions.ConnectionError: Error 111 connecting to
   127.0.0.1:11311`. The pod stayed `1/1 Running` while doing nothing. To see it: patch the
   Deployment to `command: ["sleep","infinity"]`, exec in, and look at the filesystem.
10. **`cp -a` fails on a Kubernetes volume as non-root.** Preserving mtimes on a volume whose
    root directory belongs to `root` gives `cp: preserving times for '/seed/': Operation not
    permitted` — *after* most files were copied, so the initContainer exited 1 having done
    90 % of the work. The seed uses `cp -R` plus `sync` and then asserts a marker file
    exists, so a partial copy cannot masquerade as completion.
11. **Deleting the PVC does not delete the site.** Frappe keeps state in two places: the
    `sites/<name>/` directory *and* a real MariaDB database plus a generated DB user. After a
    volume wipe, `bench new-site` failed with `Database _f471a2d288e811b8 already exists`, and
    fixing that produced `(1045, "Access denied for user '_f471a2d288e811b8'@'10.42.0.33'")`.
    A true reset is `DROP DATABASE` + `DROP USER` for the orphan as well.
12. **Jobs are immutable and there is no DAG.** Applying `migrate` in the same pass as
    `create-site` made it fail instantly (the site did not exist yet); re-running a finished
    Job needs `kubectl delete job` first — `apply` refuses to change a template. The bootstrap
    is therefore *two* sequential applies, and the `wait-for-db` initContainer is what keeps
    each one honest on its own.
13. **A kustomize `patches:` entry with a `target:` selector still needs TypeMeta** in this
    kubectl (1.36): a bare `spec:` fragment fails with `unable to parse SM or JSON patch`. Add
    `apiVersion: apps/v1`, `kind: Deployment`, and a throwaway `metadata.name` — the name is
    ignored when `target:` is present.
14. **`bench console` is interactive IPython; piping a script into it executes nothing**
    (`Do you really want to exit ([y]/n)?`). Use `bench execute`, which *does* commit — it ends
    with `if frappe.db: frappe.db.commit()` — so Part 1's "no auto-commit" warning applies to
    the console only. Bare `env/bin/python` is not a third option: its log handlers resolve to
    `/home/frappe/logs`, a directory the image does not have
    (`FileNotFoundError: /home/frappe/logs/database.log`), and exporting `FRAPPE_BENCH_ROOT`
    — which `get_bench_path()` does honour — did not change it. Not chased further: use
    `bench execute`.
15. **`kubectl wait --for=condition=Ready pod --all` never returns** in a namespace with
    completed Jobs: a finished Job pod is `Completed`, not `Ready`, so the command blocks
    until its own timeout. Wait per workload (`rollout status deploy/…`) or on the
    `job` condition instead.
16. **Do not judge the scheduler from the job table's `name` column.** In v15 a
    `Scheduled Job Type` row is named after the *short* method (`tasks.retry_pending_webhooks`),
    not the dotted path, so `where name like '%tenantbooks%'` returns nothing and looks like a
    missing registration. Filter on `method`. The `create_type` column from older Frappe is
    gone too, and `last_execution` is stored in the site's time zone while the pod logs print
    UTC — six hours apart here — so line the clocks up before concluding an event never ran.
17. **`kubectl apply` prints `configured` for a StatefulSet even when nothing changed.** The
    server defaults fields the file omits (`podManagementPolicy`, `revisionHistoryLimit`,
    `restartPolicy`, `dnsPolicy`, `persistentVolumeClaimRetentionPolicy`, plus `apiVersion` and
    `kind` inside each `volumeClaimTemplates` entry), so apply always finds a patch to send.
    `db-0` kept `AGE 104m` through three such applies — which is how you tell cosmetic churn
    from a real rollout: read the pod's age, not the word `configured`.
18. **A `kubectl patch` on a ConfigMap survives exactly until the next `kubectl apply`.** The
    retry test patched `WEBHOOK_URL` to the failing endpoint, then applied the overlay to
    re-run the bootstrap Job — the apply rewrote the ConfigMap from the file and the injected
    fault vanished with no error anywhere. Under GitOps the file *is* the state, so an
    experiment must re-create only the object it needs (`kubectl create -f` a single Job
    extracted from the rendered overlay), never `apply -k` the whole stack.
