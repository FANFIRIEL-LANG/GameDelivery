from __future__ import annotations

import sqlite3
import hashlib
from dataclasses import dataclass


class PlayerokDeliveryBlocked(ValueError):
    """A Playerok deal is not eligible for delivery from its persisted snapshot."""


@dataclass(frozen=True)
class PlayerokPaidPreparation:
    account_user_id: str
    external_deal_id: str
    deal_id: str
    listing_id: str
    config_version_id: str
    order_id: str
    binding_id: str
    operator_id: str
    reason: str


def load_playerok_paid_preparation(
    connection: sqlite3.Connection, account_user_id: str, external_deal_id: str
) -> PlayerokPaidPreparation:
    account = str(account_user_id or "").strip()
    external = str(external_deal_id or "").strip()
    if not account or not external:
        raise ValueError("Account ID и Deal ID обязательны")
    row = connection.execute(
        "SELECT d.deal_id,d.listing_id,d.config_version_id,d.platform_status,d.is_historical,"
        "d.listing_resolution_status,b.binding_id,b.config_version_id AS bound_config_id,"
        "b.listing_id AS bound_listing_id,b.operator_id,b.reason,s.baseline_completed "
        "FROM gd_deals d JOIN gd_config_versions c ON c.config_version_id=d.config_version_id "
        "AND c.listing_id=d.listing_id LEFT JOIN playerok_paid_config_bindings b "
        "ON b.account_user_id=d.playerok_account_id AND b.deal_id=d.deal_id "
        "LEFT JOIN playerok_sync_state s ON s.account_user_id=d.playerok_account_id "
        "WHERE d.source_platform='playerok' AND d.playerok_account_id=? AND d.external_deal_id=?",
        (account, external),
    ).fetchone()
    if row is None:
        raise KeyError("Playerok Deal не найден для указанного аккаунта")
    if str(row["platform_status"] or "").upper() != "PAID" or row["is_historical"] != 0:
        raise PlayerokDeliveryBlocked("Подготовка разрешена только для актуальной PAID-сделки.")
    if row["baseline_completed"] != 1:
        raise PlayerokDeliveryBlocked("Полный baseline Playerok для аккаунта ещё не завершён.")
    listing = str(row["listing_id"] or "")
    config = str(row["config_version_id"] or "")
    if row["listing_resolution_status"] != "resolved" or not listing or not config:
        raise PlayerokDeliveryBlocked("Сделка не сопоставлена с точным объявлением и ConfigVersion.")
    if (
        not row["binding_id"] or row["bound_config_id"] != config
        or row["bound_listing_id"] != listing
    ):
        raise PlayerokDeliveryBlocked("Для сделки нет точной аудированной привязки ConfigVersion.")
    order_id = "playerok-order:" + hashlib.sha256(
        (account + "\x1f" + external).encode("utf-8")
    ).hexdigest()[:32]
    return PlayerokPaidPreparation(
        account, external, str(row["deal_id"]), listing, config, order_id,
        str(row["binding_id"]), str(row["operator_id"]), str(row["reason"]),
    )


def create_playerok_order_for_preparation(
    connection: sqlite3.Connection,
    context: PlayerokPaidPreparation,
    total_quantity: int,
) -> None:
    """Create the deterministic order shell only inside its preparation transaction."""
    if not connection.in_transaction:
        raise RuntimeError("Playerok order preparation requires an active transaction")
    current = load_playerok_paid_preparation(
        connection, context.account_user_id, context.external_deal_id
    )
    if current != context:
        raise PlayerokDeliveryBlocked("Сделка или аудированная ConfigVersion изменилась после планирования.")
    if type(total_quantity) is not int or total_quantity < 1:
        raise ValueError("Количество заказа должно быть положительным целым числом")
    connection.execute(
        "INSERT OR IGNORE INTO gd_orders(order_id,deal_id,status,total_quantity) "
        "VALUES(?,?,'pending',?)", (context.order_id, context.deal_id, total_quantity),
    )
    row = connection.execute(
        "SELECT deal_id,total_quantity FROM gd_orders WHERE order_id=?", (context.order_id,)
    ).fetchone()
    if row is None or row["deal_id"] != context.deal_id or row["total_quantity"] != total_quantity:
        raise PlayerokDeliveryBlocked("Существующий заказ не совпадает с подготовленным контекстом сделки.")


def _is_playerok(source_platform: object) -> bool:
    return isinstance(source_platform, str) and source_platform.strip().casefold() == "playerok"


def _require_paid_status(status: object) -> None:
    if not isinstance(status, str) or status != "PAID":
        raise PlayerokDeliveryBlocked(
            "Подготовка и отправка сделки Playerok разрешены только при подтверждённом статусе PAID."
        )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def require_legacy_playerok_delivery_eligible(
    connection: sqlite3.Connection, order_id: str
) -> None:
    """Fail closed for every Playerok legacy order; manual orders keep old behavior."""
    columns = _table_columns(connection, "delivery_orders")
    if "source_platform" not in columns:
        # Pre-source legacy schemas contain only manual orders.
        return
    row = connection.execute(
        "SELECT source_platform "
        "FROM delivery_orders WHERE order_id=?",
        (str(order_id).strip(),),
    ).fetchone()
    if row is None or not _is_playerok(row["source_platform"]):
        return
    raise PlayerokDeliveryBlocked(
        "Legacy-путь Playerok отключён: подготовка разрешена только для канонического заказа "
        "с подтверждённой ConfigVersion."
    )


def require_canonical_playerok_delivery_eligible(
    connection: sqlite3.Connection, order_id: str
) -> None:
    """Check canonical platform status and sticky baseline state on the same DB transaction."""
    order_columns = _table_columns(connection, "gd_orders")
    deal_columns = _table_columns(connection, "gd_deals")
    if not {"order_id", "deal_id"}.issubset(order_columns) or not {
        "deal_id", "source_platform", "platform_status"
    }.issubset(deal_columns):
        raise PlayerokDeliveryBlocked("Каноническая запись доставки отсутствует или повреждена.")
    row = connection.execute(
        "SELECT d.deal_id,d.source_platform,d.platform_status "
        "FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id WHERE o.order_id=?",
        (str(order_id).strip(),),
    ).fetchone()
    if row is None or not _is_playerok(row["source_platform"]):
        return
    _require_paid_status(row["platform_status"])
    required_playerok_columns = {
        "playerok_account_id", "listing_id", "config_version_id",
        "listing_resolution_status", "is_historical",
    }
    if not required_playerok_columns.issubset(deal_columns):
        raise PlayerokDeliveryBlocked("Состояние аккаунта, объявления или baseline неизвестно.")
    if "playerok_sync_state" not in {
        str(item[0]) for item in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }:
        raise PlayerokDeliveryBlocked("Состояние baseline Playerok неизвестно.")
    playerok = connection.execute(
        "SELECT playerok_account_id,listing_id,config_version_id,listing_resolution_status,is_historical "
        "FROM gd_deals WHERE deal_id=?", (row["deal_id"],),
    ).fetchone()
    account_id = str(playerok["playerok_account_id"] or "").strip()
    if (
        not account_id or playerok["listing_id"] is None or playerok["config_version_id"] is None
        or playerok["listing_resolution_status"] != "resolved"
    ):
        raise PlayerokDeliveryBlocked(
            "Сделка Playerok не сопоставлена с точным объявлением и ConfigVersion."
        )
    sync_state = connection.execute(
        "SELECT baseline_completed FROM playerok_sync_state WHERE account_user_id=?",
        (account_id,),
    ).fetchone()
    if sync_state is None or sync_state[0] != 1:
        raise PlayerokDeliveryBlocked("Полный baseline Playerok для аккаунта ещё не завершён.")
    historical = playerok["is_historical"]
    if type(historical) is not int or historical != 0:
        raise PlayerokDeliveryBlocked(
            "Историческая или неизвестная сделка Playerok не допускается к подготовке и отправке."
        )
