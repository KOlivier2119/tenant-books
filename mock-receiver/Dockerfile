# syntax=docker/dockerfile:1
#
# The webhook receiver is deliberately its own image: it is the *peer* system in
# the demo, not part of the Frappe app.  Keeping it separate is what makes
# "our side" and "their side" obvious when tracing a delivery.
#
# receiver.py is stdlib-only (http.server, hmac, json), so there is nothing to
# compile and no second stage is needed.  Adding an empty builder stage here
# would be cargo cult.
FROM python:3.12.11-slim-bookworm

# Non-root, no capabilities, nothing writable except /tmp.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin receiver

WORKDIR /app
COPY receiver.py .

USER receiver
EXPOSE 8071

CMD ["python", "receiver.py"]
