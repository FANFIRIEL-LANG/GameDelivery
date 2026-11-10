from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QVBoxLayout,
)

from app.inventory_display import SteamInventoryImageLoader


RARITY_COLORS = {
    "consumer grade": "#8b929a", "base grade": "#8b929a", "common": "#8b929a",
    "industrial grade": "#5e98d9", "uncommon": "#5e98d9",
    "mil-spec grade": "#4b69ff", "rare": "#4b69ff",
    "restricted": "#8847ff", "mythical": "#8847ff",
    "classified": "#d32ce6", "legendary": "#d32ce6",
    "covert": "#eb4b4b", "ancient": "#eb4b4b",
    "extraordinary": "#e4ae39", "immortal": "#e4ae39",
    "contraband": "#e4ae39", "arcana": "#ade55c",
}


def rarity_color(rarity: str) -> str:
    key = (rarity or "").strip().casefold()
    return next((color for label, color in RARITY_COLORS.items() if label in key), "#596273")


def stattrak_label(item) -> str:
    if item.game_id != "cs2":
        return "Не применяется"
    text = " ".join((item.name, item.market_hash_name, *item.tags)).casefold()
    if "stattrak" in text or "stat trak" in text:
        return "Да"
    return "Нет" if item.tags or item.market_hash_name else "Неизвестно"


def format_restriction_countdown(release_at: datetime | None, *, now: datetime | None = None) -> str:
    """Format a timezone-aware explicit release time; never infer one from restrictions."""
    if not isinstance(release_at, datetime) or release_at.tzinfo is None or release_at.utcoffset() is None:
        return "Срок неизвестен"
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return "Срок неизвестен"
    seconds = (release_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    if seconds <= 0:
        return "Срок истёк · обновите снимок ASF"
    minutes = max(1, int((seconds + 59) // 60))
    days, remaining = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remaining, 60)
    if days:
        return f"Обмен через {days} д." + (f" {hours} ч." if hours else "")
    if hours:
        return f"Обмен через {hours} ч." + (f" {minutes} мин." if minutes else "")
    return f"Обмен через {minutes} мин."


class GameItemCard(QFrame):
    def __init__(self, item, parent=None, *, instances=None):
        super().__init__(parent)
        self.item = item
        self.instances = tuple(instances or (item,))
        self.setObjectName("gameItemCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        color = rarity_color(item.rarity)
        self.setStyleSheet(
            f"QFrame#gameItemCard {{ background: #202631; border: 2px solid {color}; "
            "border-radius: 10px; }"
            "QLabel { color: #e8edf5; border: none; background: transparent; }"
        )
        self.setFixedSize(184, 252)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(5)
        self.image = QLabel("Нет изображения")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setFixedHeight(104)
        self.image.setStyleSheet("color: #8792a2;")
        layout.addWidget(self.image)
        title = QLabel(item.name)
        title.setWordWrap(True)
        title.setMaximumHeight(38)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        quality = item.exterior or item.quality
        if quality:
            quality_label = QLabel(quality)
            quality_label.setObjectName("quality")
            quality_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            quality_label.setStyleSheet("color: #aeb9c8; font-size: 9pt;")
            layout.addWidget(quality_label)
        if stattrak_label(item) == "Да":
            stattrak = QLabel("StatTrak")
            stattrak.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(stattrak)
        quantity = QLabel(f"Количество: {len(self.instances)}")
        quantity.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(quantity)
        price = QLabel("Цена недоступна")
        price.setAlignment(Qt.AlignmentFlag.AlignCenter)
        price.setObjectName("muted")
        layout.addWidget(price)
        self.setToolTip(self._tooltip())

    def _tooltip(self) -> str:
        item = self.item
        quality = item.exterior or item.quality or "Не указано"
        stattrak = stattrak_label(item)
        tradable = "Да" if item.is_tradable is True else "Нет" if item.is_tradable is False else "Не проверено"
        marketable = "Да" if item.is_marketable is True else "Нет" if item.is_marketable is False else "Не проверено"
        rows = (
            ("Аккаунт / бот", item.bot_name), ("Игра", item.game_id.upper()),
            ("Предмет", item.name), ("Качество / износ", quality),
            ("StatTrak", stattrak), ("Редкость", item.rarity or "Не указана"),
            ("Тип", item.item_type or "Не указан"), ("Передаваемый", tradable),
            ("Можно продать", marketable),
            ("Market hash", item.market_hash_name or "Неизвестно"),
            ("Контекст", f"{getattr(item, 'appid', None) or 'Неизвестно'}/"
                         f"{getattr(item, 'context_id', None) or 'Неизвестно'}"),
            ("Характеристики", ", ".join(item.tags) or "Неизвестно"),
        )
        body = "".join(
            f"<tr><td style='color:#9daabd;padding:2px 8px 2px 0'>{key}</td>"
            f"<td style='padding:2px 0'><b>{escape(str(value))}</b></td></tr>"
            for key, value in rows
        )
        owners = "\n".join(
            f"{bot}: " + ", ".join(instance.asset_id for instance in members)
            for bot, members in sorted(
                self._items_by_bot().items(), key=lambda pair: pair[0].casefold()
            )
        )
        return f"<table>{body}</table><br>Владельцы / asset ID:<br>{escape(owners)}"

    def _items_by_bot(self):
        owners = {}
        for instance in self.instances:
            owners.setdefault(instance.bot_name, []).append(instance)
        return owners

    def set_image(self, pixmap: QPixmap):
        self.image.setText("")
        self.image.setPixmap(pixmap.scaled(
            148, 100, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class CS2InventoryGroupCard(QFrame):
    """Compact CS2 inventory card with a user-facing item-only tooltip."""

    def __init__(self, group, parent=None, *, rarity: str = "",
                 price_text: str = "Цена недоступна"):
        super().__init__(parent)
        first = group.instances[0]
        color = rarity_color(rarity or first.rarity)
        self.group = group
        self.setObjectName("gameItemCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            f"QFrame#gameItemCard {{ background: #202631; border: 2px solid {color}; "
            "border-radius: 10px; }"
            "QLabel { color: #e8edf5; border: none; background: transparent; }"
        )
        self.setFixedSize(184, 246)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(3)

        self.image = QLabel("Нет изображения")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setFixedHeight(88)
        self.image.setStyleSheet("color: #8792a2;")
        layout.addWidget(self.image)

        title = QLabel(first.name or "Название неизвестно")
        title.setWordWrap(True)
        title.setMaximumHeight(34)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        quality = first.exterior or first.quality
        if quality:
            quality_label = QLabel(quality)
            quality_label.setObjectName("quality")
            quality_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            quality_label.setStyleSheet("color: #aeb9c8; font-size: 9pt;")
            layout.addWidget(quality_label)

        if stattrak_label(first) == "Да":
            stattrak = QLabel("StatTrak")
            stattrak.setObjectName("stattrak")
            stattrak.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stattrak.setStyleSheet("color: #e4ae39; font-weight: 600;")
            layout.addWidget(stattrak)

        count = QLabel(f"Количество: {group.instance_count}")
        count.setObjectName("quantity")
        count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(count)

        price = QLabel(price_text)
        price.setObjectName("price")
        price.setWordWrap(True)
        price.setAlignment(Qt.AlignmentFlag.AlignCenter)
        price.setMaximumHeight(42)
        price.setStyleSheet("font-size: 8pt;")
        layout.addWidget(price)
        self.setToolTip(self._tooltip_text())

    def _tooltip_text(self):
        first = self.group.instances[0]
        lines = [first.name or "Название неизвестно"]
        quality = first.exterior or first.quality
        if quality:
            lines.append(f"Качество / износ: {quality}")
        if first.rarity:
            lines.append(f"Редкость: {first.rarity}")
        if stattrak_label(first) == "Да":
            lines.append("StatTrak")
        if first.tags:
            lines.append(f"Характеристики: {', '.join(first.tags)}")
        return "\n".join(lines)

    def set_image(self, pixmap: QPixmap):
        self.image.setText("")
        self.image.setPixmap(pixmap.scaled(
            164, 84, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


GameItemImageLoader = SteamInventoryImageLoader
