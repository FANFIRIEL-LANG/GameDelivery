"""One-item, read-only probe for an isolated ASF Generic CS2 inventory."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from typing import Any
from urllib.parse import urlsplit

from integrations.asf.client import ASFClient, ASFError


def _key(data: dict[str, Any], name: str) -> str | None:
    wanted = name.casefold()
    return next((key for key in data if isinstance(key, str) and key.casefold() == wanted), None)


def _validate_test_ipc_url(ipc_url: str) -> None:
    try:
        parsed = urlsplit(ipc_url)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.port == 1243
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except (AttributeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Probe принимает только изолированный loopback ASF IPC URL на порту 1243")


def _unwrap_inventory(payload: Any, bot_name: str) -> tuple[list[Any], list[Any]]:
    if not isinstance(payload, dict):
        raise ASFError("ASF вернул ответ инвентаря неожиданного формата")
    success_key = _key(payload, "Success")
    if success_key is not None and payload[success_key] is False:
        raise ASFError("ASF не смог получить инвентарь CS2")
    result_key = _key(payload, "Result")
    result = payload[result_key] if result_key else payload
    if not isinstance(result, dict):
        raise ASFError("ASF не вернул объект инвентаря бота")
    bot_key = _key(result, bot_name)
    if bot_key is None or not isinstance(result[bot_key], dict):
        raise ASFError("ASF не вернул инвентарь запрошенного бота")

    inventory = result[bot_key]
    assets_key = _key(inventory, "Assets")
    if assets_key is None or not isinstance(inventory[assets_key], list):
        raise ASFError("ASF не вернул список CS2 Assets")
    descriptions_key = _key(inventory, "Descriptions")
    descriptions = inventory[descriptions_key] if descriptions_key else []
    if not isinstance(descriptions, list):
        raise ASFError("ASF вернул некорректный список Descriptions")
    return inventory[assets_key], descriptions


def _raw_value(sources: tuple[dict[str, Any] | None, ...], field: str) -> Any:
    for source in sources:
        if source is None:
            continue
        key = _key(source, field)
        if key is not None and source[key] is not None and source[key] != "":
            return source[key]
    return None


def _text_value(sources: tuple[dict[str, Any] | None, ...], field: str) -> str:
    value = _raw_value(sources, field)
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or "unknown"
    return "unknown"


def _flag_value(sources: tuple[dict[str, Any] | None, ...], field: str) -> bool | str:
    value = _raw_value(sources, field)
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().casefold() in {"0", "1", "false", "true"}:
        return value.strip().casefold() in {"1", "true"}
    raise ASFError(f"ASF вернул некорректное поле {field} для выбранного предмета")


def _find_asset(assets: list[Any], asset_id: str) -> dict[str, Any]:
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and (asset_key := _key(asset, "assetid")) is not None
        and str(asset[asset_key]).strip() == asset_id
    ]
    if not matches:
        raise ASFError(f"Предмет с assetID {asset_id} не найден в CS2-инвентаре")
    if len(matches) != 1:
        raise ASFError(f"ASF вернул несколько записей с assetID {asset_id}")
    return matches[0]


def _find_description(asset: dict[str, Any], descriptions: list[Any]) -> dict[str, Any] | None:
    class_key = _key(asset, "classid")
    if class_key is None:
        return None
    instance_key = _key(asset, "instanceid")
    identity = (
        str(asset[class_key]),
        str(asset[instance_key]) if instance_key is not None else "0",
    )
    for description in descriptions:
        if not isinstance(description, dict):
            continue
        described_class_key = _key(description, "classid")
        if described_class_key is None:
            continue
        described_instance_key = _key(description, "instanceid")
        described_identity = (
            str(description[described_class_key]),
            str(description[described_instance_key])
            if described_instance_key is not None
            else "0",
        )
        if described_identity == identity:
            return description
    return None


def probe_cs2_inventory_item(
    *,
    ipc_url: str,
    bot_name: str,
    asset_id: str,
    ipc_password: str | None = None,
) -> dict[str, Any]:
    """Fetch and return one exact CS2 asset without opening storage or delivery code."""
    _validate_test_ipc_url(ipc_url)
    bot = bot_name.strip() if isinstance(bot_name, str) else ""
    asset = asset_id.strip() if isinstance(asset_id, str) else ""
    if not bot or any(ord(char) < 32 for char in bot):
        raise ValueError("Укажи корректное имя тестового ASF-бота")
    if re.fullmatch(r"[1-9][0-9]*", asset) is None:
        raise ValueError("assetID должен быть точным положительным числом из Steam")

    client = ASFClient(
        base_url=ipc_url,
        ipc_password=ipc_password,
        allow_alternate_loopback_port=True,
    )
    payload = client.get_bot_inventory(bot, app_id=730, context_id=2, language="english")
    assets, descriptions = _unwrap_inventory(payload, bot)
    selected = _find_asset(assets, asset)
    description = _find_description(selected, descriptions)
    asset_source = (selected,)
    item_sources = (description, selected)

    return {
        "asset_id": asset,
        "class_id": _text_value(asset_source, "classid"),
        "instance_id": _text_value(asset_source, "instanceid"),
        "amount": _text_value(asset_source, "amount"),
        "name": _text_value((description,), "name"),
        "type": _text_value((description,), "type"),
        "market_hash_name": _text_value((description,), "market_hash_name"),
        "tradable": _flag_value(item_sources, "tradable"),
        "marketable": _flag_value(item_sources, "marketable"),
        "tradable_after": _text_value(item_sources, "tradable_after"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Прочитать один CS2-предмет из изолированного ASF Generic")
    parser.add_argument("--ipc-url", required=True, help="Изолированный loopback IPC URL на порту 1243")
    parser.add_argument("--bot-name", required=True, help="BotName тестового ASF-бота")
    parser.add_argument("--asset-id", required=True, help="Точный Steam assetID")
    args = parser.parse_args(argv)

    try:
        _validate_test_ipc_url(args.ipc_url)
        password = getpass.getpass("IPCPassword тестового ASF (Enter, если не задан): ")
        item = probe_cs2_inventory_item(
            ipc_url=args.ipc_url,
            bot_name=args.bot_name,
            asset_id=args.asset_id,
            ipc_password=password or None,
        )
    except ASFError as exc:
        print(f"Ошибка ASF: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Ошибка параметров: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(item, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
