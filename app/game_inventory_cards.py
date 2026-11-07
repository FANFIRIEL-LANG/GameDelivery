from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout


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


class GameItemCard(QFrame):
    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("gameItemCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        color = rarity_color(item.rarity)
        self.setStyleSheet(
            f"QFrame#gameItemCard {{ background: #202631; border: 2px solid {color}; "
            "border-radius: 10px; }"
            "QLabel { color: #e8edf5; border: none; background: transparent; }"
        )
        self.setFixedSize(184, 218)
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
        rarity = QLabel(item.rarity or "Редкость не указана")
        rarity.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rarity.setStyleSheet(f"color: {color}; font-weight: 700;")
        layout.addWidget(rarity)
        quality = item.exterior or item.quality or "Качество не указано"
        stattrak = stattrak_label(item)
        summary = QLabel(f"{quality} · StatTrak: {stattrak}")
        summary.setWordWrap(True)
        summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        summary.setStyleSheet("color: #aeb9c8; font-size: 9pt;")
        layout.addWidget(summary)
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
            ("Можно продать", marketable), ("Asset ID", item.asset_id),
        )
        body = "".join(
            f"<tr><td style='color:#9daabd;padding:2px 8px 2px 0'>{key}</td>"
            f"<td style='padding:2px 0'><b>{escape(str(value))}</b></td></tr>"
            for key, value in rows
        )
        return f"<table>{body}</table>"

    def set_image(self, pixmap: QPixmap):
        self.image.setText("")
        self.image.setPixmap(pixmap.scaled(
            148, 100, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class GameItemImageLoader:
    """Steam CDN image loader with the same fallback and diagnostics as Steam cards."""

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
        parsed = QUrl(url)
        path = parsed.path()
        if not path:
            return []
        return [
            candidate for host in cls.STEAM_IMAGE_HOSTS
            if (candidate := QUrl("https://" + host + path).toString()) != url
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
                    f"HTTP={status} | {error_text} | {url}\\n"
                )
        except OSError:
            pass
