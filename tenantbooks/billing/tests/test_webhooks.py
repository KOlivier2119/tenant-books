"""Unit tests for the webhook sender.

No network, no queue, no scheduler: `requests.post` is replaced with a Mock so
we can drive the success / retry / permanent-failure paths on demand.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tenantbooks.billing.tests import helpers
from tenantbooks.billing.webhooks import (
    MAX_ATTEMPTS,
    backoff_seconds,
    canonical_json,
    deliver_webhook,
    sign,
    verify_signature,
)


class TestSignature(unittest.TestCase):
    """Pure functions - no database involved."""

    def test_signature_round_trips(self):
        body = canonical_json({"b": 2, "a": 1})
        header = sign("secret", body)
        self.assertTrue(header.startswith("sha256="))
        self.assertTrue(verify_signature("secret", body, header))

    def test_wrong_secret_fails(self):
        body = canonical_json({"a": 1})
        self.assertFalse(verify_signature("other", body, sign("secret", body)))

    def test_tampered_body_fails(self):
        body = canonical_json({"total": 100})
        header = sign("secret", body)
        self.assertFalse(verify_signature("secret", body.replace(b"100", b"999"), header))

    def test_canonical_json_is_stable(self):
        # Key order must not change the bytes, or the receiver cannot verify.
        self.assertEqual(canonical_json({"a": 1, "b": 2}), canonical_json({"b": 2, "a": 1}))
        self.assertEqual(canonical_json({"a": 1}), b'{"a":1}')

    def test_backoff_grows_and_is_capped(self):
        self.assertGreater(backoff_seconds(3), backoff_seconds(2))
        self.assertLessEqual(backoff_seconds(20), 3600 * 1.3)


class TestDeliverWebhook(FrappeTestCase):
    """Needs documents, so it runs on the test database."""

    def setUp(self):
        self.customer = helpers.make_customer()
        self.product = helpers.make_product(stock_qty=5)
        order = helpers.draft_order(self.customer, self.product, qty=1)
        order.submit()
        self.invoice_name = frappe.db.get_value("Invoice", {"order": order.name}, "name")
        self.url = "http://mock-receiver:8071/hook"
        self.secret = "unit-test-secret"

    def _patch_conf(self):
        return patch(
            "tenantbooks.billing.webhooks.get_webhook_config",
            return_value=(self.url, self.secret),
        )

    def _response(self, status_code: int, text: str = "ok") -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        return resp

    def test_success_marks_log_sent(self):
        with self._patch_conf(), patch("requests.post", return_value=self._response(200)) as post:
            result = deliver_webhook(self.invoice_name)

        self.assertEqual(result["status"], "Sent")
        # The bytes that were signed are exactly the bytes that were sent.
        args, kwargs = post.call_args
        body = kwargs["data"]
        header = kwargs["headers"]["X-TenantBooks-Signature"]
        self.assertTrue(verify_signature(self.secret, body, header))
        self.assertEqual(json.loads(body)["invoice"], self.invoice_name)

        log = frappe.get_all("Webhook Delivery Log", {"invoice": self.invoice_name})
        row = frappe.get_doc("Webhook Delivery Log", log[0].name)
        self.assertEqual(row.status, "Sent")
        self.assertEqual(row.attempts, 1)

    def test_server_error_marks_retrying_with_backoff(self):
        with self._patch_conf(), patch("requests.post", return_value=self._response(500, "boom")):
            result = deliver_webhook(self.invoice_name)

        self.assertEqual(result["status"], "Retrying")
        row = frappe.get_doc(
            "Webhook Delivery Log",
            frappe.get_all("Webhook Delivery Log", {"invoice": self.invoice_name})[0].name,
        )
        self.assertEqual(row.status, "Retrying")
        self.assertEqual(row.http_code, 500)
        self.assertIsNotNone(row.next_retry_at)
        self.assertIn("HTTP 500", row.response_body)

    def test_connection_error_is_logged_and_retried(self):
        import requests

        with self._patch_conf(), patch("requests.post", side_effect=requests.ConnectionError("refused")):
            result = deliver_webhook(self.invoice_name)

        self.assertEqual(result["status"], "Retrying")
        self.assertIn("refused", result["error"])
        row = frappe.get_doc(
            "Webhook Delivery Log",
            frappe.get_all("Webhook Delivery Log", {"invoice": self.invoice_name})[0].name,
        )
        self.assertIn("ConnectionError", row.response_body)

    def test_gives_up_after_max_attempts(self):
        row = None
        with self._patch_conf(), patch("requests.post", return_value=self._response(503)):
            for _ in range(MAX_ATTEMPTS):
                deliver_webhook(self.invoice_name)
            names = frappe.get_all("Webhook Delivery Log", {"invoice": self.invoice_name}, pluck="name")
            row = frappe.get_doc("Webhook Delivery Log", names[0])

        self.assertEqual(row.status, "Failed")
        self.assertEqual(row.attempts, MAX_ATTEMPTS)
        # one audit line per attempt
        self.assertEqual(len([ln for ln in row.response_body.splitlines() if ln.strip()]), MAX_ATTEMPTS)

    def test_missing_config_fails_without_network(self):
        with (
            patch("tenantbooks.billing.webhooks.get_webhook_config", return_value=(None, None)),
            patch("requests.post") as post,
        ):
            result = deliver_webhook(self.invoice_name)

        post.assert_not_called()
        self.assertEqual(result["status"], "Failed")
