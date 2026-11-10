"""OpenSkin-only proxy profile with credentials kept in the OS vault."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QSettings

from integrations.asf.proxy_config import SteamProxy, parse_proxy_list_text


def parse_openskin_proxy_text(value: str) -> "OpenSkinProxy":
    """Parse one proxy line through the shared ASF parser."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Вставь прокси целиком в одно поле.")
    line = value.strip()
    if "://" not in line and "@" in line:
        line = "http://" + line
    try:
        parsed = parse_proxy_list_text(line)
    except ValueError:
        raise ValueError(
            "Не удалось распознать прокси. Используй URI, host:port или host:port:логин:пароль."
        ) from None
    if len(parsed) != 1:
        raise ValueError("Укажи ровно один прокси в одной строке.")
    proxy = parsed[0]
    try:
        return OpenSkinProxy(proxy.scheme, proxy.host, proxy.port,
                             proxy.username, proxy.password)
    except ValueError as exc:
        raise ValueError(str(exc)) from None


@dataclass(frozen=True, repr=False)
class OpenSkinProxy:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False, compare=True)

    def __post_init__(self):
        scheme = str(self.scheme).strip().casefold()
        if scheme not in {"http", "socks5"}:
            raise ValueError("Для OpenSkin поддерживаются HTTP CONNECT и SOCKS5")
        normalized = SteamProxy(scheme, self.host, self.port, self.username, self.password)
        object.__setattr__(self, "scheme", normalized.scheme)
        object.__setattr__(self, "host", normalized.host)
        object.__setattr__(self, "username", normalized.username)

    def safe_summary(self) -> dict[str, object]:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "authentication": self.username is not None,
        }


class OpenSkinProxySettingsStore:
    SERVICE = "GameDelivery.OpenSkinProxy"
    ACCOUNT = "pricing-api"
    PREFIX = "openskin/proxy/"

    def __init__(self, *, settings=None, keyring_backend: Any | None = None):
        self.settings = settings or QSettings("FANFIRIEL-LANG", "GameDelivery")
        self._keyring_backend = keyring_backend

    def _backend(self):
        if self._keyring_backend is not None:
            backend = self._keyring_backend
        else:
            try:
                import keyring
                backend = keyring.get_keyring()
            except Exception:
                raise RuntimeError(
                    "Системное хранилище учётных данных для прокси недоступно"
                ) from None
        if not getattr(backend, "priority", 0) or backend.priority < 1:
            raise RuntimeError(
                "Системное хранилище учётных данных для прокси недоступно"
            )
        return backend

    def load_profile(self) -> dict[str, object]:
        """Read non-secret settings only; never place the password in QSettings."""
        value = self.settings.value
        try:
            port = int(value(self.PREFIX + "port", 8080))
        except (TypeError, ValueError):
            port = 8080
        return {
            "enabled": bool(value(self.PREFIX + "enabled", False, type=bool)),
            "scheme": str(value(self.PREFIX + "scheme", "http") or "http"),
            "host": str(value(self.PREFIX + "host", "") or ""),
            "port": port,
            "username": str(value(self.PREFIX + "username", "") or ""),
        }

    def saved_password(self) -> str | None:
        try:
            return self._backend().get_password(self.SERVICE, self.ACCOUNT)
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError(
                "Не удалось получить пароль прокси из системного хранилища"
            ) from None

    def load_enabled_proxy(self) -> OpenSkinProxy | None:
        profile = self.load_profile()
        if not profile["enabled"]:
            return None
        username = str(profile["username"]).strip() or None
        password = self.saved_password() if username else None
        if username and not password:
            raise RuntimeError("Пароль OpenSkin proxy отсутствует в системном хранилище")
        return OpenSkinProxy(
            str(profile["scheme"]), str(profile["host"]), int(profile["port"]),
            username, password,
        )

    def save(self, *, enabled: bool, scheme: str, host: str, port: int,
             username: str | None, password: str | None) -> None:
        username = str(username or "").strip() or None
        password = password or None
        if not enabled and not str(host).strip() and username is None and password is None:
            for suffix, value in (("enabled", False), ("scheme", "http"),
                                  ("host", ""), ("port", 8080), ("username", "")):
                self.settings.setValue(self.PREFIX + suffix, value)
            self.settings.sync()
            try:
                self._backend().delete_password(self.SERVICE, self.ACCOUNT)
            except RuntimeError:
                pass
            except Exception:
                raise RuntimeError(
                    "Профиль очищен, но пароль не удалось удалить из системного хранилища"
                ) from None
            return
        if username and not password:
            password = self.saved_password()
            if not password:
                raise ValueError("Введи пароль прокси или сохрани его в системном хранилище")
        proxy = OpenSkinProxy(scheme, host, port, username, password)

        backend = self._backend() if proxy.password is not None else None
        previous_password = None
        if backend is not None:
            try:
                previous_password = backend.get_password(self.SERVICE, self.ACCOUNT)
                backend.set_password(self.SERVICE, self.ACCOUNT, proxy.password)
                if backend.get_password(self.SERVICE, self.ACCOUNT) != proxy.password:
                    raise RuntimeError("Credential vault verification failed")
            except Exception:
                raise RuntimeError(
                    "Не удалось сохранить пароль прокси в системном хранилище"
                ) from None

        try:
            self.settings.setValue(self.PREFIX + "enabled", bool(enabled))
            self.settings.setValue(self.PREFIX + "scheme", proxy.scheme)
            self.settings.setValue(self.PREFIX + "host", proxy.host)
            self.settings.setValue(self.PREFIX + "port", proxy.port)
            self.settings.setValue(self.PREFIX + "username", proxy.username or "")
            self.settings.sync()
            if self.settings.status() != QSettings.Status.NoError:
                raise RuntimeError("Settings write failed")
        except Exception:
            if backend is not None:
                try:
                    if previous_password is None:
                        backend.delete_password(self.SERVICE, self.ACCOUNT)
                    else:
                        backend.set_password(self.SERVICE, self.ACCOUNT, previous_password)
                except Exception:
                    pass
            raise RuntimeError("Не удалось сохранить параметры OpenSkin proxy") from None

        if proxy.password is None:
            try:
                backend = self._backend()
                backend.delete_password(self.SERVICE, self.ACCOUNT)
            except RuntimeError:
                # No credentials were configured; a missing vault is acceptable.
                pass
            except Exception:
                raise RuntimeError(
                    "Параметры прокси сохранены, но пароль не удалось удалить из системного хранилища"
                ) from None
