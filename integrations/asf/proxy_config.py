from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5"})


@dataclass(frozen=True)
class SteamProxy:
    """Proxy settings for one ASF bot; credentials stay out of the proxy URI."""

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        scheme = self.scheme.strip().lower()
        host = self.host.strip()
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            raise ValueError("Неподдерживаемый тип прокси для ASF")
        if not host or any(char in host for char in "/\\?#@"):
            raise ValueError("Укажи корректный адрес прокси без схемы и пути")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("Порт прокси должен быть числом от 1 до 65535")
        if (self.username is None) != (self.password is None):
            raise ValueError("Для авторизации прокси укажи и логин, и пароль")
        if self.username is not None and (not self.username or not self.password):
            raise ValueError("Логин и пароль прокси не должны быть пустыми")
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "host", host)

    @property
    def uri(self) -> str:
        """ASF proxy URI; credentials deliberately are not embedded in the URI."""
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{self.scheme}://{host}:{self.port}"

    def asf_bot_settings(self) -> dict[str, object]:
        """Return per-bot WebProxy fields only.

        SteamProtocols is an ASF global setting, not a bot setting. It must be
        handled separately because changing it affects every ASF bot.
        """
        return {
            "WebProxy": self.uri,
            "WebProxyUsername": self.username,
            "WebProxyPassword": self.password,
        }

    def safe_summary(self) -> dict[str, object]:
        """Safe display/log summary that never exposes proxy credentials."""
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "authentication": self.username is not None,
            "steam_connection_proxy": "requires global ASF SteamProtocols=4",
        }


def parse_proxy_uri(uri: str, username: str | None = None, password: str | None = None) -> SteamProxy:
    """Parse a proxy URI without accepting credentials embedded in the URI."""
    parsed = urlsplit(uri.strip())
    if parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("Укажи поддерживаемый тип прокси: HTTP(S) или SOCKS4/4a/5")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Логин и пароль вводятся в отдельных полях, не в адресе прокси")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Адрес прокси не должен содержать путь, параметры или фрагмент")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректный порт прокси") from exc
    if not parsed.hostname or port is None:
        raise ValueError("Адрес прокси должен содержать хост и порт")
    return SteamProxy(parsed.scheme, parsed.hostname, port, username, password)
