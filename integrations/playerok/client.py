"""Direct, read-only Playerok connector implemented with Python's standard library.

Playerok does not provide a documented public developer API. The endpoints used
here are the website's private endpoints and may change. This module deliberately
does not use third-party marketplace clients and exposes no payment, withdrawal,
item-management, or deal-status mutation operations.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from typing import Any, Callable
from urllib.parse import urlsplit

from integrations.marketplaces import MarketplaceAdapter, MarketplaceInfo

KEYRING_SERVICE = "GameDelivery"
KEYRING_ACCOUNT = "playerok.session"

_PUBLIC = "https://playerok.com/rest-api/public"
_BFF = "https://bff.playerok.com/rest-api/public"
_GRAPHQL = "https://playerok.com/graphql"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_TIMEOUT_SECONDS = 20


def _unwrap_envelope(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("success") is True and "data" in payload:
        return payload["data"]
    return payload


def _token_from_headers(headers: Any) -> str | None:
    """Read only the Playerok token cookie; never log or return other cookies."""
    get_all = getattr(headers, "get_all", None)
    raw_headers = get_all("Set-Cookie") if callable(get_all) else None
    if not raw_headers:
        value = headers.get("Set-Cookie") if headers is not None else None
        raw_headers = [value] if value else []
    for raw in raw_headers:
        try:
            cookies = SimpleCookie()
            cookies.load(raw)
            morsel = cookies.get("token")
            if morsel and morsel.value:
                return morsel.value
        except Exception:
            continue
    return None


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
        keyring_backend: Any | None = None,
        request_func: Callable[..., tuple[Any, str | None]] | None = None,
    ) -> None:
        self._keyring_backend = keyring_backend
        # request_func is a test seam; production uses urllib.request directly.
        self._request_func = request_func
        self._session_token: str | None = None
        self._session_persisted = False
        self._pending_second_factor_session: Any | None = None

    def _keyring(self):
        if self._keyring_backend is not None:
            return self._keyring_backend
        import keyring
        return keyring

    @staticmethod
    def _request_url_is_allowed(url: str) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname in {"playerok.com", "bff.playerok.com"}
            and not parts.username
            and not parts.password
        )

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        token: str | None = None,
        bearer: bool = False,
    ) -> tuple[Any, str | None]:
        if not self._request_url_is_allowed(url):
            raise ValueError("Разрешены только HTTPS-запросы к доменам Playerok.")

        if self._request_func is not None:
            return self._request_func(
                url, method=method, payload=payload, token=token, bearer=bearer
            )

        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
            "Origin": "https://playerok.com",
            "Referer": "https://playerok.com/",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Cookie"] = "token=" + token
            if bearer:
                headers["Authorization"] = "Bearer " + token

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read()
                cookie_token = _token_from_headers(response.headers)
        except urllib.error.HTTPError as exc:
            # Do not include response bodies, request data, tokens, or cookies in errors.
            raise RuntimeError(f"Playerok вернул HTTP {exc.code}.") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            raise RuntimeError(
                "Не удалось установить HTTPS-соединение с Playerok."
                + (f" Причина: {reason}" if reason else "")
            ) from None
        except TimeoutError:
            raise RuntimeError("Истекло время ожидания ответа Playerok.") from None

        if not raw:
            return {}, cookie_token
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("Playerok вернул ответ в неожиданном формате.") from None
        return data, cookie_token

    @staticmethod
    def _raise_api_errors(payload: Any) -> Any:
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                message = first.get("message") if isinstance(first, dict) else None
                safe_message = re.sub(r"[\r\n\t]+", " ", str(message or "неизвестная ошибка"))
                raise RuntimeError("Ошибка API Playerok: " + safe_message[:240])
        return payload

    @staticmethod
    def _session_token_from_payload(payload: Any, cookie_token: str | None) -> str | None:
        payload = _unwrap_envelope(payload)
        if isinstance(payload, dict):
            value = payload.get("token")
            if isinstance(value, str) and value:
                return value
        return cookie_token

    @staticmethod
    def _second_factor_token(session: Any) -> str | None:
        if isinstance(session, str):
            return session or None
        if isinstance(session, dict):
            for key in ("token", "sessionToken", "secondFactorSession", "id"):
                value = session.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _username(profile: Any) -> str:
        if not isinstance(profile, dict):
            return "Аккаунт Playerok"
        return str(
            profile.get("username")
            or profile.get("nickname")
            or profile.get("name")
            or "Аккаунт Playerok"
        )

    def _profile(self, token: str, session_saved: bool) -> dict[str, Any]:
        payload, _ = self._request(_BFF + "/viewer", token=token, bearer=True)
        payload = self._raise_api_errors(_unwrap_envelope(payload))
        if not isinstance(payload, dict):
            raise RuntimeError("Playerok вернул профиль в неожиданном формате.")
        return {
            "connected": True,
            "username": self._username(payload),
            "user_id": str(payload.get("id") or ""),
            "session_saved": session_saved,
        }

    def has_saved_session(self) -> bool:
        return bool(self._saved_token())

    def _saved_token(self) -> str | None:
        if self._session_token:
            return self._session_token
        try:
            token = self._keyring().get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            token = None
        if token:
            self._session_token = token
            self._session_persisted = True
        return token

    def _save_token(self, token: str | None) -> bool:
        if not token:
            raise RuntimeError("Playerok не вернул токен сессии после входа.")
        self._session_token = token
        try:
            self._keyring().set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, token)
            self._session_persisted = True
            return True
        except Exception:
            # Session remains in memory for this run; UI must report it was not saved.
            self._session_persisted = False
            return False

    def request_login_code(self, email: str) -> dict[str, Any]:
        address = email.strip()
        if (
            not address
            or "@" not in address
            or address.startswith("@")
            or address.endswith("@")
            or any(character.isspace() for character in address)
        ):
            raise ValueError("Укажи корректный адрес электронной почты.")

        self._request(
            _PUBLIC + "/auth/send-otp",
            method="POST",
            payload={"email": address},
        )
        return {"sent": True, "email": address}

    def confirm_login_code(self, email: str, code: str) -> dict[str, Any]:
        address, otp = email.strip(), code.strip()
        if not address or not otp:
            raise ValueError("Укажи e-mail и код из письма.")

        payload, cookie_token = self._request(
            _PUBLIC + "/auth/confirm-otp",
            method="POST",
            payload={"email": address, "otpCode": otp},
        )
        payload = self._raise_api_errors(_unwrap_envelope(payload))
        if not isinstance(payload, dict):
            raise RuntimeError("Playerok вернул ответ авторизации в неожиданном формате.")

        if payload.get("requiresTwoFactor"):
            session = payload.get("secondFactorSession")
            if not self._second_factor_token(session):
                raise RuntimeError("Playerok не вернул данные второго фактора.")
            self._pending_second_factor_session = session
            return {"requires_second_factor": True}

        token = self._session_token_from_payload(payload, cookie_token)
        saved = self._save_token(token)
        result = self._profile(token, saved)
        self._pending_second_factor_session = None
        return result

    def confirm_second_factor(self, code: str) -> dict[str, Any]:
        otp = code.strip()
        session = self._pending_second_factor_session
        session_token = self._second_factor_token(session)
        if not session_token:
            raise ValueError("Сначала подтверди код из письма Playerok.")
        if not otp:
            raise ValueError("Введи код двухфакторной аутентификации.")

        payload, cookie_token = self._request(
            _PUBLIC + "/auth/confirm-second-factor",
            method="POST",
            payload={"token": session_token, "totpCode": otp},
        )
        payload = self._raise_api_errors(_unwrap_envelope(payload))
        token = self._session_token_from_payload(payload, cookie_token)
        saved = self._save_token(token)
        result = self._profile(token, saved)
        self._pending_second_factor_session = None
        return result

    def restore_session(self) -> dict[str, Any]:
        token = self._saved_token()
        if not token:
            raise ValueError("Сохранённая сессия Playerok не найдена. Выполни вход по e-mail.")
        return self._profile(token, self._session_persisted)

    def disconnect(self) -> dict[str, Any]:
        token = self._saved_token()
        remote_error = None
        if token:
            try:
                self._request(
                    _BFF + "/auth/logout",
                    method="POST",
                    token=token,
                    bearer=True,
                )
            except Exception as exc:
                remote_error = str(exc)

        self._session_token = None
        self._session_persisted = False
        self._pending_second_factor_session = None
        keyring_error = None
        try:
            self._keyring().delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception as exc:
            # keyring backends commonly signal that there was no stored entry.
            if token:
                keyring_error = str(exc)
        messages = []
        if remote_error:
            messages.append("Playerok не подтвердил удалённый выход: " + remote_error)
        if keyring_error:
            messages.append("Не удалось удалить токен из системного хранилища: " + keyring_error)
        if not messages:
            messages.append("Локальная сессия удалена.")
        return {
            "connected": False,
            "local_session_cleared": keyring_error is None,
            "message": " ".join(messages),
        }

    def fetch_orders(self) -> list[dict[str, Any]]:
        token = self._saved_token()
        if not token:
            raise ValueError("Сначала войди в аккаунт Playerok.")

        query = """
        query Deals($pagination: Pagination, $filter: ItemDealFilter!, $sort: Sort) {
          deals(pagination: $pagination, filter: $filter, sort: $sort) {
            edges {
              node {
                id
                status
                url
                createdAt
                item { id name }
                user { id username name }
                transaction { value }
              }
            }
            pageInfo { hasNextPage endCursor }
            totalCount
          }
        }
        """
        payload, _ = self._request(
            _GRAPHQL,
            method="POST",
            token=token,
            payload={
                "operationName": "Deals",
                "query": query,
                "variables": {
                    "filter": {"direction": "OUT", "status": ["PAID", "SENT"]},
                    "sort": None,
                    "pagination": {"first": 50},
                },
            },
        )
        payload = self._raise_api_errors(payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        connection = data.get("deals") if isinstance(data, dict) else None
        edges = connection.get("edges") if isinstance(connection, dict) else None
        if not isinstance(edges, list):
            raise RuntimeError("Playerok вернул список сделок в неожиданном формате.")

        result = []
        for edge in edges:
            deal = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(deal, dict):
                continue
            item = deal.get("item") if isinstance(deal.get("item"), dict) else {}
            buyer = deal.get("user") if isinstance(deal.get("user"), dict) else {}
            transaction = (
                deal.get("transaction")
                if isinstance(deal.get("transaction"), dict)
                else {}
            )
            status = deal.get("status")
            status = status.get("value", status) if isinstance(status, dict) else status
            created = deal.get("createdAt") or ""
            result.append({
                "platform": "playerok",
                "order_id": str(deal.get("id") or ""),
                "title": str(item.get("name") or "Товар Playerok"),
                "buyer": str(buyer.get("username") or buyer.get("name") or "—"),
                "status": str(status or "—"),
                "amount": str(transaction.get("value") or "—"),
                "created_at": str(created),
                "url": str(deal.get("url") or ""),
            })
        return result
