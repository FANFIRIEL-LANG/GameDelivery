import pytest

from integrations.asf.proxy_config import SteamProxy, parse_proxy_uri


def test_proxy_applies_to_web_and_steam_connection():
    proxy = SteamProxy("SOCKS5", "proxy.example", 1080, "proxy-user", "secret")
    settings = proxy.asf_bot_settings()

    assert settings["WebProxy"] == "socks5://proxy.example:1080"
    assert settings["WebProxyUsername"] == "proxy-user"
    assert settings["WebProxyPassword"] == "secret"
    assert settings["SteamProtocols"] == 4
    assert "secret" not in proxy.uri
    assert "secret" not in str(proxy.safe_summary())


def test_proxy_without_auth_clears_old_credentials():
    settings = SteamProxy("http", "127.0.0.1", 8888).asf_bot_settings()

    assert settings["WebProxy"] == "http://127.0.0.1:8888"
    assert settings["WebProxyUsername"] is None
    assert settings["WebProxyPassword"] is None
    assert settings["SteamProtocols"] == 4


def test_parse_proxy_uri():
    proxy = parse_proxy_uri("socks5://proxy.example:1080", "u", "p")
    assert (proxy.scheme, proxy.host, proxy.port) == ("socks5", "proxy.example", 1080)


@pytest.mark.parametrize(
    "uri",
    [
        "ftp://proxy.example:21",
        "http://user:password@proxy.example:8080",
        "http://proxy.example",
        "http://proxy.example:8080/path",
        "http://proxy.example:70000",
    ],
)
def test_rejects_unsupported_or_ambiguous_proxy_uri(uri):
    with pytest.raises(ValueError):
        parse_proxy_uri(uri)


def test_rejects_invalid_port_and_partial_auth():
    with pytest.raises(ValueError):
        SteamProxy("http", "proxy.example", 0)
    with pytest.raises(ValueError):
        SteamProxy("http", "proxy.example", 8080, username="user")
