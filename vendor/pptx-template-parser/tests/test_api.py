import json
from io import BytesIO
from urllib.error import HTTPError, URLError
import socket
import ssl
from urllib.request import Request
from unittest import TestCase
from unittest.mock import Mock, patch

from template_parser.api import APIError, request_json, diagnostics


def failure(message="Rate limit exceeded", headers=None, code=429):
    return HTTPError("https://example.com/v1/chat/completions", code, "error", headers or {},
                     BytesIO(json.dumps({"error": {"message": message}}).encode()))


class APITests(TestCase):
    def test_five_retries_with_capped_backoff(self):
        opener = Mock(side_effect=[failure() for _ in range(6)])
        with patch('template_parser.api.time.sleep') as sleep:
            with self.assertRaises(APIError):
                request_json(Request('https://example.com'), {'max_retries': 5}, opener)
        self.assertEqual(opener.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 20, 40, 60, 60])

    def test_network_errors_are_safe_and_not_retried(self):
        cases = [(URLError(socket.gaierror(11002, 'secret-key')), 'dns_error'),
                 (URLError(TimeoutError('secret-key')), 'timeout'),
                 (TimeoutError('secret-key'), 'timeout'),
                 (URLError(ssl.SSLError('secret-key')), 'tls_error'),
                 (URLError('secret-key'), 'network_error')]
        for failure_value, category in cases:
            with self.subTest(category=category):
                opener = Mock(side_effect=failure_value)
                with patch('template_parser.api.time.sleep') as sleep:
                    with self.assertRaises(APIError) as caught:
                        request_json(Request('https://example.com'), {}, opener)
                self.assertIn('category=' + category, str(caught.exception))
                self.assertNotIn('secret-key', str(caught.exception))
                self.assertEqual(opener.call_count, 1)
                sleep.assert_not_called()

    def test_dns_failure_after_rate_limit_stops(self):
        opener = Mock(side_effect=[failure(), URLError(socket.gaierror(11002, 'failed'))])
        with patch('template_parser.api.time.sleep') as sleep:
            with self.assertRaisesRegex(APIError, 'dns_error'):
                request_json(Request('https://example.com'), {}, opener)
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_raw_provider_error_is_classified_not_printed(self):
        details = diagnostics({"message":"Provider returned error", "metadata": {
            "provider_name":"Example", "raw": "Model temporarily rate-limited upstream. PRIVATE PROMPT"}},
            {"X-RateLimit-Remaining":"0"})
        self.assertIn("provider_name=Example", details)
        self.assertIn("category=upstream_capacity_or_rate_limit", details)
        self.assertIn("X-RateLimit-Remaining=0", details)
        self.assertNotIn("PRIVATE PROMPT", details)

    def test_metadata_daily_limit_stops_retries(self):
        body = {"error":{"message":"Provider returned error", "metadata":{"raw":{"message":"Daily limit exceeded"}}}}
        error = HTTPError('https://example.com',429,'error',{},BytesIO(json.dumps(body).encode()))
        opener = Mock(side_effect=error)
        with self.assertRaisesRegex(APIError, 'category=daily_limit'):
            request_json(Request('https://example.com'),{},opener)
        self.assertEqual(opener.call_count,1)
    def test_retry_after_then_success(self):
        opener = Mock(side_effect=[failure(headers={"Retry-After": "3"}), BytesIO(b'{"ok": true}')])
        with patch("template_parser.api.time.sleep") as sleep:
            self.assertEqual(request_json(Request("https://example.com"), {}, opener), {"ok": True})
            sleep.assert_called_once_with(3)

    def test_daily_quota_no_retry(self):
        opener = Mock(side_effect=failure("Daily limit exceeded"))
        with patch("template_parser.api.time.sleep") as sleep:
            with self.assertRaisesRegex(APIError, "Daily"):
                request_json(Request("https://example.com"), {}, opener)
            sleep.assert_not_called()
        self.assertEqual(opener.call_count, 1)

    def test_bounded_retries(self):
        opener = Mock(side_effect=[failure(), failure(), failure()])
        with patch("template_parser.api.time.sleep"):
            with self.assertRaises(APIError):
                request_json(Request("https://example.com"), {}, opener)
        self.assertEqual(opener.call_count, 3)

    def test_long_retry_after_and_secret_redaction(self):
        request = Request("https://example.com", headers={"Authorization": "Bearer secret-key"})
        opener = Mock(side_effect=failure("Rejected secret-key", {"Retry-After": "120"}))
        with patch("template_parser.api.time.sleep") as sleep:
            with self.assertRaises(APIError) as caught:
                request_json(request, {}, opener)
            sleep.assert_not_called()
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertIn("120", str(caught.exception))

    def test_auth_error_not_retried(self):
        opener = Mock(side_effect=failure("Unauthorized", code=401))
        with self.assertRaisesRegex(APIError, "401"):
            request_json(Request("https://example.com"), {}, opener)
        self.assertEqual(opener.call_count, 1)
