from __future__ import annotations

import json
from typing import Any

from core.database.db import Database


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DeliveryLedger:
    """Canonical Listing → Deal → Order → OrderLine → DeliveryLeg ledger.

    This portion creates immutable configuration snapshots and idempotent
    attempt records. Inventory selection and multi-bot reservation are deferred.
    """

    def __init__(self, database: Database):
        self.database = database
        self.database.initialize()

    def create_listing_config_version(
        self,
        *,
        listing_id: str,
        source_platform: str,
        external_listing_id: str | None,
        delivery_quantity_per_deal: int,
        item_selectors: dict[str, Any] | list[Any],
        bot_pool: list[str],
        title: str = "",
        auto_relist: bool = False,
    ) -> dict[str, Any]:
        listing = listing_id.strip()
        platform = source_platform.strip().lower()
        if not listing or not platform:
            raise ValueError("listing_id и source_platform обязательны")
        if isinstance(delivery_quantity_per_deal, bool) or not isinstance(delivery_quantity_per_deal, int) or delivery_quantity_per_deal < 1:
            raise ValueError("delivery_quantity_per_deal должен быть положительным целым числом")
        if not isinstance(item_selectors, (dict, list)):
            raise ValueError("item_selectors должен быть объектом или списком")
        if not isinstance(bot_pool, list) or any(not isinstance(bot, str) or not bot.strip() for bot in bot_pool):
            raise ValueError("bot_pool должен содержать непустые имена ботов")

        normalized_pool: list[str] = []
        seen: set[str] = set()
        for raw_bot in bot_pool:
            bot = raw_bot.strip()
            if bot.casefold() not in seen:
                seen.add(bot.casefold())
                normalized_pool.append(bot)

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO gd_listings
                    (listing_id, source_platform, external_listing_id, title, status, auto_relist)
                VALUES (?, ?, ?, ?, 'active', ?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    title = excluded.title,
                    auto_relist = excluded.auto_relist,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (listing, platform, external_listing_id, title, int(bool(auto_relist))),
            )
            listing_row = connection.execute(
                "SELECT source_platform, external_listing_id FROM gd_listings WHERE listing_id=?",
                (listing,),
            ).fetchone()
            if listing_row is None or listing_row["source_platform"] != platform or listing_row["external_listing_id"] != external_listing_id:
                raise ValueError("listing_id уже связан с другим source listing")
            version_number = int(connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM gd_config_versions WHERE listing_id=?",
                (listing,),
            ).fetchone()[0])
            config_id = f"{listing}:config:{version_number}"
            connection.execute(
                """
                INSERT INTO gd_config_versions
                    (config_version_id, listing_id, version_number,
                     delivery_quantity_per_deal, item_selectors_json, bot_pool_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (config_id, listing, version_number, delivery_quantity_per_deal,
                 _json(item_selectors), _json(normalized_pool)),
            )
            row = connection.execute(
                "SELECT * FROM gd_config_versions WHERE config_version_id=?", (config_id,)
            ).fetchone()
        return {
            "config_version_id": row["config_version_id"],
            "listing_id": row["listing_id"],
            "version_number": row["version_number"],
            "delivery_quantity_per_deal": row["delivery_quantity_per_deal"],
            "item_selectors": json.loads(row["item_selectors_json"]),
            "bot_pool": json.loads(row["bot_pool_json"]),
        }

    def create_deal(
        self, *, deal_id: str, source_platform: str, external_deal_id: str,
        chat_id: str | None, listing_id: str, config_version_id: str,
        platform_status: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = (
            deal_id.strip(), source_platform.strip().lower(), external_deal_id.strip(),
            chat_id.strip() if isinstance(chat_id, str) and chat_id.strip() else None,
            listing_id.strip(), config_version_id.strip(),
            platform_status.strip() or "unknown", _json(payload or {}),
        )
        if not all((values[0], values[1], values[2], values[4], values[5])):
            raise ValueError("deal_id, source, external_deal_id, listing_id и config_version_id обязательны")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO gd_deals
                    (deal_id, source_platform, external_deal_id, chat_id,
                     listing_id, config_version_id, platform_status, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, values,
            )
            row = connection.execute("SELECT * FROM gd_deals WHERE deal_id=?", (values[0],)).fetchone()
        return dict(row)

    def create_order(self, *, order_id: str, deal_id: str, total_quantity: int, status: str = "pending") -> dict[str, Any]:
        if not order_id.strip() or not deal_id.strip():
            raise ValueError("order_id и deal_id обязательны")
        if isinstance(total_quantity, bool) or not isinstance(total_quantity, int) or total_quantity < 1:
            raise ValueError("total_quantity должен быть положительным целым числом")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO gd_orders (order_id, deal_id, status, total_quantity) VALUES (?, ?, ?, ?)",
                (order_id.strip(), deal_id.strip(), status.strip() or "pending", total_quantity),
            )
            row = connection.execute("SELECT * FROM gd_orders WHERE order_id=?", (order_id.strip(),)).fetchone()
        return dict(row)

    def add_order_line(
        self, *, order_line_id: str, order_id: str, line_number: int,
        item_type: str, item_name: str, item_selector: dict[str, Any] | list[Any], quantity: int,
    ) -> dict[str, Any]:
        if not all((order_line_id.strip(), order_id.strip(), item_type.strip())):
            raise ValueError("order_line_id, order_id и item_type обязательны")
        if isinstance(line_number, bool) or not isinstance(line_number, int) or line_number < 1:
            raise ValueError("line_number должен быть положительным целым числом")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("quantity должен быть положительным целым числом")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO gd_order_lines
                    (order_line_id, order_id, line_number, item_type, item_name,
                     item_selector_json, quantity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (order_line_id.strip(), order_id.strip(), line_number,
                 item_type.strip(), item_name.strip(), _json(item_selector), quantity),
            )
            row = connection.execute(
                "SELECT * FROM gd_order_lines WHERE order_line_id=?", (order_line_id.strip(),)
            ).fetchone()
        return dict(row)

    def add_delivery_leg(
        self, *, leg_id: str, order_line_id: str, bot_name: str,
        app_id: int, context_id: str | int, asset_ids: list[str],
        status: str = "planned", trade_offer_id: str | None = None,
    ) -> dict[str, Any]:
        if not all((leg_id.strip(), order_line_id.strip(), bot_name.strip())):
            raise ValueError("leg_id, order_line_id и bot_name обязательны")
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise ValueError("app_id должен быть положительным целым числом")
        assets = [str(value).strip() for value in asset_ids]
        if not assets or any(not value for value in assets) or len(set(assets)) != len(assets):
            raise ValueError("asset_ids должен быть непустым списком без повторов")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO gd_delivery_legs
                    (leg_id, order_line_id, bot_name, app_id, context_id,
                     asset_ids_json, trade_offer_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (leg_id.strip(), order_line_id.strip(), bot_name.strip(), app_id,
                 str(context_id), _json(assets), trade_offer_id, status.strip() or "planned"),
            )
            row = connection.execute("SELECT * FROM gd_delivery_legs WHERE leg_id=?", (leg_id.strip(),)).fetchone()
        return dict(row)

    def create_delivery_attempt(
        self, *, order_id: str, leg_id: str, retry_generation: int,
        status: str = "prepared", result: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        order, leg = order_id.strip(), leg_id.strip()
        if not order or not leg:
            raise ValueError("order_id и leg_id обязательны")
        if isinstance(retry_generation, bool) or not isinstance(retry_generation, int) or retry_generation < 0:
            raise ValueError("retry_generation должен быть целым числом не меньше нуля")
        key = f"{order}:{leg}:{retry_generation}"
        attempt_id = f"attempt:{key}"
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO gd_delivery_attempts
                    (attempt_id, order_id, leg_id, retry_generation, idempotency_key, status, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (attempt_id, order, leg, retry_generation, key, status.strip() or "prepared", _json(result or {})),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM gd_delivery_attempts WHERE idempotency_key=?", (key,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Не удалось прочитать DeliveryAttempt после записи")
        return dict(row), created
