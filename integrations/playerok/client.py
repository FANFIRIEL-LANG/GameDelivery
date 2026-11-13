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
from urllib.request import HTTPRedirectHandler, build_opener
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


class _RejectRedirects(HTTPRedirectHandler):
    """Prevent urllib from forwarding session headers across HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
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
        self._account_user_id: str | None = None
        self._session_persisted = False
        self._keyring_read_failed = False
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
        if token and any(character in token for character in "\r\n;"):
            raise RuntimeError("Токен сессии содержит недопустимые символы.")

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
        if url == _GRAPHQL:
            headers["apollo-require-preflight"] = "true"
            operation_name = (
                payload.get("operationName")
                if isinstance(payload, dict)
                else None
            )
            headers["x-gql-op"] = str(operation_name or "anonymous")
            if operation_name:
                headers["x-apollo-operation-name"] = str(operation_name)
        if token:
            headers["Cookie"] = "token=" + token
            if bearer:
                headers["Authorization"] = "Bearer " + token

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with build_opener(_RejectRedirects).open(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read()
                cookie_token = _token_from_headers(response.headers)
        except urllib.error.HTTPError as exc:
            # Do not include response bodies, request data, tokens, or cookies in errors.
            raise RuntimeError(f"Playerok вернул HTTP {exc.code}.") from None
        except urllib.error.URLError:
            raise RuntimeError(
                "Не удалось установить HTTPS-соединение с Playerok."
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
                # Avoid surfacing server-provided request/credential data in UI messages.
                safe_message = re.sub(
                    r"(?i)(token|password|cookie|authorization|secret)\s*[:=]\s*[^\s,;]+",
                    r"\1=[скрыто]",
                    safe_message,
                )
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
        self._account_user_id = str(payload.get("id") or "").strip() or None
        return {
            "connected": True,
            "username": self._username(payload),
            "user_id": self._account_user_id or "",
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
            self._keyring_read_failed = True
            self._session_persisted = False
            return None

        self._keyring_read_failed = False
        self._session_persisted = bool(token)
        if token:
            self._session_token = token
        return token

    def _save_token(self, token: str | None) -> bool:
        if not token:
            raise RuntimeError("Playerok не вернул токен сессии после входа.")
        self._session_token = token
        try:
            self._keyring().set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, token)
            self._session_persisted = True
            self._keyring_read_failed = False
            return True
        except Exception:
            # Session remains in memory for this run; UI must report it was not saved.
            self._session_persisted = False
            self._keyring_read_failed = True
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
        result = self._profile(token, False)
        result["session_saved"] = self._save_token(token)
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
        result = self._profile(token, False)
        result["session_saved"] = self._save_token(token)
        self._pending_second_factor_session = None
        return result

    def restore_session(self) -> dict[str, Any]:
        token = self._saved_token()
        if not token and self._keyring_read_failed:
            raise RuntimeError(
                "Не удалось прочитать сессию Playerok из системного хранилища."
            )
        if not token:
            raise ValueError("Сохранённая сессия Playerok не найдена. Выполни вход по e-mail.")
        return self._profile(token, self._session_persisted)

    def disconnect(self) -> dict[str, Any]:
        token = self._saved_token()
        was_persisted = self._session_persisted
        keyring_state_unknown = self._keyring_read_failed
        remote_logout_failed = False
        if token:
            try:
                self._request(
                    _BFF + "/auth/logout",
                    method="POST",
                    token=token,
                    bearer=True,
                )
            except Exception:
                remote_logout_failed = True

        self._session_token = None
        self._session_persisted = False
        self._account_user_id = None
        self._pending_second_factor_session = None
        keyring_error = False
        try:
            self._keyring().delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            self._keyring_read_failed = False
        except Exception:
            # Ignore a missing entry only when reading the keyring had succeeded.
            if was_persisted or keyring_state_unknown:
                keyring_error = True
        messages = []
        if remote_logout_failed:
            messages.append(
                "Playerok не подтвердил удалённый выход. Проверь соединение и сессию."
            )
        if keyring_error:
            messages.append(
                "Не удалось удалить токен из системного хранилища."
            )
        if not messages:
            messages.append("Локальная сессия удалена.")
        return {
            "connected": False,
            "local_session_cleared": not keyring_error,
            "message": " ".join(messages),
        }

    @property
    def account_user_id(self) -> str | None:
        """Playerok seller account ID from the last successful profile check."""
        return self._account_user_id

    @staticmethod
    def _deal_status(deal: dict[str, Any]) -> str:
        status = deal.get("status")
        if isinstance(status, dict):
            status = status.get("value")
        return str(status or "UNKNOWN").strip().upper()

    @classmethod
    def _map_deal(cls, deal: Any) -> dict[str, Any]:
        if not isinstance(deal, dict):
            raise RuntimeError("Playerok вернул сделку в неожиданном формате.")
        item = deal.get("item") if isinstance(deal.get("item"), dict) else {}
        buyer = deal.get("user") if isinstance(deal.get("user"), dict) else {}
        chat = deal.get("chat") if isinstance(deal.get("chat"), dict) else {}
        transaction = (
            deal.get("transaction")
            if isinstance(deal.get("transaction"), dict)
            else {}
        )
        deal_id = str(deal.get("id") or "").strip()
        listing_id = str(item.get("id") or "").strip()
        chat_id = str(chat.get("id") or "").strip()
        if not deal_id or not listing_id:
            raise RuntimeError("Playerok не вернул отдельные deal ID и listing ID.")
        deal_url = "https://playerok.com/deal/" + deal_id
        chat_url = "https://playerok.com/chats/" + chat_id if chat_id else ""
        return {
            "platform": "playerok",
            "deal_id": deal_id,
            "order_id": deal_id,
            "chat_id": chat_id,
            "listing_id": listing_id,
            "title": str(item.get("name") or "Товар Playerok"),
            "buyer": str(buyer.get("username") or buyer.get("name") or "—"),
            "buyer_id": str(buyer.get("id") or ""),
            "status": cls._deal_status(deal),
            "amount": str(transaction.get("value") or "—"),
            "created_at": str(deal.get("createdAt") or ""),
            "deal_url": deal_url,
            "chat_url": chat_url,
            "url": deal_url,
        }

    def fetch_sales(
        self,
        *,
        statuses: tuple[str, ...] | list[str] | None = None,
        first: int = 50,
        max_pages: int = 10000,
    ) -> list[dict[str, Any]]:
        """Fetch every seller deal using cursor pagination.

        statuses=None requests the complete historical sales list. Chat unread
        counters are intentionally not used as deal discovery signals.
        """
        token = self._saved_token()
        if not token:
            raise ValueError("Сначала войди в аккаунт Playerok.")
        if isinstance(first, bool) or not isinstance(first, int) or first < 1:
            raise ValueError("first должен быть положительным числом")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages должен быть положительным числом")
        normalized_statuses = None
        if statuses is not None:
            normalized_statuses = tuple(dict.fromkeys(
                str(value).strip().upper() for value in statuses if str(value).strip()
            ))
            if not normalized_statuses:
                raise ValueError("statuses не должен быть пустым списком")

        query = """
        query Deals($pagination: Pagination, $filter: ItemDealFilter!, $sort: Sort) {
          deals(pagination: $pagination, filter: $filter, sort: $sort) {
            edges {
              node {
                id
                status
                createdAt
                item { id name }
                user { id username }
                chat { id }
                transaction { value }
              }
            }
            pageInfo { hasNextPage endCursor }
            totalCount
          }
        }
        """
        result: list[dict[str, Any]] = []
        seen_deals: dict[str, dict[str, Any]] = {}
        seen_cursors: set[str] = set()
        cursor: str | None = None
        page_count = 0

        while True:
            page_count += 1
            if page_count > max_pages:
                raise RuntimeError("Playerok превысил защитный лимит страниц сделок.")
            deal_filter: dict[str, Any] = {"direction": "OUT"}
            if normalized_statuses is not None:
                deal_filter["status"] = list(normalized_statuses)
            pagination: dict[str, Any] = {"first": first}
            if cursor is not None:
                pagination["after"] = cursor
            payload, _ = self._request(
                _GRAPHQL,
                method="POST",
                token=token,
                payload={
                    "operationName": "Deals",
                    "query": query,
                    "variables": {
                        "filter": deal_filter,
                        "sort": None,
                        "pagination": pagination,
                    },
                },
            )
            payload = self._raise_api_errors(payload)
            data = payload.get("data") if isinstance(payload, dict) else None
            connection = data.get("deals") if isinstance(data, dict) else None
            if not isinstance(connection, dict) or not isinstance(connection.get("edges"), list):
                raise RuntimeError("Playerok вернул список сделок в неожиданном формате.")

            for edge in connection["edges"]:
                if not isinstance(edge, dict) or not isinstance(edge.get("node"), dict):
                    raise RuntimeError("Playerok вернул некорректную запись в списке сделок.")
                mapped = self._map_deal(edge["node"])
                previous = seen_deals.get(mapped["deal_id"])
                if previous is not None:
                    if previous != mapped:
                        raise RuntimeError("Playerok вернул конфликтующие записи одной сделки.")
                    continue
                seen_deals[mapped["deal_id"]] = mapped
                result.append(mapped)

            page_info = connection.get("pageInfo")
            if page_info is None:
                raise RuntimeError("Playerok не вернул pageInfo; baseline не может считаться полным.")
            elif isinstance(page_info, dict):
                has_next = page_info.get("hasNextPage") is True
                end_cursor = page_info.get("endCursor")
            else:
                raise RuntimeError("Playerok вернул некорректный pageInfo.")
            if not has_next:
                break
            if not isinstance(end_cursor, str) or not end_cursor.strip():
                raise RuntimeError("Playerok сообщил о следующей странице без endCursor.")
            if end_cursor in seen_cursors or end_cursor == cursor:
                raise RuntimeError("Playerok повторил курсор пагинации; импорт остановлен.")
            seen_cursors.add(end_cursor)
            cursor = end_cursor

        return result

    def fetch_orders(self) -> list[dict[str, Any]]:
        """Compatibility wrapper used by the marketplace table."""
        return self.fetch_sales(statuses=("PAID", "SENT"))

    def fetch_deal(self, deal_id: str) -> dict[str, Any]:
        token = self._saved_token()
        external_id = str(deal_id or "").strip()
        if not token:
            raise ValueError("Сначала войди в аккаунт Playerok.")
        if not external_id:
            raise ValueError("deal_id обязателен.")
        query = """
        query Deal($id: UUID!) {
          deal(id: $id) {
            id status createdAt item { id name } user { id username }
            chat { id } transaction { value }
          }
        }
        """
        payload, _ = self._request(
            _GRAPHQL,
            method="POST",
            token=token,
            payload={
                "operationName": "Deal",
                "query": query,
                "variables": {"id": external_id},
            },
        )
        payload = self._raise_api_errors(payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        deal = data.get("deal") if isinstance(data, dict) else None
        mapped = self._map_deal(deal)
        if mapped["deal_id"] != external_id:
            raise RuntimeError("Playerok вернул не ту сделку при проверке статуса.")
        return mapped

    def update_deal_status(self, deal_id: str, status: str) -> dict[str, Any]:
        """The only enabled seller-side status action is PAID -> SENT."""
        external_id = str(deal_id or "").strip()
        target = str(status or "").strip().upper()
        if not external_id:
            raise ValueError("deal_id обязателен.")
        if target != "SENT":
            raise ValueError("GameDelivery разрешает только перевод сделки в SENT.")
        token = self._saved_token()
        if not token:
            raise ValueError("Сначала войди в аккаунт Playerok.")
        # Re-read immediately before mutation to close the status-change race.
        current = self.fetch_deal(external_id)
        current_status = self._deal_status({"status": current.get("status")})
        if current_status in {"SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}:
            return current
        if current_status != "PAID":
            raise ValueError("Playerok переводит в SENT только сделку со статусом PAID.")

        mutation = """
        mutation UpdateDeal($input: UpdateItemDealInput!) {
          updateDeal(input: $input) {
            id status createdAt item { id name } user { id username }
            chat { id } transaction { value }
          }
        }
        """
        payload, _ = self._request(
            _GRAPHQL,
            method="POST",
            token=token,
            payload={
                "operationName": "UpdateDeal",
                "query": mutation,
                "variables": {"input": {"id": external_id, "status": "SENT"}},
            },
        )
        payload = self._raise_api_errors(payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        deal = data.get("updateDeal") if isinstance(data, dict) else None
        mapped = self._map_deal(deal)
        if mapped["deal_id"] != external_id:
            raise RuntimeError("Playerok вернул другую сделку после смены статуса.")
        if mapped["status"] not in {"SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}:
            raise RuntimeError("Playerok не подтвердил перевод сделки в SENT.")
        return mapped
