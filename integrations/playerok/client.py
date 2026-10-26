"""Playerok adapter.

Playerok's documented public developer API is not available; this adapter uses
the third-party PlayerokAPI client. Keep its implementation isolated here.
Session tokens are kept in the OS keyring and never written to GameDelivery's DB.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from integrations.marketplaces import MarketplaceAdapter, MarketplaceInfo

KEYRING_SERVICE = "GameDelivery"
KEYRING_ACCOUNT = "playerok.session"


class PlayerokAdapter(MarketplaceAdapter):
    info = MarketplaceInfo(
        platform_id="playerok",
        display_name="Playerok",
        website_url="https://playerok.com/",
        implemented=True,
    )

    def __init__(
        self,
        *,
        account_factory: Callable[..., Any] | None = None,
        keyring_backend: Any | None = None,
    ) -> None:
        self._account_factory = account_factory
        self._keyring_backend = keyring_backend
        self._session_token: str | None = None
        self._pending_second_factor_session: Any | None = None

    def _account_type(self) -> Callable[..., Any]:
        if self._account_factory is not None:
            return self._account_factory
        try:
            from PlayerokAPI import Account
        except ImportError as exc:
            raise RuntimeError(
                "Клиент Playerok не установлен. Установи зависимости GameDelivery."
            ) from exc
        return Account

    def _keyring(self):
        if self._keyring_backend is not None:
            return self._keyring_backend
        import keyring
        return keyring

    @staticmethod
    def _run(coroutine):
        return asyncio.run(coroutine)

    @staticmethod
    def _username(profile: Any) -> str:
        return str(
            getattr(profile, "username", None)
            or getattr(profile, "nickname", None)
            or getattr(profile, "name", None)
            or "Аккаунт Playerok"
        )

    @staticmethod
    def _profile_result(profile: Any, session_saved: bool) -> dict[str, Any]:
        return {
            "connected": True,
            "username": PlayerokAdapter._username(profile),
            "user_id": str(getattr(profile, "id", "") or ""),
            "session_saved": session_saved,
        }

    def _saved_token(self) -> str | None:
        if self._session_token:
            return self._session_token
        try:
            token = self._keyring().get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            token = None
        if token:
            self._session_token = token
        return token

    def _save_token(self, token: str | None) -> bool:
        if not token:
            raise RuntimeError("Playerok не вернул токен сессии после входа.")
        self._session_token = str(token)
        try:
            self._keyring().set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, self._session_token)
            return True
        except Exception:
            # Keep the session usable for this run but clearly report non-persistence.
            return False

    def request_login_code(self, email: str) -> dict[str, Any]:
        address = email.strip()
        if not address or "@" not in address or address.startswith("@") or address.endswith("@"):
            raise ValueError("Укажи корректный адрес электронной почты.")

        async def operation():
            account = self._account_type()()
            try:
                await account.auth.send_otp(address)
            finally:
                await account.aclose()

        self._run(operation())
        return {"sent": True, "email": address}

    def confirm_login_code(self, email: str, code: str) -> dict[str, Any]:
        address, otp = email.strip(), code.strip()
        if not address or not otp:
            raise ValueError("Укажи e-mail и код из письма.")

        async def operation():
            account = self._account_type()()
            try:
                result = await account.auth.confirm_otp(address, otp)
                if bool(getattr(result, "requires_two_factor", False)):
                    self._pending_second_factor_session = (
                        getattr(result, "second_factor_token", None) or result
                    )
                    return {"requires_second_factor": True}
                token = getattr(account, "token", None) or getattr(result, "token", None)
                if token and not getattr(account, "token", None):
                    account.token = token
                if not getattr(account, "token", None):
                    raise RuntimeError("Playerok не подтвердил вход: токен сессии отсутствует.")
                profile = await account.get_me()
                saved = self._save_token(account.token)
                self._pending_second_factor_session = None
                return self._profile_result(profile, saved)
            finally:
                await account.aclose()

        return self._run(operation())

    def confirm_second_factor(self, code: str) -> dict[str, Any]:
        otp = code.strip()
        session = self._pending_second_factor_session
        if not session:
            raise ValueError("Сначала подтверди код из письма Playerok.")
        if not otp:
            raise ValueError("Введи код двухфакторной аутентификации.")

        async def operation():
            account = self._account_type()()
            try:
                token = await account.auth.confirm_second_factor(session, otp)
                account.token = token
                profile = await account.get_me()
                saved = self._save_token(token)
                self._pending_second_factor_session = None
                return self._profile_result(profile, saved)
            finally:
                await account.aclose()

        return self._run(operation())

    def restore_session(self) -> dict[str, Any]:
        token = self._saved_token()
        if not token:
            raise ValueError("Сохранённая сессия Playerok не найдена. Выполни вход по e-mail.")
        async def operation():
            account = self._account_type()(token=token)
            try:
                profile = await account.get_me()
                return self._profile_result(profile, True)
            finally:
                await account.aclose()
        return self._run(operation())

    def disconnect(self) -> dict[str, Any]:
        token = self._saved_token()
        remote_error = None
        if token:
            async def operation():
                account = self._account_type()(token=token)
                try:
                    await account.auth.logout()
                finally:
                    await account.aclose()
            try:
                self._run(operation())
            except Exception as exc:
                remote_error = str(exc)
        self._session_token = None
        self._pending_second_factor_session = None
        try:
            self._keyring().delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            pass
        return {
            "connected": False,
            "local_session_cleared": True,
            "message": (
                "Локальная сессия удалена. Playerok не подтвердил удалённый выход: " + remote_error
                if remote_error else "Выход выполнен, локальная сессия удалена."
            ),
        }

    def fetch_orders(self) -> list[dict[str, Any]]:
        token = self._saved_token()
        if not token:
            raise ValueError("Сначала войди в аккаунт Playerok.")

        async def operation():
            account = self._account_type()(token=token)
            try:
                page = await account.deals.search(
                    filter={"direction": "OUT", "status": ["PAID", "SENT"]},
                    first=50,
                )
                result = []
                for deal in page:
                    item = getattr(deal, "item", None)
                    buyer = getattr(deal, "user", None)
                    transaction = getattr(deal, "transaction", None)
                    status = getattr(deal, "status", None)
                    status = getattr(status, "value", status)
                    created = getattr(deal, "created_at", None)
                    result.append({
                        "platform": "playerok",
                        "order_id": str(getattr(deal, "id", "") or ""),
                        "title": str(getattr(item, "name", None) or "Товар Playerok"),
                        "buyer": str(
                            getattr(buyer, "username", None)
                            or getattr(buyer, "name", None)
                            or "—"
                        ),
                        "status": str(status or "—"),
                        "amount": str(getattr(transaction, "value", None) or "—"),
                        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
                        "url": str(getattr(deal, "url", None) or ""),
                    })
                return result
            finally:
                await account.aclose()

        return self._run(operation())
