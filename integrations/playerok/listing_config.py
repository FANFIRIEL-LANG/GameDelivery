from __future__ import annotations

import hashlib
import json
from typing import Any

from core.delivery.ledger import DeliveryLedger
from core.delivery.listing_stock import ListingStockService
from core.delivery.recipes import (
    Recipe,
    RecipeValidationError,
    SteamAssetSpec,
    UnsupportedLegacyRecipeError,
    legacy_config_to_recipe,
    normalize_legacy_cs2_selector_options,
)


class PlayerokListingConfigService:
    """Create immutable Listing -> ConfigVersion snapshots for Playerok."""

    MODES = {"steam_cards", "cs2_items", "manual"}

    def __init__(self, database: Any):
        self.database = database
        self.ledger = DeliveryLedger(database)

    @staticmethod
    def _listing_key(external_id: str) -> str:
        digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:32]
        return "playerok-listing:" + digest

    def save(
        self, *, external_listing_id: str, title: str, url: str, mode: str,
        quantity: int = 1, bot_pool: list[str] | None = None,
        selectors: dict[str, Any] | list[Any] | None = None,
        auto_messages: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        external_id = str(external_listing_id).strip()
        mode = str(mode).strip().lower()
        if not external_id:
            raise ValueError("Укажи listing_id Playerok.")
        if mode not in self.MODES:
            raise ValueError("Неизвестный режим выдачи.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("Количество за покупку должно быть положительным целым числом.")
        bots = bot_pool or []
        if not isinstance(bots, list) or any(not isinstance(x, str) or not x.strip() for x in bots):
            raise ValueError("Список разрешённых ботов задан неверно.")
        selector_data: dict[str, Any]
        if mode == "steam_cards":
            selector_data = {"delivery_mode": mode, "app_id": 753, "context_id": 6,
                             "selection": "buyer_badges_and_complete_sets"}
        elif mode == "cs2_items":
            if selectors is None:
                raise ValueError("Для CS2 укажи хотя бы один селектор предмета.")
            raw_options = [selectors] if isinstance(selectors, dict) else selectors
            if not isinstance(raw_options, list) or not raw_options:
                raise ValueError("Для CS2 укажи непустой список selector-объектов.")
            legacy_wrapper = {"delivery_mode": mode, "app_id": 730, "context_id": 2,
                              "items": raw_options}
            normalized_options = normalize_legacy_cs2_selector_options(legacy_wrapper)
            if normalized_options is None:
                raise ValueError("Не удалось нормализовать CS2 selectors.")
            selector_data = normalized_options
            legacy_recipe_selectors = {
                **legacy_wrapper,
                "items": [
                    {key: value for key, value in option.items()
                     if key not in {"game_id", "app_id", "context_id"}}
                    for option in normalized_options
                ],
            }
        else:
            selector_data = {"delivery_mode": mode, "app_id": None, "context_id": None,
                             "auto_messages": dict(auto_messages or {})}
            bots = []
            quantity = 1
        legacy_values = {
            "delivery_quantity_per_deal": quantity,
            "item_selectors": legacy_recipe_selectors if mode == "cs2_items" else selector_data,
            "bot_pool": bots,
        }
        try:
            compatible_recipe = legacy_config_to_recipe(legacy_values)
            recipe_items = compatible_recipe.items
        except UnsupportedLegacyRecipeError:
            # Keep the historical config readable by its existing path, but do
            # not invent a typed recipe for manual or ambiguous selectors.
            recipe_items = None
        result = self.ledger.create_listing_config_version(
            listing_id=self._listing_key(external_id),
            source_platform="playerok",
            external_listing_id=external_id,
            delivery_quantity_per_deal=quantity,
            item_selectors=selector_data,
            bot_pool=bots,
            title=str(title or "").strip(),
            recipe_items=recipe_items,
        )
        # Persist the canonical listing URL as metadata, not as mutable config.
        with self.database.connect() as db:
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(gd_listings)")}
            if "external_url" not in columns:
                db.execute("ALTER TABLE gd_listings ADD COLUMN external_url TEXT NOT NULL DEFAULT ''")
            db.execute(
                "UPDATE gd_listings SET external_url=?,title=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE listing_id=?",
                (str(url or "").strip(), str(title or "").strip(), result["listing_id"]),
            )
        return result


    def allocate_listing_stock(
        self, *, config_version_id: str, listing_quantity: int
    ) -> dict[str, Any]:
        """Reserve listing_quantity deals of a typed CS2 recipe across the shared bot stock."""
        config_id = str(config_version_id or "").strip()
        if not config_id:
            raise ValueError("config_version_id обязателен")
        if isinstance(listing_quantity, bool) or not isinstance(listing_quantity, int) or listing_quantity < 1:
            raise ValueError("Количество в объявлении должно быть положительным целым числом.")

        with self.database.connect() as db:
            row = db.execute(
                """
                SELECT c.listing_id,c.recipe_schema_version,c.version_number,
                       l.source_platform,l.status,
                       (SELECT MAX(current.version_number) FROM gd_config_versions current
                        WHERE current.listing_id=c.listing_id) AS current_version
                FROM gd_config_versions c
                JOIN gd_listings l ON l.listing_id=c.listing_id
                WHERE c.config_version_id=?
                """,
                (config_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"ConfigVersion {config_id} не найдена")
            if str(row["source_platform"]).casefold() != "playerok":
                raise ValueError("Общий склад объявлений подключается только к Playerok ConfigVersion.")
            if str(row["status"]).casefold() != "active":
                raise ValueError("Нельзя выделить склад для отключённого объявления.")
            if row["recipe_schema_version"] != 1:
                raise ValueError(
                    "Для общего склада нужен однозначный typed Recipe; несколько CS2 selectors "
                    "считаются альтернативами и не могут резервироваться как отдельные позиции."
                )
            if int(row["version_number"]) != int(row["current_version"]):
                raise ValueError("Нельзя выделить склад для устаревшей версии ConfigVersion.")
            recipe = DeliveryLedger.recipe_for_config_version(
                db, config_id, allow_legacy=False
            )

        if not recipe.items or any(
            item.kind != "steam_asset"
            or not isinstance(item.spec, SteamAssetSpec)
            or item.spec.game_id != "cs2"
            or item.spec.app_id != 730
            or str(item.spec.context_id) != "2"
            for item in recipe.items
        ):
            raise ValueError(
                "Сейчас общий склад Playerok поддерживает только typed CS2 Steam Asset "
                "(AppID 730 / ContextID 2); смешанные и другие Recipe не резервируются."
            )

        required_items = [item for item in recipe.items if item.required]
        if not required_items:
            raise ValueError("В Recipe нет обязательных CS2 Steam Asset позиций для резерва.")
        units_by_recipe_item = {
            item.item_id: listing_quantity * item.quantity_per_order
            for item in required_items
        }
        request_payload = json.dumps(
            units_by_recipe_item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(
            f"{config_id}|{request_payload}".encode("utf-8")
        ).hexdigest()
        allocations = ListingStockService(self.database).allocate_recipe_items(
            request_id=f"playerok-listing-stock:{digest}",
            listing_id=str(row["listing_id"]),
            config_version_id=config_id,
            units_by_recipe_item=units_by_recipe_item,
        )
        return {
            "listing_id": str(row["listing_id"]),
            "config_version_id": config_id,
            "listing_quantity": listing_quantity,
            "units_by_recipe_item": units_by_recipe_item,
            "reserved_units": sum(units_by_recipe_item.values()),
            "allocations": allocations,
        }


    def save_recipe(
        self,
        *,
        external_listing_id: str,
        title: str,
        url: str,
        items: Recipe | list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Save a typed ConfigVersion for a caller-supplied canonical listing ID.

        This API does not resolve a URL or slug and does not connect the recipe
        to sync, stock reservation, or delivery execution.
        """
        external_id = str(external_listing_id or "").strip()
        if not external_id:
            raise ValueError("Укажи подтверждённый canonical listing_id Playerok.")
        recipe = Recipe.from_values(items)
        result = self.ledger.create_recipe_config_version(
            listing_id=self._listing_key(external_id),
            source_platform="playerok",
            external_listing_id=external_id,
            recipe_items=recipe,
            title=str(title or "").strip(),
        )
        with self.database.connect() as db:
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(gd_listings)")}
            if "external_url" not in columns:
                db.execute("ALTER TABLE gd_listings ADD COLUMN external_url TEXT NOT NULL DEFAULT ''")
            db.execute(
                "UPDATE gd_listings SET external_url=?,title=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE listing_id=?",
                (str(url or "").strip(), str(title or "").strip(), result["listing_id"]),
            )
        return result
