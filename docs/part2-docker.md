# Part 2 - Docker: one image, many processes

What this part adds:

```
apps.json                       # the app manifest bench reads at image build time
.dockerignore                   # what never enters the build context (and so never enters a layer)
docker/custom/Containerfile     # two-stage build: builder (toolchain) -> backend (runtime)
docker/custom/start.sh          # gunicorn launcher, everything read from the environment
docker/compose.prod.yaml        # same topology as the dev stack, but from the image
docker/prod.env.example         # the only file with secrets in it (copy + edit)
mock-receiver/Dockerfile        # the peer system as its own pinned, non-root image
```

Everything below was run on Fedora 44, Docker 29, BuildKit/buildx 0.36.

## 1. The two stages, and why there are two

| | stage `builder` | stage `backend` |
| --- | --- | --- |
| base image | `frappe/build:v15.103.0` | `frappe/base:v15.103.0` |
| has | git, gcc, python 3.11.6, node 20.19.2 + yarn, bench | nginx, wkhtmltopdf 0.12.6.1, node 20.19.2, mariadb client |
| does | `bench init` -> clones frappe at `v15.103.0`, installs the apps from `apps.json`, builds the JS/CSS bundles | receives the finished `frappe-bench` directory and nothing else |

A single-stage image would carry a C compiler, `build-essential` and the whole
Node toolchain into production. If a container is ever compromised, a compiler
is the difference between "read the files this uid can read" and "build a
privilege-escalation exploit in place". Multi-stage is not about image size;
size is a side effect.

The final image contains **one directory**: `/home/frappe/frappe-bench`, which
holds `apps/` (frappe + tenantbooks, read-only by convention), `config/`,
`sites/` (mounted over at runtime) and `env/` (the Python virtualenv).

## 2. apps.json - how the app gets in

`bench init --apps_path=<file>` is the mechanism frappe_docker itself uses
(`docs/02-setup/02-build-setup.md`). The format is a JSON **list** - read in the
bench source, `bench/app.py:1071`:

```python
def install_apps_from_path(path, bench_path="."):
	apps = get_apps_json(path)
	for app in apps:
		get_app(app["url"], branch=app.get("branch"), bench_path=bench_path, skip_assets=True)
```

So `apps.json` here is:

```json
[
  { "url": "/opt/src/tenantbooks" }
]
```

Two things worth being able to explain:

- **Why a path and not a GitHub URL.** `/opt/src/tenantbooks` is the build
  context, mounted read-only into the builder stage. The image is built from the
  very checkout you are standing in, so the app version in the image *is* the
  checkout - no tag to keep in sync, no network fetch of your own repository, no
  credentials to give the builder. `git clone` of a local path is the same
  command either way, so switching to a URL for a multi-repo setup is a one-line
  change.
- **Why no `branch`.** With no branch, bench clones the checkout's HEAD, which is
  exactly what you asked to build. Pinning a branch name here would let a stale
  value silently win over the working tree.

`apps.json` reaches the build as a **BuildKit secret**, not a build arg:

```bash
docker buildx build --secret id=apps_json,src=apps.json -f docker/custom/Containerfile .
```

`--build-arg` values are recorded permanently in `docker image history` and in the
image config, so anyone who can pull the image can read them. A secret mount only
exists for the duration of one `RUN`. Today the file contains a path, so nothing
would leak - the mechanism is there so that adding
`https://<token>@github.com/...` for a private app is not a security decision
later. Verify:

```bash
docker history tenantbooks:v15.103.0 --no-trunc | grep -c '"url"'   # 0
docker run --rm --entrypoint ls tenantbooks:v15.103.0 /opt/frappe/apps.json
# ls: cannot access ... No such file or directory  (the mount never became a layer)
```

## 3. The one gotcha that broke the first build

```
fatal: detected dubious ownership in repository at '/opt/src/tenantbooks/.git'
```

BuildKit materialises the build context as **root-owned** files, while the RUN
executes as `frappe` (uid 1000). Git refuses to touch a repository owned by
another uid, so `git clone` of the mount fails. The fix is one line, and the
path form matters:

```dockerfile
git config --global --add safe.directory /opt/src/tenantbooks/.git
```

`/opt/src/tenantbooks` (the worktree) is **not** accepted for this case - tested
both forms in a container as uid 2000 against this repository; only the `.git`
path let the clone through. Same trap appears in CI: the runner's checkout has
to be declared safe, or the repo has to be cloned over `https://`.

Related CI trap, recorded now so Part 4 does not rediscover it:
`actions/checkout` defaults to `fetch-depth: 1`, which produces a *shallow*
repository, and `git clone` refuses to clone from a shallow repository. A CI
build that bind-mounts the checkout must use `fetch-depth: 0`.

## 4. Non-root, and group 0

Both stages end with `USER frappe`, and the runtime copy is
`COPY --from=builder --chown=frappe:0`.

- `frappe` is uid 1000 in `frappe/build` and `frappe/base` (checked with
  `docker run --user 1000 ... id`). The build itself also runs as that user: a
  root RUN would produce root-owned files that the runtime user cannot write,
  which is exactly the failure mode that makes people `chmod 777` things.
- `--chown=frappe:0` + `chmod -R g=u` is the OpenShift/Kubernetes convention for
  "arbitrary uid": kubelet may start the container as uid 17423 with gid 0, and
  group-writable files owned by gid 0 keep `/home/frappe/frappe-bench/logs`
  writable. Part 3 sets `runAsUser` + `fsGroup` to match.
- `frappe/base` itself defaults to **root** with `CMD python3`; both are
  overridden here. Forgetting `USER frappe` after a `FROM` that has no user set
  is the most common way a "hardened" image ships as root anyway.

## 5. Pinned tags

| image | tag | why this tag |
| --- | --- | --- |
| `frappe/build`, `frappe/base` | `v15.103.0` | the frappe version inside `frappe/erpnext:v15.101.2` used in Part 1 (`bench version` said `frappe 15.103.0`), so dev and prod produce the same schema |
| `mariadb` | `11.4.13` | the patch behind the `11.4` tag Part 1 ran (`org.opencontainers.image.version` label) |
| `redis` | `6.2.24-alpine` | same reason (`REDIS_VERSION` env of the pulled image) |
| `python` (mock receiver) | `3.12.11-slim-bookworm` | the mock receiver only needs stdlib |

Two tags are *not* used: `:latest` (an untagged deployment target is undeployable
in a rollback) and `:v15` (a moving tag: `v15` today is not `v15` next month, so
a "reproducible" build would not reproduce).

The image is also labelled with `org.opencontainers.image.base.name`, so
`docker inspect` tells you which frappe version is inside without reading the
Containerfile.

### What an image tag should say

`tenantbooks:v15.103.0` currently encodes the *frappe* version, which is arguable
- it answers "which framework schema does this image write?" but not "which
  commit of my app?". Part 4 tags pushed images with the git SHA
(`ghcr.io/<owner>/tenant-books@sha256:...` and `:...-<sha>`) and keeps the frappe
version in the OCI label instead. Rule of thumb: the tag is what you can type at
a prompt during an incident (`:v15.103.0-2f8ac91`), the digest is what you pin a
Deployment to, the labels are what you ask a running pod afterwards.

`frappe/erpnext:v15.101.2` also shows why "pinned" needs care: that tag is
*erpnext* 15.101.2 and contains *frappe* 15.103.0. Two different version spaces
in one tag - which is exactly how a dev/prod schema drift gets started.

## 6. One image, seven processes

`docker/compose.prod.yaml` starts the same topology as the dev stack, all from
one image with different commands:

| service | command | role |
| --- | --- | --- |
| `backend` | image default: `/usr/local/bin/start.sh` -> gunicorn | HTTP/REST |
| `frontend` | `nginx-entrypoint.sh` | static files, reverse proxy, websocket upgrade |
| `websocket` | `node apps/frappe/socketio.js` | realtime (notifications, build progress) |
| `scheduler` | `bench schedule` | fires `scheduler_events` from `hooks.py` |
| `worker-short` | `bench worker --queue short,default` | fast jobs |
| `worker-long` | `bench worker --queue long` | PDF render + webhook delivery |
| `create-site` / `migrate` | `bash -c` one-shot | schema + site bootstrap, then exit 0 |

The difference from `docker/compose.local.yaml`, in one line each:

| dev stack | prod-shaped stack |
| --- | --- |
| `image: frappe/erpnext:v15.101.2` | `image: tenantbooks:v15.103.0` (built here) |
| app bind-mounted: `- ../:/home/frappe/frappe-bench/apps/tenantbooks` | no source mount at all |
| `PYTHONPATH=/home/frappe/frappe-bench/apps/tenantbooks` | absent - `pip install -e apps/tenantbooks` happened at build time |
| secrets hardcoded (`admin`) | `${DB_ROOT_PASSWORD:?...}` - compose refuses to start without an env file |
| one `setup` job (config + new-site + migrate) | `create-site` then `migrate`, matching the two Kubernetes Jobs of Part 3 |
| `--progress=plain`, restart on failure | `restart: unless-stopped`, healthchecks, memory limits, `no-new-privileges` |

`PYTHONPATH: None` in the rendered config is the proof that the image is
self-contained:

```bash
docker compose --env-file docker/prod.env -f docker/compose.prod.yaml config \
  | grep -A3 'PYTHONPATH'   # no match
```

## 7. Gunicorn settings live in the environment, not the image

`frappe/erpnext:v15` bakes the gunicorn argv into `CMD`
(`--workers=2 --threads=4 --timeout=120 --preload`), which means changing the
worker count requires rebuilding an image. `docker/custom/start.sh` reads them
from env instead - the same values are defaults, so behaviour is unchanged:

```bash
GUNICORN_WORKERS=2 GUNICORN_THREADS=4 GUNICORN_TIMEOUT=120 \
GUNICORN_MAX_REQUESTS=5000 GUNICORN_MAX_REQUEST_JITTER=50
```

`exec` in that script is not decoration: without it bash stays PID 1, `docker
stop` / SIGTERM never reaches gunicorn, and the worker is killed at the end of
the grace period mid-request. Part 7 explains the numbers; Part 3 puts them on a
Deployment.

## 8. What is deliberately *not* in the image

- `sites/` content: site directories, `site_config.json`, `common_site_config.json`
  and uploads are environment data. They come from a named volume here, a PVC in
  Part 3. The image ships only an empty `sites/common_site_config.json` (`{}`) so
  that `bench` does not fail before `create-site` runs.
- `logs/`: same reason.
- `.git` of any app: `find apps -mindepth 1 -path '*/.git' -exec rm -rf {} +`.
  Build history is not runtime content.
- `apps.json` itself (BuildKit secret) and `docker/prod.env` (`.dockerignore`).

## 9. Verification - what was actually run

Cold build on this laptop (Docker 29, buildx 0.36, `frappe/build` +
`frappe/base` pulled first, ~7 min: 5:48 for `bench init` including the asset
bundle, 0:56 for the image export). It is CPU- and RAM-hungry: the same step took
an hour while the machine was at load 12 with 9 GB of swap in use and the Part 1
stack running, so stop other stacks first.

```bash
docker buildx build --secret id=apps_json,src=apps.json \
    --tag tenantbooks:v15.103.0 --file docker/custom/Containerfile .
# BUILD_EXIT=0, no Dockerfile warnings
```

### The image itself

```bash
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'tenantbooks|frappe'
```

| image | size | note |
| --- | --- | --- |
| `frappe/base:v15.103.0` | 1.92 GB | runtime deps only, no bench |
| `tenantbooks:v15.103.0` | 3.07 GB | + frappe, + tenantbooks, + built assets |
| `frappe/erpnext:v15.101.2` | 3.21 GB | what the dev stack uses (frappe + **erpnext**) |

So the app image is *smaller than ERPNext* while carrying our own app: ERPNext
pulls in ~80 DocType-heavy modules we do not need.

Inside the final image (`docker run --rm --entrypoint bash tenantbooks:v15.103.0 -c ...`):

```
user=frappe uid=1000 gid=1000
apps: frappe tenantbooks
bench version        -> frappe 15.103.0 / tenantbooks 0.0.1
sites/apps.txt       -> frappe tenantbooks
apps/**/.git dirs    -> 0
/opt/frappe/apps.json-> ABSENT           (the secret mount is not a layer)
sites/common_site_config.json -> {}      (placeholder, real one comes from create-site)
sites/assets/        -> assets.json, css, js, ... (built at image build time)
gcc -> none          make -> none        (multi-stage: no toolchain in runtime)
env/bin/python -c "import tenantbooks" -> apps/tenantbooks/tenantbooks/__init__.py
```

Proving the secret never landed in the image:

```bash
docker history tenantbooks:v15.103.0 --no-trunc | grep -c '"url"'   # 0
docker inspect tenantbooks:v15.103.0 --format '{{json .Config.Env}}' # no apps_json entry
```

(What *does* appear in history is the mount declaration
`--mount=type=secret,id=apps_json` - the name, never the contents.)

### The stack

```bash
docker compose -f docker/compose.local.yaml stop          # free the RAM
cp docker/prod.env.example docker/prod.env                 # edit 3 secrets
docker compose --env-file docker/prod.env -f docker/compose.prod.yaml up -d --build
docker compose --env-file docker/prod.env -f docker/compose.prod.yaml ps
```

| Check | Result |
| --- | --- |
| one-shot jobs | `create-site` Exited (0), `migrate` Exited (0), then the 7 long-running services start |
| DocTypes installed from the image | `Updating DocTypes for tenantbooks: [100%]` in the create-site log |
| containers have no source bind mount | `docker inspect` Mounts: `tenantbooks-prod_sites`, `tenantbooks-prod_logs` only |
| no PYTHONPATH crutch | `docker exec ... echo $PYTHONPATH` -> `<unset>`, yet the app imports |
| app server | `curl :8081/api/method/ping` -> `{"message":"pong"} [200]` |
| assets from the image layer | `/assets/frappe/dist/css/website.bundle.U6E7K5RP.css` -> 200, 447 KB |
| REST order | `create_order` -> `{"name":"TB-ORD-2026-0001","net_total":50.0,"invoice":"TB-INV-2026-00001"}` |
| long queue jobs | worker-long: `Job OK (...tenantbooks-pdf-TB-INV-2026-00001)` and `...-webhook-TB-INV-2026-00001` |
| webhook peer | mock-receiver (built from its own Dockerfile) logged `ACCEPTED invoice.submitted invoice=TB-INV-2026-00001` |
| delivery log row | `status=Sent, attempts=1, http_code=200` |
| PDF | `/files/TB-INV-2026-00001.pdf` -> 200, `application/pdf`, 16,917 bytes |
| prod is test-locked | `bench run-tests` refuses: "You can enable tests by ... set-config allow_tests true" |
| probes pass | `ps` -> backend `Up (healthy)`, frontend `Up (healthy)`, db `healthy`; `docker inspect` -> `restarts=0 oomkilled=false` on all ten containers (the first version of the probe did not, see gotcha 6) |

Tear down (the DB password is in the volume, so delete it too when done):

```bash
docker compose --env-file docker/prod.env -f docker/compose.prod.yaml down -v
docker compose -f docker/compose.local.yaml start        # dev stack again
```

## 10. Gotchas found while building this

1. **`safe.directory` needs the `.git` path.** `git clone` of the read-only
   context mount fails as uid 1000 because BuildKit hands the context over as
   root-owned. `git config --global --add safe.directory /opt/src/tenantbooks`
   does **not** fix it; `/opt/src/tenantbooks/.git` does. Tested both in a
   container under a foreign uid.
2. **The image is built from the commit, not from your working tree.** The mount
   is cloned with `git clone`, so uncommitted edits are silently ignored - while
   still busting the layer cache, so you pay the full rebuild and get the old
   code. Verified: `grep -c '^ARG FRAPPE_REPO'` = 2 in the worktree, 1 in
   `git show 3ac242b:...`, 1 inside the image. **Commit before you build.**
   (In CI this is invisible: a pipeline builds a SHA that is by definition
   committed.)
3. **`ARG` does not cross a `FROM`.** The stage-2 `LABEL` rendered as
   `org.opencontainers.image.base.name="/base:v15.103.0"` until `ARG FRAPPE_REPO`
   was redeclared inside the stage. BuildKit's `UndefinedVar` warning is the only
   clue; check `docker inspect` labels after adding a stage.
4. **`git clone` warns `--depth is ignored in local clones`** because bench asks
   for a shallow clone and the source is a local path. Harmless - it copies the
   full history into the *builder* stage, and the `.git` directories are deleted
   before the runtime stage.
5. **`yarn` peer-dependency warnings** (`less@^4.x`, `vue-template-compiler`)
   come from frappe's own `package.json`. They are warnings; the bundle builds.
   Do not "fix" them in an app Containerfile - that is upstream's business.
6. **A healthcheck against Frappe must send the site Host.** The first probe I
   wrote was `python -c "urllib.request.urlopen('http://127.0.0.1:8000/api/method/ping')"`
   and the container stayed `unhealthy` while every request from outside worked:
   gunicorn answers **404** when the Host does not match a site directory, because
   Frappe resolves the site per request. With `-H 'Host: tenantbooks.localhost'`
   the same URL returns 200. The final probes therefore are
   `curl -sf -H 'Host: $SITE_NAME' http://127.0.0.1:8000/api/method/ping` for the
   app box (needs the site name) and a plain `curl -sf
   http://127.0.0.1:8080/api/method/ping` for nginx (it injects the site header
   itself via `FRAPPE_SITE_NAME_HEADER`). This is the Part 3 problem in advance:
   a Kubernetes `httpGet` probe hits the *pod IP*, so it needs
   `httpHeaders: [{name: Host, value: <site>}]` or the Deployment never becomes
   ready.
7. **A 3 GB image takes ~1 minute just to export.** If a build appears stuck at
   `exporting layers`, it is copying the bench directory, not hung.

## 11. Not verified

- **Pushing the image to a registry and pulling it on another node.** The build
  and run happened on one laptop. `docker push`/`ghcr.io` login is Part 4.
- **`linux/amd64` on Apple Silicon / multi-arch.** Everything here is a native
  `linux/amd64` build on x86_64; `--platform` cross-builds of frappe (which
  compiles native wheels) were not tested and are known to be slow.
- **Image user namespaces / `--user` remapping.** The `gid 0` group-writable
  layout is in place, but running under a *different* uid than 1000 was only
  reasoned about from the file mode, not exercised - Part 3 sets
  `runAsUser`/`fsGroup` and will show it.
- **Build cache behaviour across many iterations** was not measured. Any change
  inside the context invalidates the `--mount=type=bind,source=.` layer, and
  because `bench init` (frappe clone + pip + yarn + asset build) and the app
  install live in one `RUN`, a one-line app change costs the full ~6 minutes.
  The fix is to split `bench init` (frappe only, cached by `FRAPPE_VERSION`) from
  the app install - but that means re-implementing, in shell, the five lines
  bench already provides as `install_apps_from_path`, so for one app I kept the
  single readable `RUN`.

