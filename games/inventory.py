from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from integrations.asf.client import ASFError


@dataclass(frozen=True)
class InventoryItem:
    game_id: str
    bot_name: str
    asset_id: str
    class_id: str
    instance_id: str
    name: str
    item_type: str = ""
    market_hash_name: str = ""
    icon_url: str = ""
    icon_url_large: str = ""
    is_tradable: bool | None = None
    is_marketable: bool | None = None
    quality: str = ""
    rarity: str = ""
    exterior: str = ""
    tags: tuple[str, ...] = ()


def _find_key(data: dict[str, Any], wanted: str) -> str | None:
    target = wanted.casefold()
    return next((key for key in data if str(key).casefold() == target), None)


def _flag(data: dict[str, Any], field: str, asset_id: str) -> bool | None:
    key = _find_key(data, field)
    if key is None:
        return None
    value = data[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().casefold() in {"true", "false", "1", "0"}:
        return value.strip().casefold() in {"true", "1"}
    raise ASFError(f"Некорректный флаг {field} у предмета {asset_id}")


def parse_game_inventory(payload: Any, game_id: str, bot_name: str) -> list[InventoryItem]:
    """Parse a complete ASF BotInventoryResponse without assuming card semantics."""
    from games.catalog import get_game

    game = get_game(game_id)
    bot = str(bot_name).strip()
    if not bot:
        raise ValueError("Имя бота не должно быть пустым")
    if not isinstance(payload, dict):
        raise ASFError("ASF вернул ответ инвентаря неожиданного формата")
    success_key = _find_key(payload, "Success")
    if success_key is not None and payload[success_key] is False:
        raise ASFError("ASF не смог получить инвентарь")
    result_key = _find_key(payload, "Result")
    result = payload[result_key] if result_key else payload
    if not isinstance(result, dict):
        raise ASFError("ASF не вернул список инвентаря по ботам")
    bot_key = _find_key(result, bot)
    if bot_key is None or not isinstance(result[bot_key], dict):
        raise ASFError("ASF не вернул инвентарь запрошенного бота")
    inventory = result[bot_key]
    assets_key, descriptions_key = _find_key(inventory, "Assets"), _find_key(inventory, "Descriptions")
    if assets_key is None or descriptions_key is None:
        raise ASFError("ASF не подтвердил полный ответ инвентаря; снимок не обновлён")
    assets, descriptions = inventory[assets_key], inventory[descriptions_key]
    if not isinstance(assets, list) or not isinstance(descriptions, list):
        raise ASFError("ASF вернул некорректные Assets/Descriptions")
    descriptions_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for description in descriptions:
        if not isinstance(description, dict):
            continue
        class_key = _find_key(description, "classid")
        if class_key is None:
            continue
        instance_key = _find_key(description, "instanceid")
        identity = (str(description[class_key]), str(description[instance_key]) if instance_key else "0")
        descriptions_by_key[identity] = description

    items: list[InventoryItem] = []
    seen: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ASFError("ASF вернул некорректный предмет; снимок не обновлён")
        asset_key, class_key = _find_key(asset, "assetid"), _find_key(asset, "classid")
        if asset_key is None or class_key is None:
            raise ASFError("Предмет ASF не содержит asset ID или class ID; снимок не обновлён")
        asset_id = str(asset[asset_key]).strip()
        instance_key = _find_key(asset, "instanceid")
        if not asset_id or asset_id in seen:
            raise ASFError("ASF вернул пустой или повторяющийся asset ID; снимок не обновлён")
        seen.add(asset_id)
        class_id = str(asset[class_key])
        instance_id = str(asset[instance_key]) if instance_key else "0"
        description = descriptions_by_key.get((class_id, instance_id))
        if description is None:
            raise ASFError(f"Для предмета {asset_id} не найдено описание; старый снимок сохранён")

        def value(field: str) -> str:
            key = _find_key(description, field)
            return str(description.get(key, "") or "").strip() if key else ""

        def image(field: str) -> str:
            raw = value(field)
            if not raw:
                return ""
            if raw.startswith("//"):
                return "https:" + raw
            if raw.startswith(("https://", "http://")):
                return raw
            if raw.startswith("/economy/image/"):
                return "https://community.cloudflare.steamstatic.com" + raw
            return "https://community.cloudflare.steamstatic.com/economy/image/" + raw.lstrip("/")

        tags_key = _find_key(description, "tags")
        raw_tags = description.get(tags_key, []) if tags_key else []
        tag_names: list[str] = []
        quality = rarity = exterior = ""
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                if not isinstance(tag, dict):
                    continue
                localized_key = _find_key(tag, "localized_tag_name")
                category_key = _find_key(tag, "category")
                internal_key = _find_key(tag, "internal_name")
                localized = str(tag.get(localized_key, "") if localized_key else "").strip()
                category = str(tag.get(category_key, "") if category_key else "").casefold()
                internal = str(tag.get(internal_key, "") if internal_key else "").strip()
                if localized:
                    tag_names.append(localized)
                if category in {"quality", "rarity"} and not quality:
                    quality = localized or internal
                elif category == "exterior" and not exterior:
                    exterior = localized or internal
                if category == "rarity" and not rarity:
                    rarity = localized or internal

        items.append(InventoryItem(
            game_id=game.game_id, bot_name=bot, asset_id=asset_id,
            class_id=class_id, instance_id=instance_id,
            name=value("name") or f"Unknown item {asset_id}",
            item_type=value("type"), market_hash_name=value("market_hash_name"),
            icon_url=image("icon_url"), icon_url_large=image("icon_url_large"),
            is_tradable=_flag(description, "tradable", asset_id),
            is_marketable=_flag(description, "marketable", asset_id),
            quality=quality, rarity=rarity, exterior=exterior, tags=tuple(tag_names),
        ))
    return items


class GameInventoryStore:
    """Separate per-game snapshots; does not migrate or modify Steam-card tables."""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS game_inventory_snapshot (
                    game_id TEXT NOT NULL,
                    bot_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    class_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    item_type TEXT NOT NULL DEFAULT '',
                    market_hash_name TEXT NOT NULL DEFAULT '',
                    icon_url TEXT NOT NULL DEFAULT '',
                    icon_url_large TEXT NOT NULL DEFAULT '',
                    is_tradable INTEGER,
                    is_marketable INTEGER,
                    quality TEXT NOT NULL DEFAULT '',
                    rarity TEXT NOT NULL DEFAULT '',
                    exterior TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (game_id, bot_name, asset_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS game_inventory_snapshot_meta (
                    game_id TEXT NOT NULL,
                    bot_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    PRIMARY KEY (game_id, bot_name)
                )
            """)

    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=10)
        db.execute("PRAGMA busy_timeout = 10000")
        return db

    def replace(self, game_id: str, bot_name: str, items: list[InventoryItem]) -> dict[str, object]:
        import json
        from games.catalog import get_game
        game = get_game(game_id)
        bot = str(bot_name).strip()
        if not bot:
            raise ValueError("Имя бота не должно быть пустым")
        if any(item.game_id != game.game_id or item.bot_name != bot for item in items):
            raise ValueError("Снимок содержит предметы другой игры или бота")
        if len({item.asset_id for item in items}) != len(items):
            raise ValueError("Снимок содержит повторяющиеся asset ID")
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM game_inventory_snapshot WHERE game_id = ? AND bot_name = ?", (game.game_id, bot))
            db.executemany("""
                INSERT INTO game_inventory_snapshot
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [(
                item.game_id, item.bot_name, item.asset_id, item.class_id, item.instance_id,
                item.name, item.item_type, item.market_hash_name, item.icon_url, item.icon_url_large,
                None if item.is_tradable is None else int(item.is_tradable),
                None if item.is_marketable is None else int(item.is_marketable),
                item.quality, item.rarity, item.exterior, json.dumps(item.tags, ensure_ascii=False),
            ) for item in items])
            db.execute("""
                INSERT INTO game_inventory_snapshot_meta (game_id, bot_name, updated_at, item_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(game_id, bot_name) DO UPDATE SET
                    updated_at=excluded.updated_at, item_count=excluded.item_count
            """, (game.game_id, bot, timestamp, len(items)))
        return {"game_id": game.game_id, "bot_name": bot, "updated_at": timestamp, "item_count": len(items)}

    def load(self, game_id: str, bot_name: str) -> list[InventoryItem]:
        import json
        from games.catalog import get_game
        game = get_game(game_id)
        with self._connect() as db:
            rows = db.execute("""
                SELECT game_id, bot_name, asset_id, class_id, instance_id, name, item_type,
                       market_hash_name, icon_url, icon_url_large, is_tradable, is_marketable,
                       quality, rarity, exterior, tags_json
                FROM game_inventory_snapshot WHERE game_id=? AND bot_name=? ORDER BY name, asset_id
            """, (game.game_id, bot_name.strip())).fetchall()
        return [InventoryItem(
            game_id=r[0], bot_name=r[1], asset_id=r[2], class_id=r[3], instance_id=r[4],
            name=r[5], item_type=r[6], market_hash_name=r[7], icon_url=r[8], icon_url_large=r[9],
            is_tradable=None if r[10] is None else bool(r[10]),
            is_marketable=None if r[11] is None else bool(r[11]),
            quality=r[12], rarity=r[13], exterior=r[14], tags=tuple(json.loads(r[15])),
        ) for r in rows]


def refresh_game_inventory(client, store: GameInventoryStore, game_id: str, bot_name: str):
    from games.catalog import get_game
    game = get_game(game_id)
    payload = client.get_bot_inventory(
        bot_name, app_id=game.app_id, context_id=game.context_id, language="english"
    )
    items = parse_game_inventory(payload, game.game_id, bot_name)
    return store.replace(game.game_id, bot_name, items)
