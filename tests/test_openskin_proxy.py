import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLineEdit

from integrations.openskin.client import (
    OpenSkinClient, OpenSkinError, PricePosition, build_qnetwork_proxy,
    classify_qnetwork_error, probe_proxy_connection, redact_network_detail,
    _log_network_failure, _qt_proxy_request,
)
from integrations.openskin.proxy import (
    OpenSkinProxy, OpenSkinProxySettingsStore, parse_openskin_proxy_text,
)


class FakeKeyring:
    priority = 1

    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class OpenSkinProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_single_line_proxy_parser_accepts_common_formats(self):
        cases = [
            ("http://user:pass@proxy.example:8080", "http", "proxy.example", 8080, "user", "pass"),
            ("socks5://user:pass@proxy.example:1080", "socks5", "proxy.example", 1080, "user", "pass"),
            ("proxy.example:8081:user:pass", "http", "proxy.example", 8081, "user", "pass"),
            ("proxy.example:8082", "http", "proxy.example", 8082, None, None),
            ("user:pass@proxy.example:8083", "http", "proxy.example", 8083, "user", "pass"),
            ("http://encoded%40user:p%3Ass@proxy.example:8084", "http", "proxy.example", 8084, "encoded@user", "p:ss"),
            ("socks5://ipv6-user:ipv6-pass@[2001:db8::10]:1080", "socks5", "2001:db8::10", 1080, "ipv6-user", "ipv6-pass"),
        ]
        for value, scheme, host, port, username, password in cases:
            with self.subTest(host=host, port=port):
                proxy = parse_openskin_proxy_text(value)
                self.assertEqual(
                    (proxy.scheme, proxy.host, proxy.port, proxy.username, proxy.password),
                    (scheme, host, port, username, password),
                )
                if password:
                    self.assertNotIn(password, repr(proxy))
                    self.assertNotIn(password, str(proxy.safe_summary()))

    def test_single_line_proxy_parser_rejects_unsupported_and_invalid_values_without_echoing_secrets(self):
        for value in (
            "https://user:secret@proxy.example:443",
            "http://user:secret@proxy.example",
            "proxy.example:70000",
            "user:secret@proxy.example",
            "",
        ):
            with self.subTest(value=value.split("@")[-1]):
                with self.assertRaises(ValueError) as caught:
                    parse_openskin_proxy_text(value)
                self.assertNotIn("secret", str(caught.exception))

    def test_proxy_credentials_are_kept_out_of_repr_and_safe_summary(self):
        proxy = OpenSkinProxy("http", "proxy.example", 8080, "vpn-user", "vault-only")

        self.assertNotIn("vault-only", repr(proxy))
        self.assertNotIn("vault-only", json.dumps(proxy.safe_summary()))
        self.assertEqual(proxy.safe_summary()["host"], "proxy.example")

    def test_proxy_types_are_bound_to_request_scoped_qt_proxy_objects(self):
        from PySide6.QtNetwork import QNetworkProxy

        application_proxy_before = QNetworkProxy.applicationProxy()
        http = build_qnetwork_proxy(OpenSkinProxy("http", "proxy.example", 8080, "http-user", "http-pass"))
        socks = build_qnetwork_proxy(OpenSkinProxy("socks5", "socks.example", 1080, "socks-user", "socks-pass"))

        self.assertEqual(http.type(), QNetworkProxy.ProxyType.HttpProxy)
        self.assertEqual(socks.type(), QNetworkProxy.ProxyType.Socks5Proxy)
        self.assertEqual(http.hostName(), "proxy.example")
        self.assertEqual(http.user(), "http-user")
        self.assertEqual(http.password(), "http-pass")
        self.assertEqual(socks.port(), 1080)
        self.assertEqual(socks.user(), "socks-user")
        self.assertEqual(socks.password(), "socks-pass")
        self.assertTrue(socks.capabilities() & QNetworkProxy.Capability.HostNameLookupCapability)
        self.assertEqual(QNetworkProxy.applicationProxy(), application_proxy_before)

    def test_already_finished_qt_reply_does_not_wait_for_an_unobservable_finished_signal(self):
        from PySide6.QtNetwork import QNetworkReply
        from unittest.mock import patch

        class Signal:
            def connect(self, _callback):
                pass

        class Reply:
            sslErrors = Signal()
            finished = Signal()
            requestSent = Signal()

            def isFinished(self):
                return True

            def attribute(self, _attribute):
                return 204

            def rawHeaderList(self):
                return []

            def error(self):
                return QNetworkReply.NetworkError.NoError

            def errorString(self):
                return ""

            def deleteLater(self):
                pass

        class Manager:
            proxyAuthenticationRequired = Signal()

            def setProxy(self, _proxy):
                pass

            def get(self, _request):
                return Reply()

            def deleteLater(self):
                pass

        class EventLoop:
            def exec(self):
                raise AssertionError("must not wait after the reply has finished")

            def quit(self):
                pass

        class Timer:
            timeout = Signal()

            def setSingleShot(self, _enabled):
                pass

            def start(self, _milliseconds):
                raise AssertionError("must not start timeout for a finished reply")

            def stop(self):
                pass

        proxy = OpenSkinProxy("http", "proxy.example", 8080)
        with patch("integrations.openskin.client.QNetworkAccessManager", Manager), \
                patch("integrations.openskin.client.QEventLoop", EventLoop), \
                patch("integrations.openskin.client.QTimer", Timer):
            status, body, _headers = _qt_proxy_request(
                "https://api.openskin.dev/", b"", {}, 0.01, proxy, method="GET",
            )

        self.assertEqual((status, body), (204, b""))

    def test_https_proxy_transport_is_rejected_when_qt_does_not_support_it(self):
        with self.assertRaises(ValueError):
            OpenSkinProxy("https", "proxy.example", 443)

    def test_qt_network_errors_are_classified_by_failure_stage(self):
        from PySide6.QtNetwork import QNetworkReply

        cases = [
            (QNetworkReply.NetworkError.ProxyNotFoundError, 0, "http", False, "proxy_dns"),
            (QNetworkReply.NetworkError.HostNotFoundError, 0, "socks5", False, "dns"),
            (QNetworkReply.NetworkError.ProxyConnectionRefusedError, 0, "http", False, "proxy_tcp"),
            (QNetworkReply.NetworkError.ConnectionRefusedError, 0, "socks5", False, "tcp"),
            (QNetworkReply.NetworkError.ProxyAuthenticationRequiredError, 0, "http", False, "proxy_auth"),
            (QNetworkReply.NetworkError.ProxyTimeoutError, 0, "http", False, "proxy_timeout"),
            (QNetworkReply.NetworkError.SslHandshakeFailedError, 0, "http", False, "tls"),
            (QNetworkReply.NetworkError.UnknownProxyError, 0, "socks5", False, "socks5_handshake"),
            (QNetworkReply.NetworkError.UnknownProxyError, 0, "http", False, "http_tunnel"),
        ]
        for error, status, scheme, timed_out, expected in cases:
            with self.subTest(expected=expected):
                category, _message = classify_qnetwork_error(error, status, scheme, timed_out=timed_out)
                self.assertEqual(category, expected)

        timeout_cases = (
            ("http", False, False, False, False, "connect_or_tls_timeout"),
            ("socks5", False, False, False, False, "socks5_or_tls_timeout"),
            ("http", False, True, False, False, "pre_response_timeout"),
            ("http", False, False, True, False, "proxy_auth_timeout"),
            ("http", False, False, True, True, "connect_or_tls_timeout"),
            ("http", True, True, False, False, "response_timeout"),
        )
        for scheme, sent, tls_done, auth_requested, auth_answered, expected in timeout_cases:
            with self.subTest(timeout_stage=expected):
                category, message = classify_qnetwork_error(
                    QNetworkReply.NetworkError.OperationCanceledError, 0, scheme,
                    timed_out=True, request_sent=sent,
                    tls_completed=tls_done, proxy_auth_requested=auth_requested,
                    proxy_auth_answered=auth_answered,
                )
                self.assertEqual(category, expected)
                if expected == "connect_or_tls_timeout":
                    self.assertIn("не доказывает", message)

    def test_http_statuses_are_distinguished_from_proxy_transport_failures(self):
        from PySide6.QtNetwork import QNetworkReply

        for status, category in ((403, "http_403"), (407, "proxy_auth"),
                                 (429, "http_429"), (503, "http_5xx")):
            with self.subTest(status=status):
                actual, message = classify_qnetwork_error(
                    QNetworkReply.NetworkError.NoError, status, "http",
                )
                self.assertEqual(actual, category)
                self.assertIn(str(status), message)

    def test_proxy_probe_uses_same_qt_transport_and_distinguishes_proxy_auth(self):
        proxy = OpenSkinProxy("http", "proxy.example", 8080, "user", "secret")
        requests = []

        def request(url, body, headers, timeout, selected_proxy, *, method):
            requests.append((url, body, headers, timeout, selected_proxy, method))
            return 403, b"", {}

        status = probe_proxy_connection(proxy, request_func=request)

        self.assertEqual(status, 403)  # An HTTP response proves the proxy/TLS path connected.
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], "https://api.openskin.dev/")
        self.assertEqual(requests[0][5], "GET")
        self.assertEqual(requests[0][2]["Range"], "bytes=0-0")
        self.assertIs(requests[0][4], proxy)

        def auth_required(*_args, **_kwargs):
            raise OpenSkinError("Прокси отклонил авторизацию.", http_status=407,
                                category="proxy_auth")

        with self.assertRaises(OpenSkinError) as caught:
            probe_proxy_connection(proxy, request_func=auth_required)
        self.assertEqual(caught.exception.category, "proxy_auth")

    def test_profile_password_is_saved_only_in_keyring_and_loaded_for_proxy_use(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            keyring = FakeKeyring()
            store = OpenSkinProxySettingsStore(settings=settings, keyring_backend=keyring)

            store.save(
                enabled=True, scheme="http", host="proxy.example", port=8080,
                username="vpn-user", password="vault-only",
            )

            settings.sync()
            settings_text = Path(settings.fileName()).read_text(encoding="utf-8")
            self.assertNotIn("vault-only", settings_text)
            self.assertEqual(keyring.get_password(store.SERVICE, store.ACCOUNT), "vault-only")
            proxy = store.load_enabled_proxy()
            self.assertEqual(proxy, OpenSkinProxy(
                "http", "proxy.example", 8080, "vpn-user", "vault-only"
            ))

            store.save(
                enabled=False, scheme="http", host="proxy.example", port=8080,
                username="vpn-user", password="vault-only",
            )
            self.assertIsNone(store.load_enabled_proxy())
            store.save(
                enabled=True, scheme="http", host="proxy.example", port=8080,
                username="vpn-user", password="vault-only",
            )
            self.assertEqual(store.load_enabled_proxy().host, "proxy.example")

    def test_client_routes_only_openskin_request_through_configured_proxy(self):
        proxy = OpenSkinProxy("socks5", "proxy.example", 1080)
        direct_request = Mock(side_effect=AssertionError("direct transport used"))
        proxy_request = Mock(return_value=(200, b'{"results":[{"item":"AK-47 | Redline","steam":null}]}', {}))

        client = OpenSkinClient(
            proxy=proxy, request_func=direct_request,
            proxy_request_func=proxy_request,
        )
        result = client.fetch_prices([PricePosition(730, "AK-47 | Redline")])

        self.assertEqual(result[0].steam_ask, None)
        proxy_request.assert_called_once()
        self.assertEqual(proxy_request.call_args.args[-1], proxy)
        direct_request.assert_not_called()

    def test_settings_widget_uses_one_masked_proxy_line_and_saves_parsed_credentials(self):
        from app.openskin_proxy_settings import OpenSkinProxySettingsWidget

        store = Mock()
        store.load_profile.return_value = {
            "enabled": False, "scheme": "http", "host": "", "port": 8080,
            "username": "",
        }
        store.load_enabled_proxy.return_value = None
        widget = OpenSkinProxySettingsWidget(store=store)
        widget.enabled.setChecked(True)
        widget.proxy_input.setText("http://user:hidden-password@proxy.example:8080")

        self.assertEqual(len(widget.findChildren(QLineEdit)), 1)
        self.assertEqual(widget.proxy_input.echoMode(), QLineEdit.EchoMode.Password)
        widget.save_settings()

        store.save.assert_called_once_with(
            enabled=True, scheme="http", host="proxy.example", port=8080,
            username="user", password="hidden-password",
        )
        self.assertEqual(widget.proxy_input.text(), "")
        self.assertNotIn("hidden-password", widget.proxy_status.text())

    def test_proxy_check_task_runs_transport_probe_without_price_request(self):
        from app.openskin_proxy_settings import _ProxyCheckTask

        proxy = OpenSkinProxy("http", "proxy.example", 8080, "user", "hidden-password")
        task = _ProxyCheckTask(proxy)
        outcomes = []
        task.signals.finished.connect(lambda ok, message: outcomes.append((ok, message)))
        with unittest.mock.patch(
            "app.openskin_proxy_settings.probe_proxy_connection", return_value=403,
        ) as probe:
            task.run()

        probe.assert_called_once_with(proxy)
        self.assertEqual(outcomes[0][0], True)
        self.assertIn("HTTP 403", outcomes[0][1])
        self.assertNotIn(proxy.password, outcomes[0][1])

    def test_openskin_api_check_is_a_separate_read_only_task(self):
        from app.openskin_proxy_settings import _OpenSkinCheckTask

        proxy = OpenSkinProxy("socks5", "proxy.example", 1080)
        task = _OpenSkinCheckTask(proxy)
        outcomes = []
        task.signals.finished.connect(lambda ok, message: outcomes.append((ok, message)))
        client = Mock()
        with unittest.mock.patch("app.openskin_proxy_settings.OpenSkinClient", return_value=client):
            task.run()

        client.fetch_prices.assert_called_once_with(
            [PricePosition(730, "AK-47 | Redline (Field-Tested)")]
        )
        self.assertTrue(outcomes[0][0])

    def test_settings_show_separate_proxy_and_openskin_checks(self):
        from app.openskin_proxy_settings import OpenSkinProxySettingsWidget

        store = Mock()
        store.load_profile.return_value = {
            "enabled": False, "scheme": "http", "host": "", "port": 8080,
            "username": "",
        }
        widget = OpenSkinProxySettingsWidget(store=store)
        self.assertNotEqual(widget.proxy_check_button, widget.openskin_check_button)
        self.assertIn("прокси", widget.proxy_check_button.text().casefold())
        self.assertIn("openskin", widget.openskin_check_button.text().casefold())
        self.assertNotEqual(widget.proxy_status, widget.openskin_status)

    def test_settings_buttons_dispatch_to_their_separate_background_tasks(self):
        from app.openskin_proxy_settings import _OpenSkinCheckTask, _ProxyCheckTask
        from app.openskin_proxy_settings import OpenSkinProxySettingsWidget

        store = Mock()
        store.load_profile.return_value = {
            "enabled": True, "scheme": "http", "host": "proxy.example",
            "port": 8080, "username": "",
        }
        store.load_enabled_proxy.return_value = OpenSkinProxy("http", "proxy.example", 8080)
        widget = OpenSkinProxySettingsWidget(store=store)
        pool = Mock()

        with unittest.mock.patch("app.openskin_proxy_settings.QThreadPool.globalInstance",
                                 return_value=pool):
            widget.proxy_check_button.click()
            widget.openskin_check_button.click()

        tasks = [call.args[0] for call in pool.start.call_args_list]
        self.assertEqual(len(tasks), 2)
        self.assertIsInstance(tasks[0], _ProxyCheckTask)
        self.assertIsInstance(tasks[1], _OpenSkinCheckTask)
        self.assertEqual(tasks[0].proxy.host, "proxy.example")
        self.assertEqual(tasks[1].proxy.host, "proxy.example")

    def test_saved_profile_flows_from_settings_buttons_to_same_proxy_transport(self):
        from app.openskin_proxy_settings import OpenSkinProxySettingsWidget

        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            keyring = FakeKeyring()
            store = OpenSkinProxySettingsStore(settings=settings, keyring_backend=keyring)
            store.save(
                enabled=True, scheme="socks5", host="proxy.example", port=1080,
                username="saved-user", password="saved-password",
            )
            widget = OpenSkinProxySettingsWidget(store=store)
            pool = Mock()
            calls = []

            def transport(url, body, headers, timeout, proxy, *, method="POST"):
                calls.append((url, method, proxy))
                if method == "GET":
                    return 403, b"", {}
                return 200, json.dumps({"results": [{
                    "item": "AK-47 | Redline (Field-Tested)", "steam": None,
                }]}).encode(), {}

            with unittest.mock.patch("app.openskin_proxy_settings.QThreadPool.globalInstance",
                                     return_value=pool), \
                    unittest.mock.patch("integrations.openskin.client._qt_proxy_request",
                                        side_effect=transport):
                widget.proxy_check_button.click()
                proxy_task = pool.start.call_args.args[0]
                self.assertEqual(widget.proxy_input.text(), "")
                proxy_task.run()
                widget.openskin_check_button.click()
                api_task = pool.start.call_args.args[0]
                api_task.run()

        self.assertEqual([method for _, method, _ in calls], ["GET", "POST"])
        self.assertTrue(all(proxy.password == "saved-password" for _, _, proxy in calls))
        self.assertTrue(widget.proxy_status.text().startswith("Прокси, HTTPS"))
        self.assertIn("OpenSkin API ответил", widget.openskin_status.text())
        self.assertNotIn("saved-password", widget.proxy_status.text())
        self.assertNotIn("saved-password", widget.openskin_status.text())

    def test_network_diagnostic_redacts_proxy_credentials_and_url_userinfo(self):
        proxy = OpenSkinProxy("http", "proxy.example", 8080, "private-user", "private-pass")
        detail = redact_network_detail(
            "Proxy failed at http://private-user:private-pass@proxy.example:8080 (private-pass)",
            proxy,
        )
        self.assertNotIn("private-user", detail)
        self.assertNotIn("private-pass", detail)
        self.assertIn("[redacted]", detail)

    def test_technical_network_log_redacts_proxy_credentials(self):
        proxy = OpenSkinProxy("http", "proxy.example", 8080, "private-user", "private-pass")
        with self.assertLogs("integrations.openskin.client", level="WARNING") as captured:
            _log_network_failure(
                "proxy_auth", "ProxyAuthenticationRequiredError",
                "failed private-user private-pass", proxy,
            )
        joined = " ".join(captured.output)
        self.assertIn("proxy_auth", joined)
        self.assertNotIn("private-user", joined)
        self.assertNotIn("private-pass", joined)

    def test_persisted_proxy_diagnostics_are_redacted_and_rotation_is_configured(self):
        import logging
        from integrations.openskin.client import configure_openskin_diagnostics

        proxy = OpenSkinProxy("http", "proxy.example", 8080, "private-user", "private pass")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openskin-proxy.log"
            handler = configure_openskin_diagnostics(path)
            logger = logging.getLogger("integrations.openskin.client")
            self.assertEqual(handler.maxBytes, 512 * 1024)
            self.assertEqual(handler.backupCount, 2)
            try:
                _log_network_failure(
                    "proxy_auth", "ProxyAuthenticationRequiredError",
                    "credential private-user private%20pass\nforged line", proxy,
                )
            finally:
                logger.removeHandler(handler)
                handler.close()

            contents = path.read_text(encoding="utf-8")

        self.assertIn("proxy_auth", contents)
        self.assertIn("forged line", contents)
        self.assertNotIn("private-user", contents)
        self.assertNotIn("private pass", contents)
        self.assertNotIn("private%20pass", contents)

    def test_proxy_probe_and_price_post_share_one_manager_transport(self):
        import unittest.mock

        proxy = OpenSkinProxy("socks5", "proxy.example", 1080)
        calls = []

        def transport(url, body, headers, timeout, selected_proxy, *, method="POST"):
            calls.append((url, method, selected_proxy))
            if method == "GET":
                return 404, b"", {}
            return 200, b'{"results":[{"item":"Item","steam":{"ask":1,"currency":"USD"}}]}', {}

        with unittest.mock.patch("integrations.openskin.client._qt_proxy_request", side_effect=transport):
            self.assertEqual(probe_proxy_connection(proxy), 404)
            price, = OpenSkinClient(proxy=proxy).fetch_prices([PricePosition(730, "Item")])

        self.assertEqual(price.steam_ask, 1)
        self.assertEqual([(method, selected_proxy) for _, method, selected_proxy in calls], [
            ("GET", proxy), ("POST", proxy),
        ])

    def test_proxy_probe_logs_safe_traceback_for_unexpected_transport_error(self):
        proxy = OpenSkinProxy(
            "http", "proxy.example", 8080, "proxy-user", "proxy-pass",
        )

        def broken_request(*_args, **_kwargs):
            raise TypeError("transport rejected proxy-user proxy-pass")

        with self.assertLogs("integrations.openskin.client", level="WARNING") as captured:
            with self.assertRaises(TypeError):
                probe_proxy_connection(proxy, request_func=broken_request)

        output = "\\n".join(captured.output)
        self.assertIn("context=proxy_probe", output)
        self.assertIn("exception=TypeError", output)
        self.assertIn("test_openskin_proxy.py", output)
        self.assertIn("broken_request", output)
        self.assertNotIn("proxy-user", output)
        self.assertNotIn("proxy-pass", output)

