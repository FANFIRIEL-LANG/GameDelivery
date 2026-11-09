"""Shared visual primitives for read-only inventory cards and detail tables."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QColor, QIcon, QPixmap, QTextDocument
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QStyledItemDelegate, QLabel, QStyle, QStyleOptionViewItem, QTableWidget,
    QTableWidgetItem,
)

from games.inventory_display import InventoryMetrics, PRICE_UNAVAILABLE


METRIC_COLORS = {
    "total": "#f3f4f6",
    "non_tradable": "#f87171",
    "owners": "#aeb9c8",
    "tradable": "#4ade80",
    "unknown": "#fbbf24",
}


def metrics_html(metrics: InventoryMetrics, *, include_price: bool = True) -> str:
    fields = (
        ("total", "Всего", metrics.total),
        ("non_tradable", "Непередаваемые", metrics.non_tradable),
        ("owners", "Аккаунтов", metrics.owner_accounts),
        ("tradable", "Передаваемые", metrics.tradable),
        ("unknown", "Неизвестный статус", metrics.unknown_status),
    )
    spans = " · ".join(
        f"<span style='color:{METRIC_COLORS[key]}'>{label}: {value}</span>"
        for key, label, value in fields
    )
    price = f"<br><span style='color:{METRIC_COLORS['owners']}'>{PRICE_UNAVAILABLE}</span>" if include_price else ""
    return f"{spans}{price}"


class InventoryMetricsLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setWordWrap(True)
        self.setObjectName("inventoryMetrics")

    def set_metrics(self, metrics: InventoryMetrics):
        self.setText(metrics_html(metrics))


class InventoryCardDelegate(QStyledItemDelegate):
    """Paint metric-bearing selectable cards without replacing QListWidget items."""

    def sizeHint(self, option, index):
        return QSize(176, 184)

    def paint(self, painter, option, index):
        background = QStyleOptionViewItem(option)
        background.text = ""
        background.icon = QIcon()
        metrics = index.data(Qt.ItemDataRole.UserRole + 3)
        if isinstance(metrics, InventoryMetrics):
            if metrics.non_tradable == metrics.total and metrics.total:
                background.palette.setColor(background.palette.ColorRole.Base, QColor("#292b30"))
            elif metrics.unknown_status == metrics.total and metrics.total:
                background.palette.setColor(background.palette.ColorRole.Base, QColor("#292936"))
        option.widget.style().drawControl(
            QStyle.ControlElement.CE_ItemViewItem, background, painter, option.widget
        )

        rect = option.rect.adjusted(4, 3, -4, -3)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, rect.x() + (rect.width() - 64) // 2, rect.y(), 64, 58)
        title_rect = rect.adjusted(3, 60, -3, -rect.height() + 91)
        painter.save()
        painter.setPen(option.palette.color(option.palette.ColorRole.Text))
        painter.drawText(title_rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap),
                         index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.restore()
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setPlainText(PRICE_UNAVAILABLE)
        document.setTextWidth(rect.width())
        painter.save()
        painter.translate(rect.x(), rect.y() + 91)
        document.drawContents(painter)
        painter.restore()


class SteamInventoryImageLoader:
    """One Steam CDN loader and fallback policy shared by both inventory tabs."""

    STEAM_IMAGE_HOSTS = (
        "community.cloudflare.steamstatic.com",
        "community.akamai.steamstatic.com",
        "steamcommunity-a.akamaihd.net",
        "community.steamstatic.com",
    )

    def __init__(self, parent, on_loaded):
        self.manager = QNetworkAccessManager(parent)
        self.manager.finished.connect(on_loaded)
        self.cache: dict[str, QPixmap] = {}
        self.pending: set[str] = set()
        self.attempted: dict[str, set[str]] = {}
        self.request_origin: dict[str, str] = {}

    @classmethod
    def steam_image_candidates(cls, url: str) -> list[str]:
        path = QUrl(url).path()
        if not path:
            return []
        return [
            candidate for host in cls.STEAM_IMAGE_HOSTS
            if (candidate := QUrl(f"https://{host}{path}").toString()) != url
        ]

    def request(self, url: str, origin: str | None = None):
        if not url or url in self.cache or url in self.pending:
            return
        origin = origin or url
        attempted = self.attempted.setdefault(origin, set())
        if url in attempted:
            return
        attempted.add(url)
        self.request_origin[url] = origin
        self.pending.add(url)
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", b"Mozilla/5.0 GameDelivery/1.0")
        request.setTransferTimeout(15000)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        self.manager.get(request)

    def handle_reply(self, reply: QNetworkReply):
        url = reply.request().url().toString()
        origin = self.request_origin.pop(url, url)
        self.pending.discard(url)
        pixmap = QPixmap()
        if reply.error() == QNetworkReply.NetworkError.NoError:
            pixmap.loadFromData(bytes(reply.readAll()))
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error_text = reply.errorString()
        reply.deleteLater()
        if pixmap.isNull():
            for fallback in self.steam_image_candidates(origin):
                if fallback not in self.attempted.setdefault(origin, set()):
                    self.request(fallback, origin)
                    return
            self._log_failure(origin, status, error_text)
            return
        self.cache[origin] = pixmap
        self.cache[url] = pixmap

    @staticmethod
    def _log_failure(url: str, status, error_text: str):
        try:
            log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GameDelivery" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "image-download.log").open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{datetime.now(timezone.utc).isoformat()} | "
                    f"HTTP={status} | {error_text} | {url}\n"
                )
        except OSError:
            pass


def metric_table_cells(metrics: InventoryMetrics) -> tuple[QTableWidgetItem, ...]:
    values = (
        (metrics.total, "total"),
        (metrics.non_tradable, "non_tradable"),
        (metrics.owner_accounts, "owners"),
        (metrics.tradable, "tradable"),
        (metrics.unknown_status, "unknown"),
    )
    result = []
    for value, key in values:
        item = QTableWidgetItem(str(value))
        item.setForeground(QColor(METRIC_COLORS[key]))
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        result.append(item)
    return tuple(result)


def thumbnail_table_item(url: str, icon: QIcon | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem()
    item.setData(Qt.ItemDataRole.UserRole, url)
    item.setIcon(icon if icon is not None else QIcon())
    return item


def update_table_thumbnails(table: QTableWidget, *, column: int, url: str, icon: QIcon):
    if icon.isNull():
        return
    for row in range(table.rowCount()):
        item = table.item(row, column)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == url:
            item.setIcon(icon)

