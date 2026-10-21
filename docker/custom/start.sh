#!/bin/bash
# Web process launcher for the TenantBooks image.
#
# The alternative (baking the gunicorn argv into the image CMD, which is what
# frappe/erpnext:v15 does) hides every tunable.  Reading them from the
# environment means one image serves a 1-cpu staging pod and a 4-cpu
# production pod without a rebuild.
#
# `exec` matters: without it bash would be PID 1 and SIGTERM from
# `kubectl delete pod` / `docker stop` would never reach gunicorn, so the
# worker would be SIGKILLed at the end of the grace period instead of
# finishing the requests it already accepted.
set -euo pipefail

cd /home/frappe/frappe-bench

exec ./env/bin/gunicorn \
    --chdir=/home/frappe/frappe-bench/sites \
    --bind="${GUNICORN_BIND}" \
    --workers="${GUNICORN_WORKERS}" \
    --threads="${GUNICORN_THREADS}" \
    --worker-class=gthread \
    --worker-tmp-dir=/dev/shm \
    --timeout="${GUNICORN_TIMEOUT}" \
    --max-requests="${GUNICORN_MAX_REQUESTS}" \
    --max-requests-jitter="${GUNICORN_MAX_REQUEST_JITTER}" \
    --preload \
    frappe.app:application
