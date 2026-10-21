# Part 3 — Kubernetes

Everything needed to run TenantBooks on a cluster: Deployments for the seven Frappe
roles, a MariaDB StatefulSet, two Redis, shared storage, bootstrap Jobs, autoscaling,
network policy and an Ingress. One `kubectl apply -k` and the whole stack exists.

Verified on this machine's single-node **k3s 1.36.5** cluster (namespace `tenantbooks`).
The data tier ran for real; the app tier is blocked on one root-only command (the
cluster's container runtime cannot see images built by Docker) — see
[Loading the image](#loading-the-image-into-the-cluster).

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
  `created (server dry run)` for all 37 objects — i.e. the real API server, with its real
  admission plugins and CRDs, accepted every document.
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
Deployment pods:    0 current / 0 desired
  Scale Up:  Stabilization Window: 30 seconds ... Percent 100 / 30s
  Scale Down: Stabilization Window: 300 seconds ... Pods 1 / 120s
```

`0 desired` with no target pods is the correct idle state. If metrics-server were missing
this line reads `<unknown>` and the events say `unable to fetch metrics from resource
metrics API` — the single most common reason an HPA "does nothing".

---

## 8. Network policy — `networkpolicy.yaml`

Default-deny for the namespace (both directions), then ten targeted allows.

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
`frappe-to-frontend`, `frappe-to-webhook-receiver` (+ the commented production shape with
`ipBlock: 0.0.0.0/0 except 10/8, 100.64/10, 172.16/12, 192.168/16` on 443), and matching
ingress sides `db-ingress`, `redis-ingress`, `receiver-ingress`.

Two rules are only obvious if the app has already failed once:

* `frappe-to-frontend` — the long worker renders a PDF with wkhtmltopdf, which fetches
  `/assets/*` from the site URL (`host_name`). Inside a cluster that URL resolves to the
  frontend Service, so a worker that cannot talk to nginx produces a truncated or failed
  PDF. Same failure as Part 1's bug 6, re-derivable from the policy file.
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

Three rollback facts that are not obvious until they bite:

* **`rollout undo` only helps if the previous template points at an image that still
  exists unchanged.** A `:latest` or `:main` tag means "undo" rolls forward into the same
  broken bytes. That is why Part 4 tags images with the git SHA and why
  `k8s/overlay-registry/kustomization.yaml` says so in its comment.
* **Jobs are not rolled back.** A finished Job's `.spec.template` is immutable, so
  `kubectl apply` cannot re-run or change one; delete and create instead. Undoing a
  schema migration is not a `kubectl` operation at all — that is Part 6's
  expand/contract rule (never drop a column in the migration that breaks the previous
  release, so the old code can run against the new schema).
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
because there is no such public repository. Two ways to fix it:

**A. Import the tarball (used here, needs root):**

```bash
docker save -o /tmp/tenantbooks.tar tenantbooks:v15.103.0     # 692 MB, done on this machine
sudo k3s ctr images import /tmp/tenantbooks.tar
sudo k3s ctr images ls | grep tenantbooks    # expect: docker.io/library/tenantbooks:v15.103.0
                         # (not run here: importing needs root)
```

`imagePullPolicy: IfNotPresent` then makes the local copy authoritative and nothing goes
over the network. (Docker Desktop on macOS hides this entirely — its Kubernetes shares
the image store — and `kind` has `kind load docker-image`. k3s/minikube-with-containerd
need the explicit import.)

**B. Push to a registry and use the overlay** (the CI path, Part 4):

```bash
docker tag tenantbooks:v15.103.0 ghcr.io/<you>/tenantbooks:v15.103.0
docker push ghcr.io/<you>/tenantbooks:v15.103.0
kubectl -n tenantbooks create secret docker-registry ghcr --docker-server=ghcr.io \
  --docker-username=<you> --docker-password=<token>   # + imagePullSecrets on each pod
kubectl apply -k k8s/overlay-registry
```

The overlay rewrites the image name inside the base manifests at build time, so all seven
workloads change together and no per-environment copy of a Deployment file is needed. With
a registry reference, `imagePullPolicy: IfNotPresent` is right only when the image is
pinned by **digest**; with a mutable tag you want `Always`, otherwise a node that already
has `:v15.103.0` never re-pulls a re-pushed content under the same name.

---

## 12. What was verified by running it

| Claim | Evidence (all from this machine) |
| --- | --- |
| 37 objects render into a valid manifest set | `kubectl kustomize k8s/base` → 37 documents, exit 0 |
| The API server accepts every one of them | `kubectl apply -k k8s/base --dry-run=server` → 37 × `created (server dry run)`, zero errors (namespace created first, see §2) |
| Namespace prerequisite is real | same command without the Namespace → one `Error from server (NotFound)` per object (34 at the time of measurement) |
| MariaDB comes up Ready with exec probes | `db-0 1/1 Running`; `healthcheck.sh --connect --innodb_initialized` returns 0 inside the container |
| Redis both instances Ready | `redis-cache-… 1/1`, `redis-queue-0 1/1` |
| PVCs provision on first consumer | events `WaitForFirstConsumer` → 4 PVs bound, `[ReadWriteOnce]`, `kubectl get pv` |
| Non-root can write the shared volume | probe pod: `uid=1000 gid=0(root)`, `drwxrwsrwx /sites`, `WRITE_OK` |
| NetworkPolicy actually blocks | §8 table: labelled pod `open` in 0.1–0.2 s, unlabelled pod no response |
| HPA accepted with behavior section | `describe hpa backend`: windows/policies echoed back, `0 current / 0 desired` |
| Headless Service hides a NotReady db | while `db-0 0/1`: `nc: bad address 'db'`; once Ready: `db (10.42.0.18:3306) open` |
| Broken probe deadlocks a STS rollout | `sts` spec had the new probe, pod ran the old one until `delete pod db-0` |
| Metrics-server present (so CPU HPA is meaningful) | `kubectl top nodes` → `fedora 1481m 12% 2373Mi 31%` |

Not run as a pod (blocked on §11 A/B): the seven frappe workloads reaching Ready, the
create-site Job against the in-cluster MariaDB, a PDF delivered through the receiver, and
`rollout undo` exercised end to end. All four are the *next* commands in §13; nothing about
them is cluster-specific except the image load.

## 13. Not verified, and how to verify it

Run these in order after loading the image (§11); each has a concrete expected answer.

```bash
kubectl apply -k k8s/base
kubectl -n tenantbooks wait --for=condition=Complete --timeout=15m job/create-site
kubectl -n tenantbooks logs job/create-site | tail -5     # expect "create-site done"
kubectl -n tenantbooks rollout status deploy/backend      # expect 2/2 available
curl -s http://localhost:8080/api/method/ping -H 'Host: tenantbooks.localhost'   # {"message":"pong"}

# the app tier is genuinely functional, not just Ready:
kubectl -n tenantbooks exec deploy/worker-long -- bench --site tenantbooks.localhost \
  execute tenantbooks.billing.seed.seed                  # {"customers":2,"products":3}
KEY=$(kubectl -n tenantbooks exec deploy/backend -- bench --site tenantbooks.localhost \
  execute frappe.core.doctype.user.user.generate_keys --args '["Administrator"]')
# then POST create_order as in the README and watch:
kubectl -n tenantbooks logs -l app=worker-long --tail=20  # "Job OK (...webhook...)"
kubectl -n tenantbooks logs deploy/mock-receiver --tail=5 # "ACCEPTED invoice.submitted ..."

# probes are what you think they are
kubectl -n tenantbooks describe pod -l app=backend | grep -A2 'Startup\|Ready'

# autoscaling really moves
kubectl -n tenantbooks run load --image=busybox:1.36 --restart=Never -- \
  sh -c 'for i in $(seq 1 300); do wget -q -O /dev/null http://backend:8000/api/method/ping --header="Host: tenantbooks.localhost" || true; done'
kubectl -n tenantbooks get hpa backend -w          # TARGETS -> above 70%, replicas climb

# rollback path
kubectl -n tenantbooks rollout history deploy/backend
kubectl -n tenantbooks rollout undo deploy/backend && kubectl -n tenantbooks rollout status deploy/backend

# NetworkPolicy narrowed correctly for the receiver, once it exists
kubectl -n tenantbooks exec deploy/mock-receiver -- wget -qO- http://localhost:8071/  # {"status":"ok",...}
```

Still unverified beyond those commands, and labelled as such in the README:

* **cert-manager TLS** — the manifests are schema-valid (`kubectl --dry-run=server` on a
  cluster where the CRDs exist would be needed to prove even that); no certificate was
  issued here, and HTTP-01 needs a publicly resolvable host, which a laptop does not have.
* **Multi-node RWX behaviour** — the `ReadWriteMany` switch in §4 was reasoned about, not
  observed, because this cluster has one node and one RWO provisioner.
* **Arbitrary-uid execution** (OpenShift-style) — the image is `g=u`-chmodded and group-0
  for that reason, but no `securityContext: {runAsUser: 10001}` pod was started here.
* **True multi-tenancy.** Each tenant is a site with its own database; this stack runs one
  site. The Host-header routing seam is in `ingress.yaml` and the ConfigMap, but
  per-tenant rate limiting, a second site, and shared-schema row isolation are not.
* **Resource sizing.** The requests in §6 are chosen so the stack schedules on a 7.5 GB
  laptop; none of them were measured against a load profile. `kubectl top pods` during the
  load test above is how you would start.
* **HPA at work**: metrics-server is present and the object parses, but with no backend
  pods the desired count stayed `0`; no scale-up event was observed.

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
