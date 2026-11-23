from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from core.database.db import Database
from core.delivery.stock_ledger import QuantityStockLedger, StockResource, _positive_int
from core.delivery.stock_identity import StockItemIdentity
from core.delivery.recipes import RecipeItem, SteamAssetSpec
from games.asset_ids import asset_id_key


class InsufficientListingStock(ValueError):
    def __init__(self, required: int, available: int):
        self.required = required
        self.available = available
        super().__init__(
            f"Недостаточно свободного остатка: требуется {required}, доступно {available}"
        )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


class ListingStockService:
    """Allocates shared stock to listing generations and transfers it atomically to order lines."""

    def __init__(self, database: Database):
        self.database = database

    def reallocate_listing_units(
        self,
        *,
        adjustment_id: str,
        listing_id: str,
        config_version_id: str,
        source_allocation_id: str,
        units: int,
        destinations: Iterable[StockResource],
        recipe_item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically replace a listing claim on one bot with equivalent stock on other bots.

        The source allocation is released (and its untransferred remainder is
        recreated when needed) in the same BEGIN IMMEDIATE transaction that
        claims the destination units. Both sides require usable, tradable,
        identity-verified observations. Typed recipes remain pinned to one
        Steam Asset RecipeItem; legacy listings require exact source/destination
        StockItemIdentity equality because they have no typed selector contract.
        """
        adjustment_id = str(adjustment_id or "").strip()
        listing_id = str(listing_id or "").strip()
        config_version_id = str(config_version_id or "").strip()
        source_allocation_id = str(source_allocation_id or "").strip()
        recipe_item_id = str(recipe_item_id).strip() if recipe_item_id is not None else None
        if not all((adjustment_id, listing_id, config_version_id, source_allocation_id)):
            raise ValueError(
                "adjustment_id, listing_id, config_version_id и source_allocation_id обязательны"
            )
        units = _positive_int(units, "units")

        normalized_destinations: dict[str, StockResource] = {}
        for candidate in destinations:
            resource = candidate.normalized()
            observation_id = QuantityStockLedger._observation_id(resource)
            normalized_destinations[observation_id] = resource
        ordered_destinations = sorted(
            normalized_destinations.values(),
            key=lambda resource: (
                resource.bot_name.casefold(), resource.game_id, resource.app_id,
                resource.context_id, resource.asset_id.casefold(),
            ),
        )
        if not ordered_destinations:
            raise InsufficientListingStock(units, 0)
        destination_observation_ids = [
            QuantityStockLedger._observation_id(resource)
            for resource in ordered_destinations
        ]

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM gd_stock_listing_adjustments WHERE adjustment_id=?",
                (adjustment_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["listing_id"] != listing_id
                    or prior["config_version_id"] != config_version_id
                    or prior["operation"] != "reallocate_units"
                    or prior["recipe_item_id"] != recipe_item_id
                    or int(prior["units"]) != units
                ):
                    raise ValueError("adjustment_id уже использован для другой listing reallocation")
                try:
                    audit_payload = json.loads(prior["allocation_ids_json"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Reallocation audit payload повреждён") from exc
                if (
                    not isinstance(audit_payload, dict)
                    or audit_payload.get("source_allocation_id") != source_allocation_id
                    or audit_payload.get("destination_observation_ids") != destination_observation_ids
                ):
                    raise ValueError("adjustment_id уже использован для другого source/destination")
                created_ids = audit_payload.get("created_allocation_ids")
                if not isinstance(created_ids, list) or not created_ids:
                    raise RuntimeError("Reallocation audit не содержит destination allocations")
                rows = self._listing_adjustment_rows(connection, created_ids)
                if (
                    sum(int(row["units"]) for row in rows) != units
                    or any(
                        row["owner_type"] != "listing"
                        or row["config_version_id"] != config_version_id
                        or row["recipe_item_id"] != recipe_item_id
                        for row in rows
                    )
                ):
                    raise RuntimeError("Сохранённый reallocation batch неполон или изменён")
                return rows

            config = connection.execute(
                """
                SELECT c.listing_id,c.recipe_schema_version,c.bot_pool_json,
                       l.status AS listing_status
                FROM gd_config_versions c
                JOIN gd_listings l ON l.listing_id=c.listing_id
                WHERE c.config_version_id=?
                """,
                (config_version_id,),
            ).fetchone()
            if config is None or config["listing_id"] != listing_id:
                raise ValueError("ConfigVersion не принадлежит указанному объявлению")
            if config["listing_status"] == "disabled":
                raise ValueError("Отключённому объявлению нельзя перераспределять складские units")
            if connection.execute(
                "SELECT 1 FROM gd_stock_allocation_batches "
                "WHERE listing_id=? AND config_version_id=?",
                (listing_id, config_version_id),
            ).fetchone() is None:
                raise ValueError("Для listing generation отсутствует исходный allocation batch")

            unresolved = int(connection.execute(
                """
                SELECT COUNT(*) FROM gd_stock_allocations
                WHERE owner_type='listing' AND listing_id=? AND config_version_id=?
                  AND state IN ('pending','unknown','legacy_conflict')
                """,
                (listing_id, config_version_id),
            ).fetchone()[0])
            if unresolved:
                raise ValueError(
                    "У listing generation есть pending/UNKNOWN/conflict allocations; "
                    "сначала требуется reconciliation"
                )

            source = connection.execute(
                """
                SELECT a.*,o.identity_json,o.is_tradable,
                       o.availability_state AS observation_state,
                       o.observed_amount,o.revision
                FROM gd_stock_allocations a
                JOIN gd_stock_observations o ON o.observation_id=a.observation_id
                WHERE a.allocation_id=? AND a.owner_type='listing'
                  AND a.listing_id=? AND a.config_version_id=? AND a.state='active'
                """,
                (source_allocation_id, listing_id, config_version_id),
            ).fetchone()
            if source is None:
                raise ValueError("Source allocation не является активным listing-owned ресурсом этой ConfigVersion")
            if source["recipe_item_id"] != recipe_item_id:
                raise ValueError("recipe_item_id source allocation не совпадает с запросом")
            if int(source["units"]) < units:
                raise InsufficientListingStock(units, int(source["units"]))
            if source["observation_state"] != "usable" or source["is_tradable"] != 1:
                raise ValueError("Source resource должен иметь свежее usable observation и is_tradable=True")
            try:
                source_identity_raw = json.loads(source["identity_json"])
                if not isinstance(source_identity_raw, dict):
                    raise ValueError("identity_json must be an object")
                source_identity = StockItemIdentity(**source_identity_raw).normalized()
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("Source resource identity не подтверждена; reallocation запрещён") from exc
            source_resource = StockResource(
                source["game_id"], int(source["app_id"]), source["context_id"],
                source["bot_name"], source["asset_id"],
            ).normalized()
            selector_spec = None
            legacy_bot_pool: list[str] = []
            if config["recipe_schema_version"] is not None:
                if not recipe_item_id:
                    raise ValueError("Для typed Recipe обязателен точный recipe_item_id")
                from core.delivery.ledger import DeliveryLedger
                recipe = DeliveryLedger.recipe_for_config_version(
                    connection, config_version_id, allow_legacy=False
                )
                selected_item = next(
                    (item for item in recipe.items if item.item_id == recipe_item_id), None
                )
                if selected_item is None or not isinstance(selected_item.spec, SteamAssetSpec):
                    raise ValueError("recipe_item_id должен указывать на Steam Asset RecipeItem")
                selector_spec = selected_item.spec
                if (
                    not source_identity.matches_spec(selector_spec)
                    or not source_identity.allows_bot(source_resource.bot_name, selector_spec)
                ):
                    raise ValueError("Source resource не соответствует SteamAssetSpec или allowed_bot_pool")
            else:
                if recipe_item_id is not None:
                    raise ValueError("Legacy ConfigVersion не поддерживает recipe_item_id")
                try:
                    legacy_bot_pool = json.loads(config["bot_pool_json"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("ConfigVersion bot_pool_json повреждён") from exc
                if not isinstance(legacy_bot_pool, list) or any(
                    not isinstance(bot, str) or not bot.strip() for bot in legacy_bot_pool
                ):
                    raise ValueError("ConfigVersion bot_pool должен быть списком имён ботов")
                identity_values = (
                    source_identity.market_hash_name, source_identity.name,
                    source_identity.item_type, source_identity.class_id,
                    source_identity.quality, source_identity.exterior, source_identity.rarity,
                )
                if not any(identity_values):
                    raise ValueError("Legacy source identity не содержит проверяемых item attributes")
                if legacy_bot_pool and source_resource.bot_name.casefold() not in {
                    bot.strip().casefold() for bot in legacy_bot_pool
                }:
                    raise ValueError("Source bot отсутствует в ConfigVersion allowed bot pool")

            planned: list[tuple[StockResource, str, int]] = []
            available_total = 0
            remaining = units
            for resource in ordered_destinations:
                if resource.bot_name.casefold() == source_resource.bot_name.casefold():
                    raise ValueError("Reallocation destination должен находиться на другом ASF bot")
                if (
                    resource.game_id != source_resource.game_id
                    or resource.app_id != source_resource.app_id
                    or resource.context_id != source_resource.context_id
                ):
                    raise ValueError("Reallocation destination должен иметь тот же game/app/context scope")
                if legacy_bot_pool and resource.bot_name.casefold() not in {
                    bot.strip().casefold() for bot in legacy_bot_pool
                }:
                    raise ValueError("Destination bot отсутствует в ConfigVersion allowed bot pool")
                observation_id = QuantityStockLedger._observation_id(resource)
                observation = connection.execute(
                    """
                    SELECT identity_json,is_tradable,availability_state,
                           observed_amount,revision
                    FROM gd_stock_observations WHERE observation_id=?
                    """,
                    (observation_id,),
                ).fetchone()
                if observation is None or observation["availability_state"] != "usable":
                    continue
                if observation["is_tradable"] != 1:
                    continue
                try:
                    identity_raw = json.loads(observation["identity_json"])
                    if not isinstance(identity_raw, dict):
                        continue
                    identity = StockItemIdentity(**identity_raw).normalized()
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if selector_spec is not None:
                    if (
                        not identity.matches_spec(selector_spec)
                        or not identity.allows_bot(resource.bot_name, selector_spec)
                    ):
                        continue
                elif identity != source_identity:
                    continue

                free = max(
                    0,
                    int(observation["observed_amount"])
                    - QuantityStockLedger._blocking_units(
                        connection, observation_id, int(observation["revision"])
                    ),
                )
                available_total += free
                take = min(remaining, free)
                if take:
                    planned.append((resource, observation_id, take))
                    remaining -= take
                if remaining == 0:
                    break
            if remaining:
                raise InsufficientListingStock(units, available_total)

            # All source and destination checks are complete. A failure in any
            # state transition or destination INSERT rolls back the whole move.
            connection.execute(
                "UPDATE gd_stock_allocations SET state='released',"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE allocation_id=? AND state='active'",
                (source_allocation_id,),
            )
            released_ids = [source_allocation_id]
            source_remainder_ids: list[str] = []
            source_remainder_units = int(source["units"]) - units
            if source_remainder_units:
                source_remainder_id = _stable_id(
                    "listing-reallocation-remainder", adjustment_id, source_allocation_id
                )
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active',?)
                    """,
                    (
                        source_remainder_id, source["observation_id"], source["game_id"],
                        source["app_id"], source["context_id"], source["bot_name"],
                        source["asset_id"], source["asset_id_key"], listing_id,
                        config_version_id, source_remainder_units, recipe_item_id,
                    ),
                )
                source_remainder_ids.append(source_remainder_id)

            created_ids: list[str] = []
            allocation_prefix = _stable_id("listing-reallocation", adjustment_id)
            for index, (resource, observation_id, take) in enumerate(planned):
                allocation_id = f"{allocation_prefix}:{index}"
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active',?)
                    """,
                    (
                        allocation_id, observation_id, resource.game_id, resource.app_id,
                        resource.context_id, resource.bot_name, resource.asset_id,
                        asset_id_key(resource.asset_id), listing_id, config_version_id,
                        take, recipe_item_id,
                    ),
                )
                created_ids.append(allocation_id)

            connection.execute(
                """
                INSERT INTO gd_stock_listing_adjustments
                    (adjustment_id,listing_id,config_version_id,operation,recipe_item_id,
                     units,allocation_ids_json)
                VALUES(?,?,?,'reallocate_units',?,?,?)
                """,
                (
                    adjustment_id, listing_id, config_version_id, recipe_item_id, units,
                    json.dumps({
                        "source_allocation_id": source_allocation_id,
                        "destination_observation_ids": destination_observation_ids,
                        "released_allocation_ids": released_ids,
                        "source_remainder_allocation_ids": source_remainder_ids,
                        "created_allocation_ids": created_ids,
                    }, separators=(",", ":")),
                ),
            )
            return self._listing_adjustment_rows(connection, created_ids)

    def increase_listing_units(
        self,
        *,
        adjustment_id: str,
        listing_id: str,
        config_version_id: str,
        units: int,
        resources: Iterable[StockResource],
    ) -> list[dict[str, Any]]:
        """Atomically add units to an existing legacy listing generation."""
        adjustment_id = str(adjustment_id or "").strip()
        listing_id = str(listing_id or "").strip()
        config_version_id = str(config_version_id or "").strip()
        if not adjustment_id or not listing_id or not config_version_id:
            raise ValueError("adjustment_id, listing_id и config_version_id обязательны")
        units = _positive_int(units, "units")
        normalized: dict[str, StockResource] = {}
        for candidate in resources:
            resource = candidate.normalized()
            normalized[QuantityStockLedger._observation_id(resource)] = resource
        ordered = sorted(
            normalized.values(),
            key=lambda r: (
                r.bot_name.casefold(), r.game_id, r.app_id, r.context_id,
                r.asset_id.casefold(),
            ),
        )

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM gd_stock_listing_adjustments WHERE adjustment_id=?",
                (adjustment_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["listing_id"] != listing_id
                    or prior["config_version_id"] != config_version_id
                    or prior["operation"] != "allocate_units"
                    or prior["recipe_item_id"] is not None
                    or int(prior["units"]) != units
                ):
                    raise ValueError("adjustment_id уже использован для другой listing operation")
                ids = json.loads(prior["allocation_ids_json"])
                rows = self._listing_adjustment_rows(connection, ids)
                if sum(int(row["units"]) for row in rows) != units or any(
                    row["recipe_item_id"] is not None for row in rows
                ):
                    raise RuntimeError("Сохранённый listing increase batch неполон или изменён")
                return rows

            config = connection.execute(
                "SELECT c.listing_id,c.recipe_schema_version,l.status AS listing_status "
                "FROM gd_config_versions c JOIN gd_listings l ON l.listing_id=c.listing_id "
                "WHERE c.config_version_id=?",
                (config_version_id,),
            ).fetchone()
            if config is None or config["listing_id"] != listing_id:
                raise ValueError("ConfigVersion не принадлежит указанному объявлению")
            if config["listing_status"] == "disabled":
                raise ValueError("Отключённому объявлению нельзя добавлять складские units")
            if config["recipe_schema_version"] is not None:
                raise ValueError("Для typed Recipe используйте increase_recipe_units")
            initial_batch = connection.execute(
                "SELECT 1 FROM gd_stock_allocation_batches "
                "WHERE listing_id=? AND config_version_id=?",
                (listing_id, config_version_id),
            ).fetchone()
            if initial_batch is None:
                raise ValueError("Для listing generation отсутствует исходный allocation batch")
            if not ordered:
                raise InsufficientListingStock(units, 0)

            plan: list[tuple[StockResource, str, int]] = []
            available_total = 0
            remaining = units
            for resource in ordered:
                observation_id = QuantityStockLedger._observation_id(resource)
                observation = connection.execute(
                    "SELECT observed_amount,revision,availability_state FROM gd_stock_observations "
                    "WHERE observation_id=?",
                    (observation_id,),
                ).fetchone()
                if observation is None or observation["availability_state"] != "usable":
                    continue
                free = max(
                    0,
                    int(observation["observed_amount"])
                    - QuantityStockLedger._blocking_units(
                        connection, observation_id, int(observation["revision"])
                    ),
                )
                available_total += free
                take = min(remaining, free)
                if take:
                    plan.append((resource, observation_id, take))
                    remaining -= take
                if remaining == 0:
                    break
            if remaining:
                raise InsufficientListingStock(units, available_total)

            allocation_prefix = "listing-adjustment:" + hashlib.sha256(
                adjustment_id.encode()
            ).hexdigest() + ":"
            allocation_ids: list[str] = []
            for index, (resource, observation_id, take) in enumerate(plan):
                allocation_id = f"{allocation_prefix}{index}"
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active',NULL)
                    """,
                    (
                        allocation_id, observation_id, resource.game_id, resource.app_id,
                        resource.context_id, resource.bot_name, resource.asset_id,
                        asset_id_key(resource.asset_id), listing_id, config_version_id, take,
                    ),
                )
                allocation_ids.append(allocation_id)
            connection.execute(
                """
                INSERT INTO gd_stock_listing_adjustments
                    (adjustment_id,listing_id,config_version_id,operation,recipe_item_id,
                     units,allocation_ids_json)
                VALUES(?,?,?,'allocate_units',NULL,?,?)
                """,
                (
                    adjustment_id, listing_id, config_version_id, units,
                    json.dumps(allocation_ids, separators=(",", ":")),
                ),
            )
            return self._listing_adjustment_rows(connection, allocation_ids)

    def increase_recipe_units(
        self,
        *,
        adjustment_id: str,
        listing_id: str,
        config_version_id: str,
        units_by_recipe_item: Mapping[str, int],
    ) -> list[dict[str, Any]]:
        """Atomically top up multiple typed Steam Asset RecipeItems without mixing them."""
        from core.delivery.ledger import DeliveryLedger

        adjustment_id = str(adjustment_id or "").strip()
        listing_id = str(listing_id or "").strip()
        config_version_id = str(config_version_id or "").strip()
        if not adjustment_id or not listing_id or not config_version_id:
            raise ValueError("adjustment_id, listing_id и config_version_id обязательны")
        if not isinstance(units_by_recipe_item, Mapping) or not units_by_recipe_item:
            raise ValueError("units_by_recipe_item должен содержать хотя бы одну Steam Asset позицию")
        normalized_units: dict[str, int] = {}
        for raw_id, raw_units in units_by_recipe_item.items():
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise ValueError("recipe_item_id должен быть непустой строкой")
            item_id = raw_id.strip()
            if item_id in normalized_units:
                raise ValueError("units_by_recipe_item содержит повторяющийся recipe_item_id")
            normalized_units[item_id] = _positive_int(raw_units, "units")

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM gd_stock_listing_adjustments WHERE adjustment_id=?",
                (adjustment_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["listing_id"] != listing_id
                    or prior["config_version_id"] != config_version_id
                    or prior["operation"] != "allocate_units"
                    or prior["recipe_item_id"] is not None
                    or int(prior["units"]) != sum(normalized_units.values())
                ):
                    raise ValueError("adjustment_id уже использован для другой recipe increase")
                ids = json.loads(prior["allocation_ids_json"])
                rows = self._listing_adjustment_rows(connection, ids)
                stored: dict[str, int] = {}
                for row in rows:
                    item_id = row["recipe_item_id"]
                    if not item_id:
                        raise RuntimeError("Typed increase содержит allocation без recipe_item_id")
                    stored[item_id] = stored.get(item_id, 0) + int(row["units"])
                if stored != normalized_units:
                    raise RuntimeError("Сохранённый recipe increase batch неполон или имеет другой состав")
                return rows

            config = connection.execute(
                "SELECT c.listing_id,c.recipe_schema_version,l.status AS listing_status "
                "FROM gd_config_versions c JOIN gd_listings l ON l.listing_id=c.listing_id "
                "WHERE c.config_version_id=?",
                (config_version_id,),
            ).fetchone()
            if config is None or config["listing_id"] != listing_id:
                raise ValueError("ConfigVersion не принадлежит указанному объявлению")
            if config["listing_status"] == "disabled":
                raise ValueError("Отключённому объявлению нельзя добавлять складские units")
            if config["recipe_schema_version"] != 1:
                raise ValueError("Требуется ConfigVersion с типизированным Recipe v1")
            initial_batch = connection.execute(
                "SELECT 1 FROM gd_stock_allocation_batches "
                "WHERE listing_id=? AND config_version_id=?",
                (listing_id, config_version_id),
            ).fetchone()
            if initial_batch is None:
                raise ValueError("Для listing generation отсутствует исходный allocation batch")
            recipe = DeliveryLedger.recipe_for_config_version(
                connection, config_version_id, allow_legacy=False
            )
            item_by_id = {item.item_id: item for item in recipe.items}
            steam_items = {
                item_id: item for item_id, item in item_by_id.items()
                if item.kind == "steam_asset" and isinstance(item.spec, SteamAssetSpec)
            }
            unknown_ids = set(normalized_units) - set(steam_items)
            if unknown_ids:
                raise ValueError(
                    "Увеличивать можно только steam_asset RecipeItem: "
                    + ", ".join(sorted(unknown_ids))
                )

            observations = connection.execute(
                """
                SELECT * FROM gd_stock_observations
                WHERE availability_state='usable' AND identity_json IS NOT NULL
                  AND is_tradable=1
                ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key
                """
            ).fetchall()
            parsed: list[tuple[Any, StockItemIdentity, StockResource]] = []
            free_by_observation: dict[str, int] = {}
            for observation in observations:
                try:
                    raw_identity = json.loads(observation["identity_json"])
                    if not isinstance(raw_identity, dict):
                        continue
                    identity = StockItemIdentity(**raw_identity).normalized()
                    resource = StockResource(
                        observation["game_id"], int(observation["app_id"]),
                        observation["context_id"], observation["bot_name"],
                        observation["asset_id"],
                    ).normalized()
                    free = max(
                        0,
                        int(observation["observed_amount"])
                        - QuantityStockLedger._blocking_units(
                            connection, observation["observation_id"],
                            int(observation["revision"]),
                        ),
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if free > 0:
                    parsed.append((observation, identity, resource))
                    free_by_observation[observation["observation_id"]] = free

            plan: list[tuple[str, StockResource, str, int]] = []
            for item_id, recipe_item in [
                (key, value) for key, value in steam_items.items()
                if key in normalized_units
            ]:
                remaining = normalized_units[item_id]
                available_total = 0
                for observation, identity, resource in parsed:
                    observation_id = observation["observation_id"]
                    free = free_by_observation.get(observation_id, 0)
                    if free <= 0:
                        continue
                    if not identity.matches_spec(recipe_item.spec) or not identity.allows_bot(
                        resource.bot_name, recipe_item.spec
                    ):
                        continue
                    available_total += free
                    take = min(remaining, free)
                    if take:
                        plan.append((item_id, resource, observation_id, take))
                        free_by_observation[observation_id] -= take
                        remaining -= take
                    if remaining == 0:
                        break
                if remaining:
                    raise InsufficientListingStock(normalized_units[item_id], available_total)

            allocation_prefix = "listing-adjustment:" + hashlib.sha256(
                adjustment_id.encode()
            ).hexdigest() + ":"
            allocation_ids: list[str] = []
            for index, (item_id, resource, observation_id, take) in enumerate(plan):
                allocation_id = f"{allocation_prefix}{index}"
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active',?)
                    """,
                    (
                        allocation_id, observation_id, resource.game_id, resource.app_id,
                        resource.context_id, resource.bot_name, resource.asset_id,
                        asset_id_key(resource.asset_id), listing_id, config_version_id,
                        take, item_id,
                    ),
                )
                allocation_ids.append(allocation_id)
            connection.execute(
                """
                INSERT INTO gd_stock_listing_adjustments
                    (adjustment_id,listing_id,config_version_id,operation,recipe_item_id,
                     units,allocation_ids_json)
                VALUES(?,?,?,'allocate_units',NULL,?,?)
                """,
                (
                    adjustment_id, listing_id, config_version_id, sum(normalized_units.values()),
                    json.dumps(allocation_ids, separators=(",", ":")),
                ),
            )
            return self._listing_adjustment_rows(connection, allocation_ids)

    @staticmethod
    def _listing_adjustment_rows(connection, allocation_ids: list[str]) -> list[dict[str, Any]]:
        if not allocation_ids:
            return []
        marks = ",".join("?" for _ in allocation_ids)
        return [
            dict(row) for row in connection.execute(
                f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({marks}) "
                "ORDER BY recipe_item_id,bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key",
                allocation_ids,
            ).fetchall()
        ]

    def release_listing_units(
        self,
        *,
        adjustment_id: str,
        listing_id: str,
        config_version_id: str,
        units: int,
        recipe_item_id: str | None = None,
        connection=None,
    ) -> list[dict[str, Any]]:
        """Release only listing-owned units; order-owned and uncertain stock stays blocked."""
        adjustment_id = str(adjustment_id or "").strip()
        listing_id = str(listing_id or "").strip()
        config_version_id = str(config_version_id or "").strip()
        recipe_item_id = str(recipe_item_id).strip() if recipe_item_id is not None else None
        if not adjustment_id or not listing_id or not config_version_id:
            raise ValueError("adjustment_id, listing_id и config_version_id обязательны")
        units = _positive_int(units, "units")
        if connection is not None:
            return self._release_listing_units_in_transaction(
                connection, adjustment_id=adjustment_id, listing_id=listing_id,
                config_version_id=config_version_id, units=units,
                recipe_item_id=recipe_item_id, operation="release_units",
            )
        with self.database.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            return self._release_listing_units_in_transaction(
                owned, adjustment_id=adjustment_id, listing_id=listing_id,
                config_version_id=config_version_id, units=units,
                recipe_item_id=recipe_item_id, operation="release_units",
            )

    def disable_listing_stock(
        self,
        *,
        adjustment_id: str,
        listing_id: str,
        config_version_id: str,
        connection=None,
    ) -> list[dict[str, Any]]:
        """Disable one listing generation and release its remaining listing-owned stock.

        Active order allocations are deliberately not selected. Pending, UNKNOWN,
        and legacy-conflict listing allocations block the operation until reconciled.
        """
        adjustment_id = str(adjustment_id or "").strip()
        listing_id = str(listing_id or "").strip()
        config_version_id = str(config_version_id or "").strip()
        if not adjustment_id or not listing_id or not config_version_id:
            raise ValueError("adjustment_id, listing_id и config_version_id обязательны")
        if connection is not None:
            return self._release_listing_units_in_transaction(
                connection, adjustment_id=adjustment_id, listing_id=listing_id,
                config_version_id=config_version_id, units=None,
                recipe_item_id=None, operation="disable",
            )
        with self.database.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            return self._release_listing_units_in_transaction(
                owned, adjustment_id=adjustment_id, listing_id=listing_id,
                config_version_id=config_version_id, units=None,
                recipe_item_id=None, operation="disable",
            )

    def _release_listing_units_in_transaction(
        self,
        connection,
        *,
        adjustment_id: str,
        listing_id: str,
        config_version_id: str,
        units: int | None,
        recipe_item_id: str | None,
        operation: str,
    ) -> list[dict[str, Any]]:
        if not connection.in_transaction:
            raise RuntimeError("Listing stock adjustment requires an active transaction")

        prior = connection.execute(
            "SELECT * FROM gd_stock_listing_adjustments WHERE adjustment_id=?",
            (adjustment_id,),
        ).fetchone()
        if prior is not None:
            if (
                prior["listing_id"] != listing_id
                or prior["config_version_id"] != config_version_id
                or prior["operation"] != operation
                or prior["recipe_item_id"] != recipe_item_id
                or (units is not None and int(prior["units"]) != units)
            ):
                raise ValueError("adjustment_id уже использован для другой listing operation")
            ids = json.loads(prior["allocation_ids_json"])
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            return [
                dict(row) for row in connection.execute(
                    f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({marks}) "
                    "ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key,allocation_id",
                    ids,
                ).fetchall()
            ]

        config = connection.execute(
            "SELECT listing_id,recipe_schema_version FROM gd_config_versions WHERE config_version_id=?",
            (config_version_id,),
        ).fetchone()
        if config is None or config["listing_id"] != listing_id:
            raise ValueError("ConfigVersion не принадлежит указанному объявлению")
        typed_recipe = config["recipe_schema_version"] is not None
        if typed_recipe:
            if operation == "release_units":
                from core.delivery.ledger import DeliveryLedger
                recipe = DeliveryLedger.recipe_for_config_version(
                    connection, config_version_id, allow_legacy=False
                )
                item = next((value for value in recipe.items if value.item_id == recipe_item_id), None)
                if item is None or not isinstance(item.spec, SteamAssetSpec):
                    raise ValueError("Для typed Recipe требуется точный Steam Asset recipe_item_id")
        elif recipe_item_id is not None:
            raise ValueError("Legacy ConfigVersion не поддерживает recipe_item_id")

        unresolved = connection.execute(
            """
            SELECT COUNT(*) FROM gd_stock_allocations
            WHERE owner_type='listing' AND listing_id=? AND config_version_id=?
              AND state IN ('pending','unknown','legacy_conflict')
            """,
            (listing_id, config_version_id),
        ).fetchone()[0]
        if int(unresolved):
            raise ValueError(
                "У listing generation есть pending/UNKNOWN/conflict allocations; "
                "сначала требуется reconciliation"
            )

        where_recipe = ""
        params: tuple[Any, ...]
        if typed_recipe and operation == "release_units":
            where_recipe = " AND recipe_item_id=?"
            params = (listing_id, config_version_id, recipe_item_id)
        else:
            params = (listing_id, config_version_id)
        sources = connection.execute(
            """
            SELECT a.*,o.availability_state AS observation_state
            FROM gd_stock_allocations a
            JOIN gd_stock_observations o ON o.observation_id=a.observation_id
            WHERE a.owner_type='listing' AND a.listing_id=? AND a.config_version_id=?
              AND a.state='active'
            """ + where_recipe + """
            ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key,allocation_id
            """,
            params,
        ).fetchall()
        available = sum(int(row["units"]) for row in sources)
        release_units = available if operation == "disable" else int(units or 0)
        if operation == "release_units" and release_units <= 0:
            raise ValueError("Для partial release требуется положительное количество units")
        if operation == "disable" and release_units < 0:
            raise ValueError("Количество units для disable не может быть отрицательным")
        if release_units > available:
            raise InsufficientListingStock(release_units, available)

        remaining = release_units
        changed_ids: list[str] = []
        for index, source in enumerate(sources):
            if remaining <= 0:
                break
            source_units = int(source["units"])
            take = min(source_units, remaining)
            connection.execute(
                "UPDATE gd_stock_allocations SET state='released',"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE allocation_id=? AND state='active'",
                (source["allocation_id"],),
            )
            changed_ids.append(source["allocation_id"])
            keep = source_units - take
            if keep:
                if source["observation_state"] != "usable":
                    raise ValueError(
                        "Нельзя частично изменять listing allocation по stale/unavailable observation; "
                        "требуется свежий полный snapshot"
                    )
                remainder_id = _stable_id(
                    "listing-adjustment-remainder", adjustment_id, source["allocation_id"]
                )
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active',?)
                    """,
                    (
                        remainder_id, source["observation_id"], source["game_id"],
                        source["app_id"], source["context_id"], source["bot_name"],
                        source["asset_id"], source["asset_id_key"], listing_id,
                        config_version_id, keep, source["recipe_item_id"],
                    ),
                )
                changed_ids.append(remainder_id)
            remaining -= take

        if remaining:
            raise InsufficientListingStock(release_units, release_units - remaining)
        connection.execute(
            """
            INSERT INTO gd_stock_listing_adjustments
                (adjustment_id,listing_id,config_version_id,operation,recipe_item_id,
                 units,allocation_ids_json)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                adjustment_id, listing_id, config_version_id, operation,
                recipe_item_id, release_units,
                json.dumps(changed_ids, separators=(",", ":")),
            ),
        )
        if operation == "disable":
            connection.execute(
                "UPDATE gd_listings SET status='disabled',auto_relist=0,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE listing_id=?",
                (listing_id,),
            )
        if not changed_ids:
            return []
        marks = ",".join("?" for _ in changed_ids)
        return [
            dict(row) for row in connection.execute(
                f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({marks}) "
                "ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key,allocation_id",
                changed_ids,
            ).fetchall()
        ]

    def allocate_listing(
        self,
        *,
        request_id: str,
        listing_id: str,
        config_version_id: str,
        units: int,
        resources: Iterable[StockResource],
    ) -> list[dict[str, Any]]:
        request_id = str(request_id or "").strip()
        listing_id = str(listing_id or "").strip()
        config_version_id = str(config_version_id or "").strip()
        if not request_id or not listing_id or not config_version_id:
            raise ValueError("request_id, listing_id и config_version_id обязательны")
        units = _positive_int(units, "units")
        normalized_by_physical_id = {}
        for candidate in resources:
            resource = candidate.normalized()
            normalized_by_physical_id[QuantityStockLedger._observation_id(resource)] = resource
        ordered = sorted(
            normalized_by_physical_id.values(),
            key=lambda r: (
                r.bot_name.casefold(), r.game_id, r.app_id, r.context_id,
                r.asset_id.casefold(),
            ),
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM gd_stock_allocation_batches WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["listing_id"] != listing_id
                    or existing["config_version_id"] != config_version_id
                    or int(existing["requested_units"]) != units
                ):
                    raise ValueError("request_id уже использован для другого allocation")
                rows = [
                    dict(row) for row in connection.execute(
                        "SELECT * FROM gd_stock_allocations WHERE allocation_id LIKE ? ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key",
                        (f"listing-batch:{hashlib.sha256(request_id.encode()).hexdigest()}:%",),
                    ).fetchall()
                ]
                if sum(int(row["units"]) for row in rows) != units:
                    raise RuntimeError("Сохранённый listing allocation batch неполон; требуется ручная сверка")
                return rows

            config = connection.execute(
                "SELECT c.listing_id,c.recipe_schema_version,l.status AS listing_status "
                "FROM gd_config_versions c JOIN gd_listings l ON l.listing_id=c.listing_id "
                "WHERE c.config_version_id=?",
                (config_version_id,),
            ).fetchone()
            if config is None or config["listing_id"] != listing_id:
                raise ValueError("ConfigVersion не принадлежит указанному объявлению")
            if config["listing_status"] == "disabled":
                raise ValueError("Listing generation отключено; для нового запуска требуется новая ConfigVersion")
            if config["recipe_schema_version"] is not None:
                raise ValueError(
                    "Для типизированного Recipe используйте allocate_recipe_items: "
                    "обычная аллокация не привязывает ресурсы к RecipeItem"
                )
            if not ordered:
                raise InsufficientListingStock(units, 0)

            plan: list[tuple[StockResource, str, int]] = []
            available_total = 0
            remaining = units
            for resource in ordered:
                observation_id = QuantityStockLedger._observation_id(resource)
                observation = connection.execute(
                    "SELECT observed_amount,revision,availability_state FROM gd_stock_observations "
                    "WHERE observation_id=?",
                    (observation_id,),
                ).fetchone()
                if observation is None or observation["availability_state"] != "usable":
                    continue
                used = QuantityStockLedger._blocking_units(
                    connection, observation_id, int(observation["revision"])
                )
                free = max(0, int(observation["observed_amount"]) - used)
                available_total += free
                take = min(remaining, free)
                if take:
                    plan.append((resource, observation_id, take))
                    remaining -= take
                if remaining == 0:
                    break
            if remaining:
                raise InsufficientListingStock(units, available_total)

            connection.execute(
                "INSERT INTO gd_stock_allocation_batches(request_id,listing_id,config_version_id,requested_units) "
                "VALUES(?,?,?,?)",
                (request_id, listing_id, config_version_id, units),
            )
            batch_hash = hashlib.sha256(request_id.encode()).hexdigest()
            allocation_ids = []
            for index, (resource, observation_id, take) in enumerate(plan):
                allocation_id = f"listing-batch:{batch_hash}:{index}"
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active',?)
                    """,
                    (
                        allocation_id, observation_id, resource.game_id, resource.app_id,
                        resource.context_id, resource.bot_name, resource.asset_id,
                        asset_id_key(resource.asset_id),
                        listing_id, config_version_id, take, None,
                    ),
                )
                allocation_ids.append(allocation_id)
            placeholders = ",".join("?" for _ in allocation_ids)
            return [
                dict(row) for row in connection.execute(
                    f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({placeholders}) ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key",
                    allocation_ids,
                ).fetchall()
            ]

    def allocate_recipe_items(
        self,
        *,
        request_id: str,
        listing_id: str,
        config_version_id: str,
        units_by_recipe_item: Mapping[str, int],
    ) -> list[dict[str, Any]]:
        """Allocate exact tradable Steam assets to separate pinned RecipeItems."""
        from core.delivery.ledger import DeliveryLedger

        request_id = str(request_id or "").strip()
        listing_id = str(listing_id or "").strip()
        config_version_id = str(config_version_id or "").strip()
        if not request_id or not listing_id or not config_version_id:
            raise ValueError("request_id, listing_id и config_version_id обязательны")
        if not isinstance(units_by_recipe_item, Mapping) or not units_by_recipe_item:
            raise ValueError("units_by_recipe_item должен содержать хотя бы одну Steam Asset позицию")
        normalized_units: dict[str, int] = {}
        for raw_item_id, raw_units in units_by_recipe_item.items():
            if not isinstance(raw_item_id, str) or not raw_item_id.strip():
                raise ValueError("recipe_item_id должен быть непустой строкой")
            item_id = raw_item_id.strip()
            if item_id in normalized_units:
                raise ValueError("units_by_recipe_item содержит повторяющийся recipe_item_id")
            normalized_units[item_id] = _positive_int(raw_units, "units")

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            config = connection.execute(
                "SELECT c.listing_id,c.recipe_schema_version,l.status AS listing_status "
                "FROM gd_config_versions c JOIN gd_listings l ON l.listing_id=c.listing_id "
                "WHERE c.config_version_id=?",
                (config_version_id,),
            ).fetchone()
            if config is None or config["listing_id"] != listing_id:
                raise ValueError("ConfigVersion не принадлежит указанному объявлению")
            if config["listing_status"] == "disabled":
                raise ValueError("Listing generation отключено; для нового запуска требуется новая ConfigVersion")
            if config["recipe_schema_version"] != 1:
                raise ValueError("Требуется ConfigVersion с типизированным Recipe v1")
            recipe = DeliveryLedger.recipe_for_config_version(
                connection, config_version_id, allow_legacy=False
            )
            item_by_id = {item.item_id: item for item in recipe.items}
            steam_items = {
                item_id: item for item_id, item in item_by_id.items()
                if item.kind == "steam_asset" and isinstance(item.spec, SteamAssetSpec)
            }
            if not steam_items:
                raise ValueError("В Recipe нет Steam Asset позиций для quantity stock allocation")
            unknown_ids = set(normalized_units) - set(steam_items)
            if unknown_ids:
                raise ValueError(
                    "Аллокация разрешена только для steam_asset RecipeItem; "
                    f"неподдерживаемые IDs: {', '.join(sorted(unknown_ids))}"
                )
            missing_required = {
                item_id for item_id, item in steam_items.items()
                if item.required and item_id not in normalized_units
            }
            if missing_required:
                raise ValueError(
                    "Не заданы units для обязательных Steam Asset RecipeItem: "
                    + ", ".join(sorted(missing_required))
                )

            batch_hash = hashlib.sha256(request_id.encode()).hexdigest()
            allocation_prefix = f"listing-batch:{batch_hash}:"
            prior = connection.execute(
                "SELECT * FROM gd_stock_allocation_batches WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["listing_id"] != listing_id
                    or prior["config_version_id"] != config_version_id
                    or int(prior["requested_units"]) != sum(normalized_units.values())
                ):
                    raise ValueError("request_id уже использован для другого recipe allocation")
                rows = [
                    dict(row) for row in connection.execute(
                        "SELECT * FROM gd_stock_allocations WHERE allocation_id LIKE ? "
                        "ORDER BY recipe_item_id,bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key",
                        (allocation_prefix + "%",),
                    ).fetchall()
                ]
                stored_units: dict[str, int] = {}
                for row in rows:
                    item_id = row["recipe_item_id"]
                    if not item_id:
                        raise RuntimeError("Recipe allocation содержит строку без recipe_item_id")
                    stored_units[item_id] = stored_units.get(item_id, 0) + int(row["units"])
                if stored_units != normalized_units:
                    raise RuntimeError(
                        "Сохранённый recipe allocation batch неполон или имеет другой состав; "
                        "требуется ручная сверка"
                    )
                return rows

            other_batch = connection.execute(
                "SELECT request_id FROM gd_stock_allocation_batches "
                "WHERE listing_id=? AND config_version_id=?",
                (listing_id, config_version_id),
            ).fetchone()
            if other_batch is not None:
                raise ValueError(
                    "Для этой ConfigVersion уже создан allocation batch; "
                    "изменение требует отдельной listing generation"
                )

            observations = connection.execute(
                """
                SELECT * FROM gd_stock_observations
                WHERE availability_state='usable'
                  AND identity_json IS NOT NULL
                  AND is_tradable=1
                ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key
                """
            ).fetchall()
            parsed: list[tuple[Any, StockItemIdentity, StockResource]] = []
            free_by_observation: dict[str, int] = {}
            for observation in observations:
                try:
                    raw_identity = json.loads(observation["identity_json"])
                    if not isinstance(raw_identity, dict):
                        continue
                    identity = StockItemIdentity(**raw_identity).normalized()
                    resource = StockResource(
                        observation["game_id"], int(observation["app_id"]),
                        observation["context_id"], observation["bot_name"],
                        observation["asset_id"],
                    ).normalized()
                    free = max(
                        0,
                        int(observation["observed_amount"])
                        - QuantityStockLedger._blocking_units(
                            connection, observation["observation_id"],
                            int(observation["revision"]),
                        ),
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if free <= 0:
                    continue
                parsed.append((observation, identity, resource))
                free_by_observation[observation["observation_id"]] = free

            plan: list[tuple[str, StockResource, str, int]] = []
            requested_items = [
                (key, value) for key, value in steam_items.items()
                if key in normalized_units
            ]
            for item_id, recipe_item in requested_items:
                spec = recipe_item.spec
                required_units = normalized_units[item_id]
                available_total = 0
                remaining = required_units
                for observation, identity, resource in parsed:
                    observation_id = observation["observation_id"]
                    free = free_by_observation.get(observation_id, 0)
                    if free <= 0:
                        continue
                    if not identity.matches_spec(spec) or not identity.allows_bot(resource.bot_name, spec):
                        continue
                    available_total += free
                    take = min(remaining, free)
                    if take:
                        plan.append((item_id, resource, observation_id, take))
                        free_by_observation[observation_id] -= take
                        remaining -= take
                    if remaining == 0:
                        break
                if remaining:
                    raise InsufficientListingStock(required_units, available_total)

            connection.execute(
                """
                INSERT INTO gd_stock_allocation_batches
                    (request_id,listing_id,config_version_id,requested_units)
                VALUES(?,?,?,?)
                """,
                (request_id, listing_id, config_version_id, sum(normalized_units.values())),
            )
            allocation_ids = []
            for index, (item_id, resource, observation_id, take) in enumerate(plan):
                allocation_id = f"{allocation_prefix}{index}"
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active',?)
                    """,
                    (
                        allocation_id, observation_id, resource.game_id, resource.app_id,
                        resource.context_id, resource.bot_name, resource.asset_id,
                        asset_id_key(resource.asset_id), listing_id, config_version_id,
                        take, item_id,
                    ),
                )
                allocation_ids.append(allocation_id)
            placeholders = ",".join("?" for _ in allocation_ids)
            return [
                dict(row) for row in connection.execute(
                    f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({placeholders}) "
                    "ORDER BY recipe_item_id,bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key",
                    allocation_ids,
                ).fetchall()
            ]

    def transfer_to_order(
        self,
        *,
        transfer_event_id: str,
        listing_id: str,
        config_version_id: str,
        order_line_id: str,
        units: int,
        recipe_item_id: str | None = None,
        connection=None,
    ) -> list[dict[str, Any]]:
        transfer_event_id = str(transfer_event_id or "").strip()
        if not all((transfer_event_id, listing_id, config_version_id, order_line_id)):
            raise ValueError("transfer_event_id, listing_id, config_version_id и order_line_id обязательны")
        units = _positive_int(units, "units")
        if connection is not None:
            return self._transfer_to_order_in_transaction(
                connection,
                transfer_event_id=transfer_event_id,
                listing_id=listing_id,
                config_version_id=config_version_id,
                order_line_id=order_line_id,
                units=units,
                recipe_item_id=recipe_item_id,
            )
        with self.database.connect() as owned_connection:
            owned_connection.execute("BEGIN IMMEDIATE")
            return self._transfer_to_order_in_transaction(
                owned_connection,
                transfer_event_id=transfer_event_id,
                listing_id=listing_id,
                config_version_id=config_version_id,
                order_line_id=order_line_id,
                units=units,
                recipe_item_id=recipe_item_id,
            )

    def _transfer_to_order_in_transaction(
        self,
        connection,
        *,
        transfer_event_id: str,
        listing_id: str,
        config_version_id: str,
        order_line_id: str,
        units: int,
        recipe_item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        recipe_item_id = str(recipe_item_id).strip() if recipe_item_id is not None else None
        prior = connection.execute(
            "SELECT * FROM gd_stock_transfers WHERE transfer_event_id=?",
            (transfer_event_id,),
        ).fetchone()
        if prior is not None:
            if (
                prior["listing_id"] != listing_id
                or prior["config_version_id"] != config_version_id
                or prior["order_line_id"] != order_line_id
                or prior["recipe_item_id"] != recipe_item_id
                or int(prior["units"]) != units
            ):
                raise ValueError("transfer_event_id уже использован с другими параметрами")
            ids = json.loads(prior["allocation_ids_json"])
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            return [
                dict(row) for row in connection.execute(
                    f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({marks}) ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key",
                    ids,
                ).fetchall()
            ]

        line = connection.execute(
            "SELECT quantity,config_version_id,item_type,item_selector_json "
            "FROM gd_order_lines WHERE order_line_id=?",
            (order_line_id,),
        ).fetchone()
        if line is None or line["config_version_id"] != config_version_id:
            raise ValueError("OrderLine отсутствует или относится к другой ConfigVersion")

        from core.delivery.ledger import DeliveryLedger
        config = connection.execute(
            "SELECT recipe_schema_version FROM gd_config_versions WHERE config_version_id=?",
            (config_version_id,),
        ).fetchone()
        if config is None:
            raise ValueError("ConfigVersion не найдена")
        typed_recipe = config["recipe_schema_version"] is not None
        selected_recipe_item = None
        if typed_recipe:
            if not recipe_item_id:
                raise ValueError("Для typed Recipe transfer обязателен recipe_item_id")
            recipe = DeliveryLedger.recipe_for_config_version(
                connection, config_version_id, allow_legacy=False
            )
            selected_recipe_item = next(
                (item for item in recipe.items if item.item_id == recipe_item_id), None
            )
            if selected_recipe_item is None or not isinstance(selected_recipe_item.spec, SteamAssetSpec):
                raise ValueError("recipe_item_id не указывает на Steam Asset позицию этой ConfigVersion")
            try:
                line_snapshot = json.loads(line["item_selector_json"])
                pinned_item = RecipeItem.from_dict(line_snapshot["recipe_item"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("OrderLine не содержит корректный pinned RecipeItem") from exc
            if (
                line_snapshot.get("config_version_id") != config_version_id
                or line_snapshot.get("recipe_item_id") != recipe_item_id
                or line["item_type"] != selected_recipe_item.kind
                or pinned_item.to_dict() != selected_recipe_item.to_dict()
            ):
                raise ValueError("OrderLine не закреплена за указанным Steam Asset RecipeItem")
        elif recipe_item_id is not None:
            raise ValueError("Legacy ConfigVersion не поддерживает recipe_item_id")
        already_transferred = int(connection.execute(
            "SELECT COALESCE(SUM(units),0) FROM gd_stock_transfers WHERE order_line_id=?",
            (order_line_id,),
        ).fetchone()[0])
        if already_transferred + units > int(line["quantity"]):
            raise ValueError("Перенос превышает количество OrderLine")

        source_filter = (
            "AND a.recipe_item_id=?" if typed_recipe else "AND a.recipe_item_id IS NULL"
        )
        source_params = (
            (listing_id, config_version_id, recipe_item_id)
            if typed_recipe else (listing_id, config_version_id)
        )
        sources = connection.execute(
            f"""
            SELECT a.*,o.identity_json,o.is_tradable,o.availability_state AS observation_state
            FROM gd_stock_allocations a
            JOIN gd_stock_observations o ON o.observation_id=a.observation_id
            WHERE a.owner_type='listing' AND a.listing_id=? AND a.config_version_id=?
              AND a.state='active' {source_filter}
            ORDER BY a.bot_name COLLATE NOCASE,a.game_id,a.app_id,a.context_id,
                     a.asset_id_key,a.allocation_id
            """,
            source_params,
        ).fetchall()
        if typed_recipe:
            for source in sources:
                try:
                    current_identity = StockItemIdentity(**json.loads(source["identity_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise ValueError("Текущая identity ресурса не подтверждена; transfer заблокирован") from None
                if (
                    source["observation_state"] != "usable"
                    or source["is_tradable"] != 1
                    or not current_identity.matches_spec(selected_recipe_item.spec)
                    or not current_identity.allows_bot(source["bot_name"], selected_recipe_item.spec)
                ):
                    raise ValueError("Ресурс больше не соответствует SteamAsset RecipeItem; transfer заблокирован")
        if sum(int(row["units"]) for row in sources) < units:
            raise InsufficientListingStock(
                units, sum(int(row["units"]) for row in sources)
            )

        remaining = units
        transfer_ids: list[str] = []
        event_hash = hashlib.sha256(transfer_event_id.encode()).hexdigest()
        for index, source in enumerate(sources):
            if remaining <= 0:
                break
            source_units = int(source["units"])
            take = min(source_units, remaining)
            connection.execute(
                "UPDATE gd_stock_allocations SET state='released',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE allocation_id=? AND state='active'",
                (source["allocation_id"],),
            )
            if take < source_units:
                remainder_id = _stable_id("listing-remainder", transfer_event_id, source["allocation_id"])
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state,recipe_item_id)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active',?)
                    """,
                    (
                        remainder_id, source["observation_id"], source["game_id"],
                        source["app_id"], source["context_id"], source["bot_name"],
                        source["asset_id"], source["asset_id_key"], listing_id,
                        config_version_id, source_units - take, source["recipe_item_id"],
                    ),
                )
            order_id = f"order-transfer:{event_hash}:{index}"
            connection.execute(
                """
                INSERT INTO gd_stock_allocations
                    (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                     asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                     order_line_id,legacy_owner_id,units,state,recipe_item_id)
                VALUES(?,?,?,?,?,?,?,?, 'order',NULL,NULL,?,NULL,?,'active',?)
                """,
                (
                    order_id, source["observation_id"], source["game_id"],
                    source["app_id"], source["context_id"], source["bot_name"],
                    source["asset_id"], source["asset_id_key"], order_line_id, take,
                    recipe_item_id,
                ),
            )
            transfer_ids.append(order_id)
            remaining -= take
        if remaining:
            raise InsufficientListingStock(units, units - remaining)
        connection.execute(
            """
            INSERT INTO gd_stock_transfers
                (transfer_event_id,listing_id,config_version_id,order_line_id,units,allocation_ids_json,recipe_item_id)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                transfer_event_id, listing_id, config_version_id, order_line_id,
                units, json.dumps(transfer_ids, separators=(",", ":")), recipe_item_id,
            ),
        )
        marks = ",".join("?" for _ in transfer_ids)
        return [
            dict(row) for row in connection.execute(
                f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({marks}) ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key",
                transfer_ids,
            ).fetchall()
        ]
