from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ASFError(RuntimeError):
    """Raised when ASF IPC cannot complete a request."""


def _find_key(mapping: dict[str, Any], key: str) -> str | None:
    wanted = key.casefold()
    return next((actual for actual in mapping if actual.casefold() == wanted), None)


def _unwrap_result(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise ASFError("ASF вернул ответ неожиданного формата")
    success_key = _find_key(payload, "Success")
    if success_key is not None and payload[success_key] is False:
        raise ASFError("ASF не подтвердил выполнение операции")
    result_key = _find_key(payload, "Result")
    return payload[result_key] if result_key is not None else payload


@dataclass(frozen=True)
class ASFClient:
    """Minimal client for the local, official ASF IPC API.

    Keep IPC bound to localhost. Configure IPCPassword in ASF and pass it
    to this client from local settings; never commit it or write it to logs.
    """

    base_url: str = "http://127.0.0.1:1242"
    ipc_password: str | None = None
    timeout: float = 5.0

    def request(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None
    ) -> Any:
        if not path.startswith("/Api/"):
            raise ValueError("Разрешены только пути ASF API, начинающиеся с /Api/")
        if "://" in path or "\n" in path or "\r" in path:
            raise ValueError("Некорректный путь ASF API")
        if not self.base_url.startswith("http://127.0.0.1") and not self.base_url.startswith("http://localhost"):
            raise ValueError("ASF IPC должен оставаться доступным только через localhost")

        url = self.base_url.rstrip("/") + path
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.ipc_password:
            headers["Authentication"] = self.ipc_password

        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ASFError("ASF вернул ответ не в формате JSON") from exc
        except HTTPError as exc:
            if exc.code == 401:
                raise ASFError("ASF IPC отклонил авторизацию; проверь IPCPassword") from None
            if exc.code == 403:
                raise ASFError("ASF IPC временно заблокировал запросы или запретил доступ") from None
            raise ASFError(f"ASF IPC вернул HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ASFError("ASF IPC недоступен на локальном компьютере") from None

    def get_status(self) -> Any:
        return self.request("/Api/ASF")

    def get_bot(self, bot_name: str) -> Any:
        if not bot_name.strip():
            raise ValueError("Имя бота не должно быть пустым")
        return self.request("/Api/Bot/" + quote(bot_name, safe=""))

    def get_bot_config(self, bot_name: str) -> dict[str, Any]:
        """Read the bot config from the official bot object returned by ASF IPC."""
        payload = _unwrap_result(self.get_bot(bot_name))
        if not isinstance(payload, dict):
            raise ASFError("ASF не вернул объект бота")
        actual_bot_key = _find_key(payload, bot_name)
        if actual_bot_key is None:
            raise ASFError("ASF не вернул запрошенного бота")
        bot = payload[actual_bot_key]
        if not isinstance(bot, dict):
            raise ASFError("ASF вернул некорректный объект бота")
        config_key = _find_key(bot, "BotConfig")
        if config_key is None or not isinstance(bot[config_key], dict):
            raise ASFError("ASF не предоставил BotConfig; настройки не изменены")
        config = dict(bot[config_key])
        if not config:
            raise ASFError("ASF вернул пустую конфигурацию; настройки не изменены")
        return config

    def apply_proxy_settings(self, bot_name: str, proxy_settings: dict[str, Any]) -> dict[str, Any]:
        """Merge only proxy fields into a fetched BotConfig, then save it via ASF IPC.

        The response from ASF may contain account secrets; this method never
        returns or logs the config. ASF's BotPost endpoint writes the supplied
        complete config, so fail closed if the existing config cannot be read.
        """
        if not bot_name.strip():
            raise ValueError("Имя бота не должно быть пустым")
        allowed = {"WebProxy", "WebProxyUsername", "WebProxyPassword"}
        if set(proxy_settings) != allowed:
            raise ValueError("Переданы неполные или неожиданные параметры прокси")
        config = self.get_bot_config(bot_name)
        config.update(proxy_settings)
        payload = self.request(
            "/Api/Bot/" + quote(bot_name, safe=""),
            method="POST",
            body={"BotConfig": config},
        )
        result = _unwrap_result(payload)
        if result is False:
            raise ASFError("ASF не подтвердил сохранение конфигурации прокси")
        if isinstance(result, dict):
            bot_key = _find_key(result, bot_name)
            if bot_key is not None and result[bot_key] is False:
                raise ASFError("ASF не сохранил конфигурацию прокси")
        # Deliberately return only a safe summary; never expose BotConfig.
        return {
            "bot_name": bot_name,
            "settings_submitted": sorted(allowed),
            "saved": True,
            "steam_connection_proxy_requires_global_setting": True,
        }


    def get_global_config(self) -> dict[str, Any]:
        """Read the full public global config returned by ASF's /Api/ASF endpoint."""
        payload = _unwrap_result(self.get_status())
        if not isinstance(payload, dict):
            raise ASFError("ASF не вернул общую конфигурацию")
        config_key = _find_key(payload, "GlobalConfig")
        if config_key is None or not isinstance(payload[config_key], dict) or not payload[config_key]:
            raise ASFError("ASF не предоставил GlobalConfig; настройки не изменены")
        return dict(payload[config_key])

    def apply_global_steam_protocols(self, *, confirmed_instance_wide_change: bool = False) -> dict[str, Any]:
        """Opt-in global change: SteamProtocols=4 affects every bot in this ASF instance."""
        if confirmed_instance_wide_change is not True:
            raise ValueError(
                "Изменение затронет все боты ASF; требуется явное подтверждение пользователя"
            )
        config = self.get_global_config()
        protocol_key = _find_key(config, "SteamProtocols")
        if protocol_key is None:
            raise ASFError("ASF не вернул SteamProtocols; общая конфигурация не изменена")
        previous_value = config[protocol_key]
        if previous_value == 4:
            return {"saved": True, "steam_protocols": 4, "changed": False, "scope": "all ASF bots"}
        config[protocol_key] = 4
        payload = self.request("/Api/ASF", method="POST", body={"GlobalConfig": config})
        result = _unwrap_result(payload)
        if result is False:
            raise ASFError("ASF не подтвердил сохранение общей конфигурации")
        verified = self.get_global_config()
        verified_key = _find_key(verified, "SteamProtocols")
        if verified_key is None or verified[verified_key] != 4:
            raise ASFError("ASF не подтвердил SteamProtocols=4 после сохранения")
        return {"saved": True, "steam_protocols": 4, "changed": True, "scope": "all ASF bots"}

    def send_command(self, command: str) -> Any:
        """Low-level command primitive; not an automatic trade sender."""
        if not command.strip():
            raise ValueError("Команда ASF не должна быть пустой")
        return self.request("/Api/Command", method="POST", body={"Command": command})
