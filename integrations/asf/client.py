from __future__ import annotations

import base64
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen


class ASFError(RuntimeError):
    """Raised when ASF IPC cannot complete a request."""


def _restrict_pending_trade_permissions(path: Path, *, directory: bool = False) -> None:
    """Restrict temporary trade-token storage to its owner and SYSTEM.

    On Windows, use a protected DACL instead of relying on inherited permissions.
    On POSIX, remove all group/other access. Fail closed if hardening fails.
    """
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return

    import ctypes
    from ctypes import wintypes

    sddl = (
        "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"
        if directory
        else "D:P(A;;FA;;;OW)(A;;FA;;;SY)"
    )
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()

    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL

    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    get_dacl.restype = wintypes.BOOL

    set_named_security_info = advapi32.SetNamedSecurityInfoW
    set_named_security_info.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    set_named_security_info.restype = wintypes.DWORD

    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p

    if not convert(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not get_dacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl_present.value or not dacl.value:
            raise OSError("Не удалось создать ограниченный DACL для временного секрета")

        # SE_FILE_OBJECT; DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        result = set_named_security_info(
            str(path), 1, 0x80000004, None, None, dacl, None
        )
        if result != 0:
            raise ctypes.WinError(result)
    finally:
        local_free(descriptor)


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
        try:
            parsed_base = urlsplit(self.base_url)
            local_host = parsed_base.hostname in {"127.0.0.1", "localhost", "::1"}
            valid_port = parsed_base.port == 1242
        except ValueError:
            local_host = False
            valid_port = False
        if (
            parsed_base.scheme != "http"
            or not local_host
            or not valid_port
            or parsed_base.username is not None
            or parsed_base.password is not None
            or parsed_base.path not in {"", "/"}
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise ValueError("ASF IPC должен оставаться доступным только через localhost:1242")

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

    def get_bot_inventory(
        self,
        bot_name: str,
        *,
        app_id: int = 753,
        context_id: int = 6,
        language: str = "english",
    ) -> Any:
        """Fetch one bot's Steam inventory through ASF's documented IPC route."""
        if not bot_name.strip():
            raise ValueError("Имя бота не должно быть пустым")
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise ValueError("app_id должен быть положительным целым числом")
        if isinstance(context_id, bool) or not isinstance(context_id, int) or context_id <= 0:
            raise ValueError("context_id должен быть положительным целым числом")
        if not language.strip() or any(ch in language for ch in "/\\\\?#"):
            raise ValueError("Некорректный язык инвентаря")
        path = (
            "/Api/Bot/" + quote(bot_name, safe="")
            + f"/Inventory/{app_id}/{context_id}?language=" + quote(language, safe="")
        )
        return self.request(path)

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
        # Verify the non-secret proxy fields by reading the bot config back.
        # ASF may omit secret fields from responses, so never require the password
        # to be echoed and never return the config to the UI.
        verified = self.get_bot_config(bot_name)
        expected_uri = proxy_settings["WebProxy"]
        expected_username = proxy_settings["WebProxyUsername"]
        actual_uri_key = _find_key(verified, "WebProxy")
        actual_username_key = _find_key(verified, "WebProxyUsername")
        if actual_uri_key is None or verified[actual_uri_key] != expected_uri:
            raise ASFError("ASF не подтвердил адрес веб-прокси после сохранения")
        if actual_username_key is None or verified[actual_username_key] != expected_username:
            raise ASFError("ASF не подтвердил логин веб-прокси после сохранения")

        # Deliberately return only a safe summary; never expose BotConfig.
        return {
            "bot_name": bot_name,
            "settings_submitted": sorted(allowed),
            "saved": True,
            "verified": True,
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

    def start_bots(self, bot_names: Iterable[str]) -> dict[str, Any]:
        """Ask ASF to start several configured bots with one IPC command."""
        names = [str(name).strip() for name in bot_names]
        if not names or any(
            not name or "," in name or any(ord(char) < 32 for char in name)
            for name in names
        ):
            raise ValueError("Некорректный список имён ботов ASF для массового запуска")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("В списке массового запуска есть повторяющиеся боты")
        response = self.send_command("start " + ",".join(names))
        result = _unwrap_result(response)
        if result is False:
            raise ASFError("ASF не подтвердил команду массового запуска")
        return {"bot_names": names, "command_sent": True}

    def start_bot(self, bot_name: str) -> dict[str, Any]:
        """Ask ASF to start one configured bot, including a bot disabled in JSON."""
        name = bot_name.strip()
        if not name or "," in name or any(ord(char) < 32 for char in name):
            raise ValueError("Некорректное имя бота ASF для команды запуска")
        response = self.send_command("start " + name)
        result = _unwrap_result(response)
        if result is False:
            raise ASFError("ASF не подтвердил команду запуска бота")
        return {"bot_name": name, "command_sent": True}

    def stop_bot(self, bot_name: str) -> dict[str, Any]:
        """Ask ASF to stop one bot without stopping the ASF process itself."""
        name = bot_name.strip()
        if not name or "," in name or any(ord(char) < 32 for char in name):
            raise ValueError("Некорректное имя бота ASF для команды остановки")
        response = self.send_command("stop " + name)
        result = _unwrap_result(response)
        if result is False:
            raise ASFError("ASF не подтвердил команду остановки бота")
        return {"bot_name": name, "command_sent": True}

    def preview_trade_offer(
        self,
        *,
        order_id: str,
        bot_name: str,
        trade_url: str,
        asset_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Ask the installed GameDelivery ASF plugin to validate a no-send trade draft.

        The recipient token is validated locally but is never sent to ASF during
        preview. The plugin re-reads the live inventory and returns a preview only.
        """
        order = order_id.strip()
        bot = bot_name.strip()
        if not order or not bot:
            raise ValueError("Нужны ID заказа и имя бота ASF")

        parsed = urlsplit(trade_url.strip())
        params = parse_qs(parsed.query, keep_blank_values=True)
        partner_values = params.get("partner", [])
        token_values = params.get("token", [])
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}
            or parsed.path.rstrip("/") != "/tradeoffer/new"
            or len(partner_values) != 1
            or not partner_values[0].isdigit()
            or not 1 <= int(partner_values[0]) <= 4294967295
            or len(token_values) != 1
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token_values[0])
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("Укажи корректную HTTPS trade URL получателя Steam")

        normalized_ids = [str(value).strip() for value in asset_ids]
        if not normalized_ids or len(normalized_ids) > 255:
            raise ValueError("Для проверки нужно от 1 до 255 asset ID")
        if any(not value.isdigit() or int(value) <= 0 for value in normalized_ids):
            raise ValueError("Asset ID должны быть положительными целыми числами")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("В составе заказа найдены повторяющиеся asset ID")

        partner_id = int(partner_values[0])
        steam_id64 = 76561197960265728 + partner_id
        request_payload = {
            "OrderId": order,
            "BotName": bot,
            "TargetSteamId64": steam_id64,
            "AssetIds": normalized_ids,
        }
        encoded_payload = base64.urlsafe_b64encode(
            json.dumps(request_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("ascii").rstrip("=")
        response = self.request(
            "/Api/Command",
            method="POST",
            body={"Command": "gamedelivery-preview " + encoded_payload},
        )
        result = _unwrap_result(response)
        if not isinstance(result, str):
            raise ASFError(
                "ASF не вернул ответ GameDelivery-плагина. Проверь, что плагин установлен."
            )
        try:
            preview = json.loads(result)
        except json.JSONDecodeError:
            raise ASFError(
                "ASF вернул неожиданный ответ команды GameDelivery. Проверь установку плагина."
            ) from None

        if not isinstance(preview, dict) or preview.get("success") is not True:
            message = preview.get("error") if isinstance(preview, dict) else None
            raise ASFError(str(message or "ASF не подтвердил черновик обмена")[:240])
        preview_bot = preview.get("botName")
        if (
            preview.get("mode") != "preview-only"
            or preview.get("sendEnabled") is not False
            or preview.get("orderId") != order
            or not isinstance(preview_bot, str)
            or preview_bot.casefold() != bot.casefold()
            or preview.get("targetSteamId64") != steam_id64
        ):
            raise ASFError("ASF вернул некорректное подтверждение черновика обмена")

        preview_assets = preview.get("assets")
        if not isinstance(preview_assets, list):
            raise ASFError("ASF не вернул проверенный состав черновика")
        returned_ids = [
            str(asset.get("assetId", ""))
            for asset in preview_assets
            if isinstance(asset, dict)
        ]
        if len(returned_ids) != len(normalized_ids) or returned_ids != normalized_ids:
            raise ASFError("Состав черновика ASF не совпадает с активами заказа")
        return preview

    def get_bots(self) -> dict[str, Any]:
        """Return bot status when /Api/ASF exposes it; otherwise let callers use disk config.

        The ASF status endpoint does not guarantee a top-level Bots field across
        supported versions. Bot discovery must therefore not fail just because
        that optional field is absent: management screens merge this result with
        the bot JSON files discovered locally.
        """
        payload = _unwrap_result(self.get_status())
        if not isinstance(payload, dict):
            raise ASFError("ASF вернул ответ статуса в неожиданном формате")
        bots_key = _find_key(payload, "Bots")
        if bots_key is None:
            return {}
        bots_value = payload[bots_key]
        if isinstance(bots_value, dict):
            return dict(bots_value)
        if isinstance(bots_value, list):
            names = [str(name).strip() for name in bots_value if str(name).strip()]
            return {name: {} for name in names}
        raise ASFError("ASF вернул список Bots в неподдерживаемом формате")

    def send_trade_offer(
        self,
        *,
        order_id: str,
        bot_name: str,
        trade_url: str,
        asset_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Send one reviewed card offer via ASF; the trade token is passed only in a one-use local file."""
        order = order_id.strip()
        bot = bot_name.strip()
        if not order or len(order) > 128 or any(ord(ch) < 32 for ch in order):
            raise ValueError("Некорректный ID заказа для отправки")
        if not bot:
            raise ValueError("Нужно имя ASF-бота")

        parsed_url = urlsplit(trade_url.strip())
        params = parse_qs(parsed_url.query, keep_blank_values=True)
        partners = params.get("partner", [])
        tokens = params.get("token", [])
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}
            or parsed_url.path.rstrip("/") != "/tradeoffer/new"
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.fragment
            or len(partners) != 1
            or not partners[0].isdigit()
            or not 1 <= int(partners[0]) <= 4294967295
            or len(tokens) != 1
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tokens[0])
        ):
            raise ValueError("Укажи корректную HTTPS trade URL получателя Steam")

        normalized_ids = [str(value).strip() for value in asset_ids]
        if not normalized_ids or len(normalized_ids) > 255:
            raise ValueError("Для отправки нужно от 1 до 255 asset ID")
        if any(not value.isdigit() or int(value) <= 0 for value in normalized_ids):
            raise ValueError("Asset ID должны быть положительными целыми числами")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("В составе заказа найдены повторяющиеся asset ID")

        target_steam_id64 = 76561197960265728 + int(partners[0])
        nonce = secrets.token_hex(32)
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            local_app_data = str(Path.home() / "AppData" / "Local")
        secret_dir = Path(local_app_data) / "GameDelivery" / "pending_trade_requests"
        secret_dir.mkdir(parents=True, exist_ok=True)
        _restrict_pending_trade_permissions(secret_dir, directory=True)
        secret_path = secret_dir / (nonce + ".json")

        secret_payload = {
            "CredentialNonce": nonce,
            "OrderId": order,
            "BotName": bot,
            "TargetSteamId64": target_steam_id64,
            "AssetIds": normalized_ids,
            "TradeToken": tokens[0],
        }
        command_payload = {
            "OrderId": order,
            "BotName": bot,
            "TargetSteamId64": target_steam_id64,
            "AssetIds": normalized_ids,
            "CredentialNonce": nonce,
        }
        encoded_payload = base64.urlsafe_b64encode(
            json.dumps(command_payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).decode("ascii").rstrip("=")

        try:
            with secret_path.open("x", encoding="utf-8") as handle:
                _restrict_pending_trade_permissions(secret_path)
                json.dump(secret_payload, handle, ensure_ascii=True, separators=(",", ":"))
            response = self.send_command("gamedelivery-send " + encoded_payload)
            result = _unwrap_result(response)
            if not isinstance(result, str):
                raise ASFError("ASF не вернул структурированный результат отправки.")
            try:
                outcome = json.loads(result)
            except json.JSONDecodeError:
                raise ASFError("ASF вернул некорректный результат отправки.") from None
            if not isinstance(outcome, dict):
                raise ASFError("ASF вернул некорректный результат отправки.")

            if outcome.get("success") is True:
                if (
                    outcome.get("mode") != "sent"
                    or outcome.get("sendEnabled") is not True
                    or outcome.get("orderId") != order
                    or str(outcome.get("botName", "")).casefold() != bot.casefold()
                    or outcome.get("targetSteamId64") != target_steam_id64
                    or outcome.get("assetCount") != len(normalized_ids)
                    or not isinstance(outcome.get("offerIds"), list)
                    or not outcome["offerIds"]
                    or any(not str(value).isdigit() for value in outcome["offerIds"])
                ):
                    raise ASFError("ASF не подтвердил отправку ожидаемого состава заказа.")
                returned_assets = outcome.get("assets")
                if not isinstance(returned_assets, list) or [
                    str(item.get("assetId", "")) for item in returned_assets if isinstance(item, dict)
                ] != normalized_ids:
                    raise ASFError("Состав активов в ответе ASF не совпадает с заказом.")
                return outcome

            # A known offer ID must be tied to this exact order, recipient, bot, and asset list.
            # Older plugin failures may not include details; those remain unknown and are never retried.
            if outcome.get("resultUnknown") is True and "offerIds" in outcome:
                offer_ids = outcome.get("offerIds")
                if (
                    outcome.get("mode") != "send"
                    or outcome.get("sendEnabled") is not True
                    or outcome.get("orderId") != order
                    or str(outcome.get("botName", "")).casefold() != bot.casefold()
                    or outcome.get("targetSteamId64") != target_steam_id64
                    or outcome.get("assetCount") != len(normalized_ids)
                    or not isinstance(offer_ids, list)
                    or any(
                        not str(value).isdigit() or int(value) <= 0
                        for value in offer_ids
                    )
                ):
                    raise ASFError("ASF вернул некорректные данные для неизвестного результата отправки.")
                returned_assets = outcome.get("assets")
                if not isinstance(returned_assets, list) or [
                    str(item.get("assetId", "")) for item in returned_assets if isinstance(item, dict)
                ] != normalized_ids:
                    raise ASFError("Состав активов в неизвестном результате ASF не совпадает с заказом.")

            # Preserve the structured uncertainty flag so the UI can lock this order if needed.
            return outcome
        finally:
            try:
                secret_path.unlink(missing_ok=True)
            except OSError:
                pass

    def restart(self, *, confirmed: bool = False) -> Any:
        """Restart ASF only after explicit user confirmation."""
        if confirmed is not True:
            raise ValueError("Перезапуск ASF требует явного подтверждения")
        return self.request("/Api/ASF/Restart", method="POST", body={})

    def exit(self) -> Any:
        """Request a graceful ASF shutdown through the documented Owner command."""
        response = self.send_command("exit")
        result = _unwrap_result(response)
        if result is False:
            raise ASFError("ASF не подтвердил команду остановки")
        return response

    @staticmethod
    def build_new_bot_config(
        bot_name: str,
        *,
        steam_login: str | None = None,
        enabled: bool = False,
    ) -> dict[str, Any]:
        """Build a minimal safe config payload for a new ASF bot.

        ASF creates bots by loading BotName.json from its config directory;
        IPC BotPost only updates an already-existing bot. This method never
        accepts or stores Steam passwords or session secrets.
        """
        name = bot_name.strip()
        if (
            not name
            or name.casefold() == "asf"
            or name.startswith(".")
            or any(char in name for char in '/\\\\<>:"|?*')
            or any(ord(char) < 32 for char in name)
            or name.endswith(".")
        ):
            raise ValueError("Недопустимое имя бота ASF")
        if steam_login is not None and (
            not steam_login.strip()
            or any(ord(char) < 32 for char in steam_login)
        ):
            raise ValueError("Некорректный SteamLogin")
        config: dict[str, Any] = {"Enabled": bool(enabled)}
        if steam_login is not None:
            config["SteamLogin"] = steam_login.strip()
        return config

    @staticmethod
    def serialize_new_bot_config(bot_name: str, *, steam_login: str | None = None) -> tuple[str, str]:
        """Return (filename, JSON text) for user-reviewed manual ASF import."""
        name = bot_name.strip()
        config = ASFClient.build_new_bot_config(name, steam_login=steam_login, enabled=False)
        return name + ".json", json.dumps(config, ensure_ascii=False, indent=2) + "\n"

