from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from core.database.db import Database
from core.delivery.stock_ledger import QuantityStockLedger, StockResource, _positive_int
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
                        "SELECT * FROM gd_stock_allocations WHERE allocation_id LIKE ? ORDER BY allocation_id",
                        (f"listing-batch:{hashlib.sha256(request_id.encode()).hexdigest()}:%",),
                    ).fetchall()
                ]
                if sum(int(row["units"]) for row in rows) != units:
                    raise RuntimeError("Сохранённый listing allocation batch неполон; требуется ручная сверка")
                return rows

            config = connection.execute(
                "SELECT listing_id FROM gd_config_versions WHERE config_version_id=?",
                (config_version_id,),
            ).fetchone()
            if config is None or config["listing_id"] != listing_id:
                raise ValueError("ConfigVersion не принадлежит указанному объявлению")
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
                         order_line_id,legacy_owner_id,units,state)
                    VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active')
                    """,
                    (
                        allocation_id, observation_id, resource.game_id, resource.app_id,
                        resource.context_id, resource.bot_name, resource.asset_id,
                        asset_id_key(resource.asset_id),
                        listing_id, config_version_id, take,
                    ),
                )
                allocation_ids.append(allocation_id)
            placeholders = ",".join("?" for _ in allocation_ids)
            return [
                dict(row) for row in connection.execute(
                    f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({placeholders}) ORDER BY allocation_id",
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
    ) -> list[dict[str, Any]]:
        transfer_event_id = str(transfer_event_id or "").strip()
        if not all((transfer_event_id, listing_id, config_version_id, order_line_id)):
            raise ValueError("transfer_event_id, listing_id, config_version_id и order_line_id обязательны")
        units = _positive_int(units, "units")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM gd_stock_transfers WHERE transfer_event_id=?",
                (transfer_event_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["listing_id"] != listing_id
                    or prior["config_version_id"] != config_version_id
                    or prior["order_line_id"] != order_line_id
                    or int(prior["units"]) != units
                ):
                    raise ValueError("transfer_event_id уже использован с другими параметрами")
                ids = json.loads(prior["allocation_ids_json"])
                if not ids:
                    return []
                marks = ",".join("?" for _ in ids)
                return [
                    dict(row) for row in connection.execute(
                        f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({marks}) ORDER BY allocation_id",
                        ids,
                    ).fetchall()
                ]

            line = connection.execute(
                "SELECT quantity,config_version_id FROM gd_order_lines WHERE order_line_id=?",
                (order_line_id,),
            ).fetchone()
            if line is None or line["config_version_id"] != config_version_id:
                raise ValueError("OrderLine отсутствует или относится к другой ConfigVersion")
            already_transferred = int(connection.execute(
                "SELECT COALESCE(SUM(units),0) FROM gd_stock_transfers WHERE order_line_id=?",
                (order_line_id,),
            ).fetchone()[0])
            if already_transferred + units > int(line["quantity"]):
                raise ValueError("Перенос превышает количество OrderLine")

            sources = connection.execute(
                """
                SELECT * FROM gd_stock_allocations
                WHERE owner_type='listing' AND listing_id=? AND config_version_id=?
                  AND state='active'
                ORDER BY bot_name COLLATE NOCASE,game_id,app_id,context_id,asset_id_key,allocation_id
                """,
                (listing_id, config_version_id),
            ).fetchall()
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
                             order_line_id,legacy_owner_id,units,state)
                        VALUES(?,?,?,?,?,?,?,?, 'listing',?,?,NULL,NULL,?,'active')
                        """,
                        (
                            remainder_id, source["observation_id"], source["game_id"],
                            source["app_id"], source["context_id"], source["bot_name"],
                            source["asset_id"], source["asset_id_key"], listing_id,
                            config_version_id, source_units - take,
                        ),
                    )
                order_id = f"order-transfer:{event_hash}:{index}"
                connection.execute(
                    """
                    INSERT INTO gd_stock_allocations
                        (allocation_id,observation_id,game_id,app_id,context_id,bot_name,
                         asset_id,asset_id_key,owner_type,listing_id,config_version_id,
                         order_line_id,legacy_owner_id,units,state)
                    VALUES(?,?,?,?,?,?,?,?, 'order',NULL,NULL,?,NULL,?,'active')
                    """,
                    (
                        order_id, source["observation_id"], source["game_id"],
                        source["app_id"], source["context_id"], source["bot_name"],
                        source["asset_id"], source["asset_id_key"], order_line_id, take,
                    ),
                )
                transfer_ids.append(order_id)
                remaining -= take
            if remaining:
                raise InsufficientListingStock(units, units - remaining)
            connection.execute(
                """
                INSERT INTO gd_stock_transfers
                    (transfer_event_id,listing_id,config_version_id,order_line_id,units,allocation_ids_json)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    transfer_event_id, listing_id, config_version_id, order_line_id,
                    units, json.dumps(transfer_ids, separators=(",", ":")),
                ),
            )
            marks = ",".join("?" for _ in transfer_ids)
            return [
                dict(row) for row in connection.execute(
                    f"SELECT * FROM gd_stock_allocations WHERE allocation_id IN ({marks}) ORDER BY allocation_id",
                    transfer_ids,
                ).fetchall()
            ]
