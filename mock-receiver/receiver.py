#!/usr/bin/env python3
"""Mock webhook receiver - standard library only, no dependencies to install.

Verifies the HMAC-SHA256 signature that tenantbooks puts in
`X-TenantBooks-Signature` and rejects anything that does not match.

    SECRET=demo-secret PORT=8071 python3 receiver.py

Fault injection, to demo retries and backoff:
    curl -X POST 'http://localhost:8071/hook?fail=1' ...   -> always 500
"""

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("WEBHOOK_SECRET", "demo-secret")
PORT = int(os.environ.get("PORT", "8071"))


def expected_signature(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


class Receiver(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802 - name is fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        signature = self.headers.get("X-TenantBooks-Signature", "")

        # hmac.compare_digest: constant-time, so the check cannot be probed
        # byte-by-byte for a partial match.
        if not hmac.compare_digest(expected_signature(body), signature):
            print(f"REJECTED bad signature: {signature[:20]}", flush=True)
            self._respond(401, {"error": "signature mismatch"})
            return

        if "fail=1" in self.path:
            print("REJECTED forced failure (?fail=1)", flush=True)
            self._respond(500, {"error": "injected failure"})
            return

        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "body is not valid JSON"})
            return

        print(f"ACCEPTED {event.get('event')} invoice={event.get('invoice')}", flush=True)
        self._respond(200, {"received": True, "invoice": event.get("invoice")})

    def do_GET(self):  # noqa: N802
        self._respond(200, {"status": "ok", "endpoints": ["POST /hook", "POST /hook?fail=1"]})


if __name__ == "__main__":
    print(f"mock receiver on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Receiver).serve_forever()
