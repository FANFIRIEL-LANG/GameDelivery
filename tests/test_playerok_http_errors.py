import io
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from integrations.playerok.client import PlayerokAdapter, PlayerokHTTPError


class PlayerokHttpErrorTests(unittest.TestCase):
    def _request_with_http_error(self, *, headers, body):
        url = "https://playerok.com/rest-api/public/auth/send-otp"
        error = HTTPError(
            url,
            500,
            "Internal Server Error",
            headers,
            io.BytesIO(body),
        )
        opener = unittest.mock.Mock()
        opener.open.side_effect = error
        adapter = PlayerokAdapter()

        with patch("integrations.playerok.client.build_opener", return_value=opener):
            with self.assertRaises(PlayerokHTTPError) as raised:
                adapter._request(
                    url,
                    method="POST",
                    payload={"email": "user@example.test"},
                    operation="send-otp",
                )
        return str(raised.exception)

    def test_send_otp_wire_request_keeps_existing_method_url_headers_and_body(self):
        class Response:
            headers = Message()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        class CapturingOpener:
            request = None

            def open(self, request, *, timeout):
                self.request = request
                return Response()

        opener = CapturingOpener()
        adapter = PlayerokAdapter()
        with patch("integrations.playerok.client.build_opener", return_value=opener):
            result = adapter.request_login_code("user@example.test")

        request = opener.request
        self.assertEqual(request.full_url, "https://playerok.com/rest-api/public/auth/send-otp")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b'{"email": "user@example.test"}')
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(
            request.get_header("User-agent"),
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Origin"), "https://playerok.com")
        self.assertEqual(request.get_header("Referer"), "https://playerok.com/")
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(result, {"sent": True, "email": "user@example.test"})

    def test_http_error_shows_safe_allowlisted_diagnostics_only(self):
        headers = Message()
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["X-Request-ID"] = "req-123456"
        headers["Traceparent"] = (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
        )
        headers["Authorization"] = "Bearer response-secret"
        headers["Set-Cookie"] = "token=response-cookie-secret"
        body = (
            b'{"code":"INTERNAL_ERROR","email":"user@example.test",'
            b'"message":"password=do-not-show token=do-not-show"}'
        )

        message = self._request_with_http_error(headers=headers, body=body)

        self.assertIn("HTTP 500", message)
        self.assertIn("send-otp", message)
        self.assertIn("application/json", message)
        self.assertIn("req-123456", message)
        self.assertIn(
            "trace_id=00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
            message,
        )
        self.assertIn("INTERNAL_ERROR", message)
        for secret in (
            "user@example.test",
            "response-secret",
            "response-cookie-secret",
            "do-not-show",
            "password=",
            "token=",
        ):
            self.assertNotIn(secret, message)

    def test_invalid_error_code_and_unsafe_request_id_are_omitted(self):
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        headers["X-Request-ID"] = "user@example.test"
        body = b'{"code":"user@example.test","message":"private response text"}'

        message = self._request_with_http_error(headers=headers, body=body)

        self.assertIn("HTTP 500", message)
        self.assertIn("text/html", message)
        self.assertNotIn("user@example.test", message)
        self.assertNotIn("private response text", message)
        self.assertNotIn("код ошибки", message)
        self.assertNotIn("request_id", message)

    def test_error_code_that_looks_like_a_credential_is_omitted(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        body = b'{"code":"ACCESS_TOKEN_VALUE"}'

        message = self._request_with_http_error(headers=headers, body=body)

        self.assertIn("HTTP 500", message)
        self.assertNotIn("ACCESS_TOKEN_VALUE", message)
        self.assertNotIn("код ошибки", message)


if __name__ == "__main__":
    unittest.main()
