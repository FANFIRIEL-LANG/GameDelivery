from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


class SteamTradeCredentialStore:
    """Keep Steam trade tokens in the OS credential vault, never in SQLite."""

    SERVICE = "GameDelivery.SteamTradeURL"
    _TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$", re.ASCII)
    _PARTNER_PATTERN = re.compile(r"^[0-9]{1,10}$", re.ASCII)

    def __init__(self, *, backend: Any | None = None) -> None:
        if backend is None:
            import keyring

            backend = keyring
        self._backend = backend

    @classmethod
    def _parts(cls, trade_url: str, *, require_token: bool) -> tuple[str, str | None]:
        value = str(trade_url or "").strip()
        parsed = urlsplit(value)
        params = parse_qs(parsed.query, keep_blank_values=True)
        partners = params.get("partner", [])
        tokens = params.get("token", [])
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}
            or parsed.path.rstrip("/") != "/tradeoffer/new"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or len(partners) != 1
            or cls._PARTNER_PATTERN.fullmatch(partners[0]) is None
            or not 1 <= int(partners[0]) <= 4294967295
            or len(tokens) > 1
            or (require_token and len(tokens) != 1)
        ):
            raise ValueError("Укажи корректную HTTPS Steam Trade URL для этого заказа.")
        partner = str(int(partners[0]))
        token = tokens[0] if tokens else None
        if token is not None and cls._TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("Steam Trade URL содержит некорректный token.")
        return partner, token

    @staticmethod
    def _safe_url(partner: str) -> str:
        query = urlencode({"partner": partner})
        return urlunsplit(("https", "steamcommunity.com", "/tradeoffer/new/", query, ""))

    @staticmethod
    def _account(order_id: str) -> str:
        order = str(order_id or "").strip()
        if not order or len(order) > 128 or any(ord(char) < 32 for char in order):
            raise ValueError("Некорректный ID заказа для системного хранилища Steam.")
        return order

    def _save_token(self, order_id: str, token: str) -> None:
        account = self._account(order_id)
        try:
            self._backend.set_password(self.SERVICE, account, token)
            saved = self._backend.get_password(self.SERVICE, account)
            if saved != token:
                raise RuntimeError("Credential vault did not verify the saved value")
        except Exception:
            try:
                self._backend.delete_password(self.SERVICE, account)
            except Exception:
                pass
            raise OSError(
                "Не удалось сохранить Steam trade token в системном хранилище учётных данных."
            ) from None

    def store_trade_url(self, order_id: str, trade_url: str) -> str:
        partner, token = self._parts(trade_url, require_token=True)
        assert token is not None
        self._save_token(order_id, token)
        return self._safe_url(partner)

    def migrate_legacy_url(self, order_id: str, trade_url: str) -> str:
        """Move an old plaintext URL into the OS vault before SQLite is updated."""
        partner, token = self._parts(trade_url, require_token=False)
        if token is not None:
            self._save_token(order_id, token)
        return self._safe_url(partner)

    def load_trade_url(self, order_id: str, stored_url: str) -> str:
        partner, embedded_token = self._parts(stored_url, require_token=False)
        account = self._account(order_id)
        if embedded_token is not None:
            raise ValueError(
                "Старая Steam Trade URL ещё не перенесена в системное хранилище. "
                "Перезапусти миграцию заказов перед проверкой."
            )
        try:
            token = self._backend.get_password(self.SERVICE, account)
        except Exception:
            raise OSError(
                "Не удалось прочитать Steam trade token из системного хранилища учётных данных."
            ) from None
        if not token or self._TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError(
                "Steam trade token для этого заказа не найден в системном хранилище. "
                "Открой заказ и заново укажи Trade URL."
            )
        return self._safe_url(partner) + "&" + urlencode({"token": token})

    def delete_trade_url(self, order_id: str) -> None:
        account = self._account(order_id)
        try:
            token = self._backend.get_password(self.SERVICE, account)
            if token is not None:
                self._backend.delete_password(self.SERVICE, account)
        except Exception:
            raise OSError(
                "Не удалось удалить Steam trade token из системного хранилища учётных данных."
            ) from None
