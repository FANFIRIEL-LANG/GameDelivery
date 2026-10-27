import unittest

from integrations.asf.proxy_config import SteamProxy, parse_proxy_uri, parse_proxy_list_text


class ProxyConfigTests(unittest.TestCase):
    def test_bot_settings_only_contain_per_bot_proxy_fields(self):
        proxy = SteamProxy("SOCKS5", "proxy.example", 1080, "proxy-user", "secret")
        settings = proxy.asf_bot_settings()
        self.assertEqual(settings["WebProxy"], "socks5://proxy.example:1080")
        self.assertEqual(settings["WebProxyUsername"], "proxy-user")
        self.assertEqual(settings["WebProxyPassword"], "secret")
        self.assertNotIn("SteamProtocols", settings)
        self.assertIn("requires global ASF SteamProtocols=4", str(proxy.safe_summary()))
        self.assertNotIn("secret", proxy.uri)
        self.assertNotIn("secret", str(proxy.safe_summary()))

    def test_proxy_without_auth_clears_old_credentials(self):
        settings = SteamProxy("http", "127.0.0.1", 8888).asf_bot_settings()
        self.assertEqual(settings["WebProxy"], "http://127.0.0.1:8888")
        self.assertIsNone(settings["WebProxyUsername"])
        self.assertIsNone(settings["WebProxyPassword"])
        self.assertNotIn("SteamProtocols", settings)

    def test_parse_proxy_uri(self):
        proxy = parse_proxy_uri("socks5://proxy.example:1080", "u", "p")
        self.assertEqual((proxy.scheme, proxy.host, proxy.port), ("socks5", "proxy.example", 1080))

    def test_rejects_unsupported_or_ambiguous_proxy_uris(self):
        for uri in (
            "ftp://proxy.example:21",
            "http://proxy.example",
            "http://proxy.example:8080/path",
            "http://proxy.example:70000",
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError):
                    parse_proxy_uri(uri)

    def test_parse_proxy_uri_extracts_embedded_credentials(self):
        proxy = parse_proxy_uri("socks5://proxy-user:secret@proxy.example:1080")
        self.assertEqual(proxy.username, "proxy-user")
        self.assertEqual(proxy.password, "secret")
        self.assertEqual(proxy.uri, "socks5://proxy.example:1080")

    def test_parse_proxy_list_text_accepts_authenticated_and_plain_proxy_rows(self):
        proxies = parse_proxy_list_text("\n".join([
            "# main list",
            "socks5://proxy-one.example:1080;proxy-user;secret",
            "http://proxy-two.example:8080",
        ]))
        self.assertEqual(len(proxies), 2)
        self.assertEqual(proxies[0].scheme, "socks5")
        self.assertEqual(proxies[0].username, "proxy-user")
        self.assertEqual(proxies[0].password, "secret")
        self.assertEqual(proxies[1].uri, "http://proxy-two.example:8080")

    def test_parse_proxy_list_text_reports_line_without_exposing_proxy_password(self):
        with self.assertRaisesRegex(ValueError, "Строка 2"):
            parse_proxy_list_text("\n".join([
                "http://ok.example:8080",
                "http://proxy.example:8080;user",
            ]))

    def test_proxy_list_accepts_all_documented_auth_separators_and_plain_uri(self):
        rows = [
            "socks5://semi.example:1080;user1;pass1",
            "socks5://pipe.example:1081|user2|pass2",
            "socks5://comma.example:1082,user3,pass3",
            "socks5://tab.example:1083\tuser4\tpass4",
            "socks5://embedded:pass5@uri.example:1084",
            "socks5://plain.example:1085",
        ]
        proxies = parse_proxy_list_text("\n".join(rows))
        self.assertEqual(len(proxies), 6)
        self.assertEqual(
            [(proxy.username, proxy.password) for proxy in proxies],
            [
                ("user1", "pass1"),
                ("user2", "pass2"),
                ("user3", "pass3"),
                ("user4", "pass4"),
                ("embedded", "pass5"),
                (None, None),
            ],
        )
        for proxy, host in zip(
            proxies,
            ("semi.example", "pipe.example", "comma.example", "tab.example", "uri.example", "plain.example"),
        ):
            settings = proxy.asf_bot_settings()
            self.assertEqual(settings["WebProxy"], f"socks5://{host}:{proxy.port}")
            self.assertNotIn("@", settings["WebProxy"])
        for proxy, secret in zip(proxies[:5], ("pass1", "pass2", "pass3", "pass4", "pass5")):
            self.assertNotIn(secret, proxy.uri)
            self.assertNotIn(secret, str(proxy.safe_summary()))

    def test_rejects_invalid_port_and_partial_auth(self):
        with self.assertRaises(ValueError):
            SteamProxy("http", "proxy.example", 0)
        with self.assertRaises(ValueError):
            SteamProxy("http", "proxy.example", 8080, username="user")


if __name__ == "__main__":
    unittest.main()
