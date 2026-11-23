from __future__ import annotations

import hashlib
import json
from typing import Any

from core.database.db import Database
from core.delivery.recipes import Recipe, RecipeValidationError, legacy_config_to_recipe
from games.asset_ids import validate_asset_id
from games.asset_ids import asset_id_key


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
        delivery_quantity_per_deal: int | None,
        item_selectors: dict[str, Any] | list[Any],
        bot_pool: list[str],
        title: str = "",
        auto_relist: bool = False,
        recipe_items: Any = None,
    ) -> dict[str, Any]:
        listing = listing_id.strip()
        platform = source_platform.strip().lower()
        if not listing or not platform:
            raise ValueError("listing_id и source_platform обязательны")
        recipe = Recipe.from_values(recipe_items) if recipe_items is not None else None
        if delivery_quantity_per_deal is None:
            if recipe is None:
                raise ValueError("Без Recipe delivery_quantity_per_deal обязателен")
        elif isinstance(delivery_quantity_per_deal, bool) or not isinstance(delivery_quantity_per_deal, int) or delivery_quantity_per_deal < 1:
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
            stored_recipe = [
                {**item.to_dict(), "item_id": f"{config_id}:item:{item.item_id}"}
                for item in recipe.items
            ] if recipe is not None else []
            if recipe is not None:
                # Validate the final, version-qualified identities with the
                # same contract used when ConfigVersion is later read.
                validated = Recipe.from_values(stored_recipe)
                stored_recipe = validated.to_list()
                stored_ids = [item["item_id"] for item in stored_recipe]
                if len(stored_ids) != len(set(stored_ids)):
                    raise RecipeValidationError("Префиксованные RecipeItem.item_id повторяются")
            connection.execute(
                """
                INSERT INTO gd_config_versions
                    (config_version_id, listing_id, version_number,
                     delivery_quantity_per_deal, item_selectors_json, bot_pool_json,
                     recipe_schema_version,recipe_items_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (config_id, listing, version_number, delivery_quantity_per_deal,
                 _json(item_selectors), _json(normalized_pool),
                 1 if recipe is not None else None, _json(stored_recipe)),
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
            "recipe_items": stored_recipe,
        }

    def create_recipe_config_version(
        self,
        *,
        listing_id: str,
        source_platform: str,
        external_listing_id: str | None,
        recipe_items: Any,
        title: str = "",
        auto_relist: bool = False,
    ) -> dict[str, Any]:
        """Persist a typed recipe without enabling a legacy executor for it."""
        recipe = Recipe.from_values(recipe_items)
        return self.create_listing_config_version(
            listing_id=listing_id,
            source_platform=source_platform,
            external_listing_id=external_listing_id,
            # Null keeps legacy scalar-based order creation fail-closed until
            # typed recipes are explicitly connected to the order pipeline.
            delivery_quantity_per_deal=None,
            item_selectors={"recipe_schema_version": 1},
            bot_pool=[],
            title=title,
            auto_relist=auto_relist,
            recipe_items=recipe,
        )

    @staticmethod
    def recipe_for_config_version(
        connection, config_version_id: str, *, allow_legacy: bool = True
    ) -> Recipe:
        row = connection.execute(
            "SELECT recipe_schema_version,recipe_items_json,delivery_quantity_per_deal,"
            "item_selectors_json,bot_pool_json FROM gd_config_versions WHERE config_version_id=?",
            (config_version_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"ConfigVersion {config_version_id} не найден")
        if row["recipe_schema_version"] is not None:
            if row["recipe_schema_version"] != 1:
                raise ValueError("Версия схемы Recipe не поддерживается")
            try:
                raw_items = json.loads(row["recipe_items_json"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Recipe ConfigVersion содержит некорректный JSON") from exc
            if not isinstance(raw_items, list):
                raise ValueError("Recipe ConfigVersion должен содержать список RecipeItem")
            return Recipe.from_values(raw_items)
        if not allow_legacy:
            raise KeyError(f"ConfigVersion {config_version_id} не содержит типизированный рецепт")
        return legacy_config_to_recipe(dict(row))

    def materialize_recipe_order_lines(
        self, order_id: str, *, allow_legacy: bool = True
    ) -> list[dict[str, Any]]:
        """Create a version-pinned set of OrderLines once; does not reserve or send."""
        order = str(order_id or "").strip()
        if not order:
            raise ValueError("order_id обязателен")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT o.order_id,d.config_version_id FROM gd_orders o "
                "JOIN gd_deals d ON d.deal_id=o.deal_id WHERE o.order_id=?",
                (order,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Заказ {order} не найден")
            config_version_id = str(row["config_version_id"] or "")
            if not config_version_id:
                raise ValueError("Заказ не закреплён за ConfigVersion")
            return self.materialize_recipe_order_lines_in_transaction(
                connection, order, config_version_id, allow_legacy=allow_legacy
            )

    @classmethod
    def materialize_recipe_order_lines_in_transaction(
        cls, connection, order_id: str, config_version_id: str, *, allow_legacy: bool = True
    ) -> list[dict[str, Any]]:
        """Expand the exact ConfigVersion using the caller's active transaction."""
        if not connection.in_transaction:
            raise RuntimeError("OrderLine materialization requires an active transaction")
        order = str(order_id or "").strip()
        config_id = str(config_version_id or "").strip()
        if not order or not config_id:
            raise ValueError("order_id и config_version_id обязательны")
        ownership = connection.execute(
            "SELECT d.config_version_id,d.listing_id,c.listing_id AS config_listing_id "
            "FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id "
            "JOIN gd_config_versions c ON c.config_version_id=? WHERE o.order_id=?",
            (config_id, order),
        ).fetchone()
        if ownership is None:
            raise KeyError(f"Заказ {order} не найден или ConfigVersion {config_id} не существует")
        if ownership["config_version_id"] != config_id:
            raise ValueError("ConfigVersion не принадлежит сделке заказа")
        if ownership["listing_id"] != ownership["config_listing_id"]:
            raise ValueError("ConfigVersion не принадлежит объявлению сделки заказа")
        recipe = cls.recipe_for_config_version(connection, config_id, allow_legacy=allow_legacy)
        expected = []
        for item in recipe.items:
            line_id = cls._stable_recipe_line_id(order, config_id, item.item_id)
            selector_snapshot = {
                "config_version_id": config_id,
                "recipe_item_id": item.item_id,
                "recipe_item": item.to_dict(),
            }
            expected.append((item, line_id, _json(selector_snapshot)))
        existing = connection.execute(
            "SELECT * FROM gd_order_lines WHERE order_id=? ORDER BY line_number", (order,)
        ).fetchall()
        if existing:
            existing_by_id = {str(line["order_line_id"]): line for line in existing}
            if len(existing_by_id) != len(expected) or any(
                line_id not in existing_by_id
                or existing_by_id[line_id]["line_number"] != item.position
                or existing_by_id[line_id]["item_type"] != item.kind
                or existing_by_id[line_id]["quantity"] != item.quantity_per_order
                or existing_by_id[line_id]["item_selector_json"] != selector_json
                for item, line_id, selector_json in expected
            ):
                raise ValueError("В заказе уже есть OrderLine, не совпадающие с закреплённым рецептом")
            return [dict(existing_by_id[line_id]) for _, line_id, _ in expected]
        for item, line_id, selector_json in expected:
            connection.execute(
                "INSERT INTO gd_order_lines "
                "(order_line_id,order_id,config_version_id,line_number,item_type,item_name,item_selector_json,quantity) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (line_id, order, config_id, item.position, item.kind, item.display_name,
                 selector_json, item.quantity_per_order),
            )
        return [dict(row) for row in connection.execute(
            "SELECT * FROM gd_order_lines WHERE order_id=? ORDER BY line_number", (order,)
        ).fetchall()]

    @staticmethod
    def _stable_recipe_line_id(order_id: str, config_version_id: str, item_id: str) -> str:
        digest = hashlib.sha256(
            f"{order_id}\x1f{config_version_id}\x1f{item_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"recipe-line:{digest}"

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
            owner = connection.execute(
                "SELECT d.config_version_id FROM gd_orders o "
                "JOIN gd_deals d ON d.deal_id=o.deal_id WHERE o.order_id=?",
                (order_id.strip(),),
            ).fetchone()
            if owner is None or not owner["config_version_id"]:
                raise ValueError("OrderLine требует заказа с ConfigVersion сделки")
            connection.execute(
                """
                INSERT INTO gd_order_lines
                    (order_line_id, order_id, config_version_id, line_number, item_type, item_name,
                     item_selector_json, quantity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order_line_id.strip(), order_id.strip(), owner["config_version_id"], line_number,
                 item_type.strip(), item_name.strip(), _json(item_selector), quantity),
            )
            row = connection.execute(
                "SELECT * FROM gd_order_lines WHERE order_line_id=?", (order_line_id.strip(),)
            ).fetchone()
        return dict(row)

    def bind_order_recipient(self, order_id: str, steam_id64: int | str) -> dict[str, str]:
        """Bind one immutable Steam recipient after all required legs are reserved."""
        order = str(order_id or "").strip()
        if not order or isinstance(steam_id64, bool):
            raise ValueError("order_id и корректный SteamID64 обязательны")
        try:
            recipient = int(steam_id64)
        except (TypeError, ValueError):
            raise ValueError("SteamID64 получателя имеет некорректный формат") from None
        if not 76561197960265728 < recipient <= 76561197964560623:
            raise ValueError("SteamID64 получателя находится вне диапазона индивидуальных Steam-аккаунтов")
        normalized = str(recipient)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT o.status,o.recipient_steam_id64,
                       COUNT(l.leg_id) AS leg_count,
                       SUM(CASE WHEN l.status='reserved' THEN 1 ELSE 0 END) AS reserved_count
                FROM gd_orders o
                LEFT JOIN gd_order_lines ol ON ol.order_id=o.order_id
                LEFT JOIN gd_delivery_legs l ON l.order_line_id=ol.order_line_id
                WHERE o.order_id=?
                GROUP BY o.order_id
                """,
                (order,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Канонический заказ {order} не найден")
            if row["status"] != "reserved":
                raise ValueError("Получателя можно закрепить только для зарезервированного заказа")
            leg_count = int(row["leg_count"] or 0)
            reserved_count = int(row["reserved_count"] or 0)
            if leg_count < 1 or reserved_count != leg_count:
                raise ValueError("Нельзя закрепить получателя: не все обязательные DeliveryLeg зарезервированы")
            existing = row["recipient_steam_id64"]
            if existing is not None and str(existing) != normalized:
                raise ValueError("Получатель уже закреплён за другим SteamID64; автоматическая перепривязка запрещена")
            if existing is None:
                changed = connection.execute(
                    "UPDATE gd_orders SET recipient_steam_id64=? "
                    "WHERE order_id=? AND recipient_steam_id64 IS NULL",
                    (normalized, order),
                ).rowcount
                if changed != 1:
                    raise ValueError("Получатель изменился параллельно; повтори ASF preview")
        return {"order_id": order, "recipient_steam_id64": normalized}

    def add_delivery_leg(
        self, *, leg_id: str, order_line_id: str, bot_name: str,
        app_id: int, context_id: str | int, asset_ids: list[str],
        status: str = "planned", trade_offer_id: str | None = None,
        asset_amounts: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        if not all((leg_id.strip(), order_line_id.strip(), bot_name.strip())):
            raise ValueError("leg_id, order_line_id и bot_name обязательны")
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise ValueError("app_id должен быть положительным целым числом")
        assets = [validate_asset_id(value) for value in asset_ids]
        if not assets or len({asset_id_key(value) for value in assets}) != len(assets):
            raise ValueError("asset_ids должен быть непустым списком без повторов")
        normalized_amounts = None
        if asset_amounts is not None:
            if not isinstance(asset_amounts, dict) or set(asset_amounts) != set(assets):
                raise ValueError("asset_amounts должен содержать ровно один Amount для каждого Asset ID")
            normalized_amounts = {}
            for asset_id in assets:
                amount = asset_amounts[asset_id]
                if type(amount) is not int or not 1 <= amount <= 0xFFFFFFFF:
                    raise ValueError("Amount для каждого Asset ID должен быть целым числом от 1 до 4294967295")
                normalized_amounts[asset_id] = amount
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO gd_delivery_legs
                    (leg_id, order_line_id, bot_name, app_id, context_id,
                     asset_ids_json, asset_amounts_json, trade_offer_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (leg_id.strip(), order_line_id.strip(), bot_name.strip(), app_id,
                 str(context_id), _json(assets),
                 _json(normalized_amounts) if normalized_amounts is not None else None,
                 trade_offer_id, status.strip() or "planned"),
            )
            row = connection.execute("SELECT * FROM gd_delivery_legs WHERE leg_id=?", (leg_id.strip(),)).fetchone()
        return dict(row)

    def create_delivery_attempt(
        self, *, order_id: str, leg_id: str, retry_generation: int,
        status: str = "prepared", result: dict[str, Any] | None = None,
        connection=None,
    ) -> tuple[dict[str, Any], bool]:
        order, leg = order_id.strip(), leg_id.strip()
        if not order or not leg:
            raise ValueError("order_id и leg_id обязательны")
        if isinstance(retry_generation, bool) or not isinstance(retry_generation, int) or retry_generation < 0:
            raise ValueError("retry_generation должен быть целым числом не меньше нуля")
        key = f"{order}:{leg}:{retry_generation}"
        attempt_id = f"attempt:{key}"
        def insert(connection):
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
            return row, cursor.rowcount == 1

        if connection is None:
            with self.database.connect() as connection:
                row, created = insert(connection)
        else:
            row, created = insert(connection)
        if row is None:
            raise RuntimeError("Не удалось прочитать DeliveryAttempt после записи")
        return dict(row), created
