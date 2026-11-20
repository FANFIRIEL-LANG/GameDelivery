"""Background, read-only Playerok GraphQL transport.

The operation names, persisted-query hashes, and pagination shapes follow the
existing GameDelivery contracts adapted from Playerok Universal (MIT; see
``universal_chat_contract.py``). This module deliberately exposes no mutation
operation and never includes credentials or response bodies in exceptions.
"""
from __future__ import annotations

import json
import re
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic, sleep
from typing import Any, Callable
from urllib.parse import urlencode

from integrations.playerok.browser_chat_api import (
    PlayerokBrowserChatAPI,
    PlayerokChat,
    PlayerokChatMessage,
    _get_url,
)
from integrations.playerok.client import (
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    _GRAPHQL,
    _USER_AGENT,
)
from integrations.playerok.universal_chat_contract import PERSISTED_QUERIES

_ORIGIN = "https://playerok.com"
_PAGE_SIZE = 24
_MAX_PAGES = 1000
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_RETRYABLE_STATUS = frozenset({408, 429})
_CLOUDFLARE_MARKERS = (
    "<title>just a moment",
    "cf-browser-verification",
    "window._cf_chl_opt",
    "checking your browser before accessing",
)
_CHROME_MAJOR = re.compile(r"(?:Chrome|Chromium)/(\d+)")
_CURL_CHROME_TARGETS = (99, 100, 101, 104, 107, 110, 116, 119, 120, 123, 124, 131, 133, 136, 142, 145, 146, 150, 151, 152)
_ALLOWED_SESSION_COOKIES = frozenset({"token", "__ddg5_", "__ddg3", "cf_clearance", "__cf_bm"})
_ALLOWED_OPERATIONS = frozenset({
    "viewer", "deals", "deal", "items", "userChats", "chat", "chatMessages",
})
_ALLOWED_DEAL_STATUSES = frozenset({"PAID", "SENT", "CONFIRMED"})
_SALES_SNAPSHOT_MAX_AGE_SECONDS = 300.0
_SALES_SNAPSHOT_LOCK = Lock()
_TRUSTED_SALES_SNAPSHOTS = weakref.WeakKeyDictionary()


class PlayerokGraphQLTransportError(RuntimeError):
    """Safe transport error; never carries a token, cookie, URL, or response body."""

    def __init__(self, code: str, *, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"Playerok GraphQL: {code}{suffix}")


@dataclass(frozen=True)
class PlayerokAuthContext:
    # Secret-bearing field is excluded from repr to prevent accidental logging.
    token: str = field(repr=False)
    user_agent: str = _USER_AGENT
    cookies: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self):
        if type(self.token) is not str or not self.token.strip() or any(
            ch in self.token for ch in "\r\n;"
        ):
            raise ValueError("invalid_session_context")
        if type(self.user_agent) is not str or not self.user_agent.strip() or any(
            ch in self.user_agent for ch in "\r\n"
        ):
            raise ValueError("invalid_session_context")
        match = _CHROME_MAJOR.search(self.user_agent)
        if match is None or not any(target <= int(match.group(1)) for target in _CURL_CHROME_TARGETS):
            raise ValueError("unsupported_session_fingerprint")
        raw_cookies = self.cookies or (("token", self.token),)
        if not isinstance(raw_cookies, tuple) or len(raw_cookies) > len(_ALLOWED_SESSION_COOKIES):
            raise ValueError("invalid_session_context")
        seen = set()
        for pair in raw_cookies:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("invalid_session_context")
            name, value = pair
            if (
                type(name) is not str or name not in _ALLOWED_SESSION_COOKIES or name in seen
                or type(value) is not str or not value or len(value) > 4096
                or any(ch in value for ch in "\r\n;")
            ):
                raise ValueError("invalid_session_context")
            seen.add(name)
        cookie_map = dict(raw_cookies)
        if cookie_map.get("token") != self.token:
            raise ValueError("invalid_session_context")
        object.__setattr__(self, "cookies", tuple(sorted(cookie_map.items())))

    @property
    def impersonate(self) -> str:
        major = int(_CHROME_MAJOR.search(self.user_agent).group(1))
        target = max(value for value in _CURL_CHROME_TARGETS if value <= major)
        return f"chrome{target}"

    @classmethod
    def from_keyring(cls, keyring_backend: Any | None = None) -> "PlayerokAuthContext":
        try:
            if keyring_backend is None:
                import keyring as keyring_backend
            raw = keyring_backend.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            raise PlayerokGraphQLTransportError("keyring_unavailable") from None
        if not raw:
            raise PlayerokGraphQLTransportError("session_missing")
        try:
            record = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            # Backward compatibility with the original token-only keyring value.
            record = None
        if isinstance(record, dict) and record.get("schema") == "gd-playerok-auth-context-v1":
            token, user_agent = record.get("token"), record.get("user_agent")
            raw_cookies = record.get("cookies")
            if not isinstance(raw_cookies, dict):
                raise PlayerokGraphQLTransportError("invalid_session_context")
        else:
            token, user_agent = raw, _USER_AGENT
            raw_cookies = None
        try:
            cookies = tuple(raw_cookies.items()) if isinstance(raw_cookies, dict) else ()
            return cls(token=token, user_agent=user_agent, cookies=cookies)
        except (TypeError, ValueError):
            raise PlayerokGraphQLTransportError("invalid_session_context") from None


@dataclass(frozen=True)
class PlayerokDealRecord:
    deal_id: str
    status: str
    created_at: str | None
    chat_id: str | None
    listing_id: str | None
    listing_ref: str | None
    buyer_id: str | None


@dataclass(frozen=True)
class PlayerokListingRecord:
    listing_id: str
    listing_ref: str | None
    name: str | None
    status: str | None


class PlayerokGraphQLSalesSnapshot:
    """Immutable normalized full snapshot issued only after a complete GraphQL scan."""

    __slots__ = ("_account_user_id", "_deals_json", "_token", "__weakref__")

    def __init__(self, account_user_id: str, deals: list[dict[str, Any]]):
        object.__setattr__(self, "_account_user_id", account_user_id)
        object.__setattr__(self, "_deals_json", json.dumps(
            deals, ensure_ascii=False, separators=(",", ":")
        ))
        object.__setattr__(self, "_token", None)

    def __setattr__(self, name, value):
        raise AttributeError("PlayerokGraphQLSalesSnapshot is immutable")

    @property
    def account_user_id(self) -> str:
        return self._account_user_id

    @property
    def deals(self) -> list[dict[str, Any]]:
        return json.loads(self._deals_json)


def is_fresh_trusted_graphql_sales_snapshot(
    value: object, *, now: float | None = None
) -> bool:
    if not isinstance(value, PlayerokGraphQLSalesSnapshot):
        return False
    current = monotonic() if now is None else now
    with _SALES_SNAPSHOT_LOCK:
        proof = _TRUSTED_SALES_SNAPSHOTS.get(value)
    if proof is None or proof[0] is not value._token:
        return False
    _token, started, completed = proof
    return (
        started <= completed <= current
        and completed - started <= _SALES_SNAPSHOT_MAX_AGE_SECONDS
        and current - completed <= _SALES_SNAPSHOT_MAX_AGE_SECONDS
    )


def _issue_graphql_sales_snapshot(
    account_user_id: str, deals: list[dict[str, Any]], started: float, completed: float
) -> PlayerokGraphQLSalesSnapshot:
    snapshot = PlayerokGraphQLSalesSnapshot(account_user_id, deals)
    token = object()
    object.__setattr__(snapshot, "_token", token)
    with _SALES_SNAPSHOT_LOCK:
        _TRUSTED_SALES_SNAPSHOTS[snapshot] = (token, started, completed)
    return snapshot


class PlayerokBrowserGraphQLSalesReader:
    """Read a complete sales snapshot through the current WebEngine page.

    The transport is the callback-based ``WebEngineGraphQLTransport``. This
    reader intentionally has no keyring/token dependency and runs its state
    machine on the page's Qt callback thread.
    """

    def __init__(
        self,
        transport: Any,
        callback: Callable[[PlayerokGraphQLSalesSnapshot | None, str | None], None],
        *,
        expected_account_user_id: str | None = None,
        page_size: int = _PAGE_SIZE,
        max_pages: int = _MAX_PAGES,
    ):
        if not callable(callback):
            raise TypeError("callback must be callable")
        if type(page_size) is not int or not 1 <= page_size <= _PAGE_SIZE:
            raise ValueError("page_size must be 1..24")
        if type(max_pages) is not int or not 1 <= max_pages <= _MAX_PAGES:
            raise ValueError("max_pages must be 1..1000")
        self.transport = transport
        self.callback = callback
        self.expected_account_user_id = (
            _required_id(expected_account_user_id)
            if expected_account_user_id is not None else None
        )
        self.page_size = page_size
        self.max_pages = max_pages
        self.session_lost = False
        self.cancelled = False
        self._completed = False
        self._started_at = monotonic()
        self._account_id: str | None = None
        self._cursor: str | None = None
        self._seen_cursors: set[str] = set()
        self._records: dict[str, PlayerokDealRecord] = {}
        self._expected_total: int | None = None
        self._pages = 0

    def start(self) -> "PlayerokBrowserGraphQLSalesReader":
        if self._completed:
            return self
        self._request("viewer", {}, self._viewer_received)
        return self

    def cancel(self, *, stage: str = "cancelled") -> None:
        if self._completed:
            return
        self.cancelled = True
        self._finish(None, "page_lost" if stage in {"page_destroyed", "timeout"} else "cancelled")

    def _request(self, operation: str, variables: dict[str, Any], received) -> None:
        if self._completed:
            return
        try:
            self.transport.request(
                operation, variables, method="GET",
                callback=lambda payload, error: received(payload, error),
            )
        except Exception:
            self._finish(None, "page_lost")

    def _viewer_received(self, payload: Any, error: str | None) -> None:
        if self._completed:
            return
        if error:
            self._fail(error)
            return
        try:
            viewer = payload["data"]["viewer"]
            if not isinstance(viewer, dict):
                raise PlayerokGraphQLTransportError("invalid_viewer")
            if type(viewer.get("id")) is not str or not viewer["id"].strip():
                raise PlayerokGraphQLTransportError("invalid_viewer")
            account_id = _required_id(viewer["id"])
            if self.expected_account_user_id and account_id != self.expected_account_user_id:
                raise PlayerokGraphQLTransportError("account_mismatch")
            self._account_id = account_id
            self._fetch_next_page()
        except PlayerokGraphQLTransportError as exc:
            self._fail(exc.code)
        except (KeyError, TypeError):
            self._fail("invalid_viewer")

    def _fetch_next_page(self) -> None:
        if self._completed:
            return
        self._pages += 1
        if self._pages > self.max_pages:
            self._finish(None, "page_limit")
            return
        variables = {
            "pagination": {"first": self.page_size, "after": self._cursor},
            "filter": {"userId": self._account_id, "direction": "Out", "status": None},
            "showForbiddenImage": True,
        }
        request_cursor = self._cursor
        self._request("deals", variables, lambda payload, error: self._page_received(
            payload, error, request_cursor
        ))

    def _page_received(self, payload: Any, error: str | None, request_cursor: str | None) -> None:
        if self._completed:
            return
        if error:
            self._fail(error)
            return
        try:
            edges, page_info, total = _connection_page(payload, "deals")
            if len(edges) > self.page_size:
                raise PlayerokGraphQLTransportError("partial_page")
            if total is not None:
                if self._expected_total is None:
                    self._expected_total = total
                elif self._expected_total != total:
                    raise PlayerokGraphQLTransportError("changing_total_count")
            for edge in edges:
                record = PlayerokReadOnlyGraphQLClient._deal(
                    edge.get("node") if isinstance(edge, dict) else None
                )
                previous = self._records.get(record.deal_id)
                if previous is not None and previous != record:
                    raise PlayerokGraphQLTransportError("conflicting_duplicate")
                self._records.setdefault(record.deal_id, record)
            if not page_info["hasNextPage"]:
                if self._expected_total is not None and self._expected_total != len(self._records):
                    raise PlayerokGraphQLTransportError("incomplete_snapshot")
                self._complete_snapshot()
                return
            next_cursor = page_info.get("endCursor")
            if not edges:
                raise PlayerokGraphQLTransportError("invalid_pagination")
            if len(edges) < self.page_size:
                raise PlayerokGraphQLTransportError("partial_page")
            if (
                type(next_cursor) is not str or not next_cursor.strip()
                or next_cursor == request_cursor or next_cursor in self._seen_cursors
            ):
                raise PlayerokGraphQLTransportError("invalid_pagination")
            self._seen_cursors.add(next_cursor)
            self._cursor = next_cursor
            self._fetch_next_page()
        except PlayerokGraphQLTransportError as exc:
            self._fail(exc.code)
        except (KeyError, TypeError):
            self._fail("invalid_page")

    def _complete_snapshot(self) -> None:
        deals = [{
            "platform": "playerok",
            "deal_id": record.deal_id,
            "order_id": record.deal_id,
            "status": record.status.strip().upper(),
            "created_at": record.created_at or "",
            "chat_id": record.chat_id or "",
            "listing_id": record.listing_id or "",
            "source_listing_ref": record.listing_ref or "",
            "buyer_id": record.buyer_id or "",
            "buyer": "",
            "title": "",
            "amount": "",
            "deal_url": "",
            "chat_url": "",
        } for record in self._records.values()]
        snapshot = _issue_graphql_sales_snapshot(
            self._account_id or "", deals, self._started_at, monotonic()
        )
        self._finish(snapshot, None)

    def _fail(self, code: str) -> None:
        session_error = code in {
            "session_expired", "session_missing", "unauthenticated", "http_401",
            "http_403", "forbidden", "invalid_viewer",
        }
        self.session_lost = session_error
        self._finish(None, "session_expired" if session_error else code)

    def _finish(self, snapshot, error: str | None) -> None:
        if self._completed:
            return
        self._completed = True
        try:
            self.callback(snapshot, error)
        except Exception:
            pass


def _required_id(value: Any) -> str:
    if type(value) is not str or not value.strip() or len(value) > 256:
        raise PlayerokGraphQLTransportError("invalid_response")
    return value


def _optional_id(value: Any) -> str | None:
    return None if value is None else _required_id(value)


def _optional_string(value: Any, *, limit: int = 1024) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > limit:
        raise PlayerokGraphQLTransportError("invalid_response")
    return value


def _connection_page(payload: Any, key: str) -> tuple[list[Any], dict[str, Any], int | None]:
    try:
        connection = payload["data"][key]
        edges, page_info = connection["edges"], connection["pageInfo"]
    except (KeyError, TypeError):
        raise PlayerokGraphQLTransportError("invalid_page") from None
    if not isinstance(edges, list) or not isinstance(page_info, dict):
        raise PlayerokGraphQLTransportError("invalid_page")
    if type(page_info.get("hasNextPage")) is not bool:
        raise PlayerokGraphQLTransportError("invalid_page")
    total = connection.get("totalCount")
    if total is not None and (type(total) is not int or total < 0):
        raise PlayerokGraphQLTransportError("invalid_page")
    return edges, page_info, total


class PlayerokGraphQLHTTPTransport:
    """Synchronous request primitive intended to run from a worker thread."""

    def __init__(
        self,
        context: PlayerokAuthContext,
        *,
        session: Any | None = None,
        session_factory: Callable[..., Any] | None = None,
        retries: int = 2,
        timeout: float = 20.0,
        sleeper: Callable[[float], None] = sleep,
    ):
        if type(retries) is not int or not 0 <= retries <= 3:
            raise ValueError("retries must be between 0 and 3")
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError("invalid timeout")
        self.context = context
        self.timeout = float(timeout)
        self.retries = retries
        self._sleeper = sleeper
        if session is not None:
            self._session = session
        else:
            try:
                if session_factory is None:
                    from curl_cffi import requests as curl_requests
                    session_factory = curl_requests.Session
                self._session = session_factory(impersonate=context.impersonate)
            except Exception:
                raise PlayerokGraphQLTransportError("http_backend_unavailable") from None

    def _headers(self, operation: str) -> dict[str, str]:
        return {
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": self.context.user_agent,
            "origin": _ORIGIN,
            "referer": _ORIGIN + "/",
            "apollo-require-preflight": "true",
            "apollographql-client-name": "web",
            "x-gql-op": operation,
            "x-gql-path": "/",
            "x-timezone-offset": "-240",
            "x-apollo-operation-name": operation,
            "cookie": "; ".join(f"{name}={value}" for name, value in self.context.cookies),
            "authorization": "Bearer " + self.context.token,
        }

    @staticmethod
    def _is_cloudflare(response: Any, body_text: str) -> bool:
        content_type = ""
        try:
            content_type = str(response.headers.get("content-type", "")).lower()
        except Exception:
            pass
        text = body_text[:8192].lower()
        return "text/html" in content_type or any(marker in text for marker in _CLOUDFLARE_MARKERS)

    def execute(self, operation: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if operation not in _ALLOWED_OPERATIONS:
            raise PlayerokGraphQLTransportError("unsupported_operation")
        if variables is None:
            variables = {}
        if not isinstance(variables, dict):
            raise PlayerokGraphQLTransportError("invalid_variables")
        headers = self._headers(operation)
        if operation == "viewer":
            request_args = {
                "url": _GRAPHQL,
                "json": {"operationName": "viewer", "query": "query viewer { viewer { id username __typename } }", "variables": {}},
            }
            method = "post"
        else:
            if operation not in PERSISTED_QUERIES:
                raise PlayerokGraphQLTransportError("unsupported_operation")
            if operation in {"deals", "items"}:
                params = {
                    "operationName": operation,
                    "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
                    "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_QUERIES[operation]}}, separators=(",", ":")),
                }
                request_args = {"url": _GRAPHQL, "params": params}
            else:
                # Reuse the persisted contracts and URL layout from the browser API.
                url = _get_url(operation, variables)
                request_args = {"url": url}
            method = "get"

        last_error = "network_error"
        for attempt in range(self.retries + 1):
            try:
                response = getattr(self._session, method)(
                    **request_args,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except Exception:
                if attempt < self.retries:
                    self._sleeper(0.15 * (attempt + 1))
                    continue
                raise PlayerokGraphQLTransportError(last_error) from None

            try:
                status = int(response.status_code)
                body_text = response.text if type(response.text) is str else ""
                if len(body_text.encode("utf-8", errors="replace")) > _MAX_RESPONSE_BYTES:
                    raise PlayerokGraphQLTransportError("response_too_large", status_code=status)
            except PlayerokGraphQLTransportError:
                raise
            except Exception:
                raise PlayerokGraphQLTransportError("invalid_response") from None

            if self._is_cloudflare(response, body_text):
                raise PlayerokGraphQLTransportError("cloudflare_challenge", status_code=status)
            if status in _RETRYABLE_STATUS or 500 <= status <= 599:
                if attempt < self.retries:
                    self._sleeper(0.15 * (attempt + 1))
                    continue
                raise PlayerokGraphQLTransportError("http_error", status_code=status)
            if status in (401, 403):
                raise PlayerokGraphQLTransportError("session_expired", status_code=status)
            if not 200 <= status < 300:
                raise PlayerokGraphQLTransportError("http_error", status_code=status)
            try:
                payload = response.json()
            except Exception:
                raise PlayerokGraphQLTransportError("invalid_response", status_code=status) from None
            if not isinstance(payload, dict):
                raise PlayerokGraphQLTransportError("invalid_response", status_code=status)
            errors = payload.get("errors")
            if errors:
                code = "graphql_error"
                if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    extensions = errors[0].get("extensions")
                    raw_code = extensions.get("code") if isinstance(extensions, dict) else None
                    safe_codes = {
                        "UNAUTHENTICATED": "session_expired",
                        "FORBIDDEN": "forbidden",
                        "BAD_USER_INPUT": "bad_user_input",
                        "NOT_FOUND": "not_found",
                        "PERSISTED_QUERY_NOT_FOUND": "persisted_query_not_found",
                        "PERSISTED_QUERY_NOT_SUPPORTED": "persisted_query_not_supported",
                        "GRAPHQL_VALIDATION_FAILED": "graphql_validation_failed",
                    }
                    code = safe_codes.get(raw_code, code)
                raise PlayerokGraphQLTransportError(code, status_code=status)
            if not isinstance(payload.get("data"), dict):
                raise PlayerokGraphQLTransportError("invalid_response", status_code=status)
            return payload
        raise PlayerokGraphQLTransportError(last_error)

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class _SynchronousChatTransport:
    def __init__(self, transport: PlayerokGraphQLHTTPTransport):
        self.transport = transport

    def request(self, operation, variables, *, method, callback):
        if method != "GET":
            callback(None, "unsupported_method")
            return
        try:
            callback(self.transport.execute(operation, variables), None)
        except PlayerokGraphQLTransportError as exc:
            callback(None, exc.code)


class PlayerokReadOnlyGraphQLClient:
    """Typed read-only operations over one keyring-backed HTTP session."""

    def __init__(self, transport: PlayerokGraphQLHTTPTransport):
        self.transport = transport
        self._chat_api = PlayerokBrowserChatAPI(_SynchronousChatTransport(transport))
        self._executor: ThreadPoolExecutor | None = None

    @classmethod
    def from_keyring(cls, keyring_backend: Any | None = None, **transport_kwargs):
        context = PlayerokAuthContext.from_keyring(keyring_backend)
        return cls(PlayerokGraphQLHTTPTransport(context, **transport_kwargs))

    @staticmethod
    def save_browser_context(
        cookies: dict[str, str],
        user_agent: str,
        *,
        keyring_backend: Any | None = None,
    ) -> None:
        """Persist only the small allowlisted Playerok auth context to keyring."""
        token = cookies.get("token") if isinstance(cookies, dict) else None
        try:
            pairs = tuple((name, value) for name, value in cookies.items() if name in _ALLOWED_SESSION_COOKIES)
            context = PlayerokAuthContext(token=token, user_agent=user_agent, cookies=pairs)
        except (AttributeError, TypeError, ValueError):
            raise PlayerokGraphQLTransportError("invalid_session_context") from None
        try:
            if keyring_backend is None:
                import keyring as keyring_backend
            keyring_backend.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, json.dumps({
                "schema": "gd-playerok-auth-context-v1",
                "token": context.token,
                "user_agent": context.user_agent,
                "cookies": dict(context.cookies),
            }, separators=(",", ":"), ensure_ascii=False))
        except Exception:
            raise PlayerokGraphQLTransportError("keyring_write_failed") from None
        try:
            stored_context = PlayerokAuthContext.from_keyring(keyring_backend)
        except PlayerokGraphQLTransportError:
            raise PlayerokGraphQLTransportError("keyring_readback_failed") from None
        if stored_context != context:
            raise PlayerokGraphQLTransportError("keyring_readback_mismatch")

    def get_viewer(self) -> dict[str, str]:
        viewer = self.transport.execute("viewer").get("data", {}).get("viewer")
        if not isinstance(viewer, dict):
            raise PlayerokGraphQLTransportError("invalid_viewer")
        return {
            "user_id": _required_id(viewer.get("id")),
            "username": _optional_string(viewer.get("username"), limit=256) or "",
        }

    @staticmethod
    def _deal(raw: Any) -> PlayerokDealRecord:
        if not isinstance(raw, dict):
            raise PlayerokGraphQLTransportError("invalid_deal")
        item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
        chat = raw.get("chat") if isinstance(raw.get("chat"), dict) else {}
        buyer = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        status = raw.get("status")
        if isinstance(status, dict):
            status = status.get("value")
        if type(status) is not str or not status:
            raise PlayerokGraphQLTransportError("invalid_deal")
        return PlayerokDealRecord(
            deal_id=_required_id(raw.get("id")),
            status=status,
            created_at=_optional_string(raw.get("createdAt"), limit=128),
            chat_id=_optional_id(chat.get("id")),
            listing_id=_optional_id(item.get("id")),
            listing_ref=_optional_string(item.get("slug"), limit=256),
            buyer_id=_optional_id(buyer.get("id")),
        )

    def fetch_deal(self, deal_id: str) -> PlayerokDealRecord | None:
        deal_id = _required_id(deal_id)
        payload = self.transport.execute("deal", {
            "id": deal_id, "hasSupportAccess": False, "showForbiddenImage": True,
        })
        raw = payload["data"].get("deal")
        if raw is None:
            return None
        deal = self._deal(raw)
        if deal.deal_id != deal_id:
            raise PlayerokGraphQLTransportError("identity_mismatch")
        return deal

    def fetch_deals(
        self,
        account_user_id: str,
        *,
        statuses: tuple[str, ...] | None = ("PAID", "SENT", "CONFIRMED"),
        page_size: int = _PAGE_SIZE,
        max_pages: int = _MAX_PAGES,
    ) -> tuple[PlayerokDealRecord, ...]:
        account_user_id = _required_id(account_user_id)
        if type(page_size) is not int or not 1 <= page_size <= _PAGE_SIZE:
            raise ValueError("page_size must be 1..24")
        if type(max_pages) is not int or not 1 <= max_pages <= _MAX_PAGES:
            raise ValueError("max_pages must be 1..1000")
        if statuses is not None and (
            not statuses
            or any(type(value) is not str or value not in _ALLOWED_DEAL_STATUSES for value in statuses)
        ):
            raise ValueError("unsupported_deal_status")
        cursor = None
        seen_cursors: set[str] = set()
        records: dict[str, PlayerokDealRecord] = {}
        expected_total: int | None = None
        for _page in range(max_pages):
            variables = {
                "pagination": {"first": page_size, "after": cursor},
                "filter": {
                    "userId": account_user_id,
                    "direction": "Out",
                    "status": list(statuses) if statuses is not None else None,
                },
                "showForbiddenImage": True,
            }
            payload = self.transport.execute("deals", variables)
            edges, page_info, total = _connection_page(payload, "deals")
            if len(edges) > page_size:
                raise PlayerokGraphQLTransportError("partial_page")
            if total is not None:
                if expected_total is None:
                    expected_total = total
                elif expected_total != total:
                    raise PlayerokGraphQLTransportError("changing_total_count")
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                record = self._deal(node)
                previous = records.get(record.deal_id)
                if previous is not None and previous != record:
                    raise PlayerokGraphQLTransportError("conflicting_duplicate")
                records.setdefault(record.deal_id, record)
            if not page_info["hasNextPage"]:
                if expected_total is not None and expected_total != len(records):
                    raise PlayerokGraphQLTransportError("incomplete_snapshot")
                return tuple(records.values())
            next_cursor = page_info.get("endCursor")
            if not edges:
                raise PlayerokGraphQLTransportError("invalid_pagination")
            if len(edges) < page_size:
                raise PlayerokGraphQLTransportError("partial_page")
            if type(next_cursor) is not str or not next_cursor.strip() or next_cursor == cursor or next_cursor in seen_cursors:
                raise PlayerokGraphQLTransportError("invalid_pagination")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise PlayerokGraphQLTransportError("page_limit")

    def fetch_sales_snapshot(
        self,
        *,
        expected_account_user_id: str | None = None,
        page_size: int = _PAGE_SIZE,
        max_pages: int = _MAX_PAGES,
    ) -> PlayerokGraphQLSalesSnapshot:
        """Verify the signed-in seller and fetch a complete, normalized deal snapshot."""
        started = monotonic()
        viewer = self.get_viewer()
        account_id = viewer["user_id"]
        if expected_account_user_id is not None:
            expected = _required_id(expected_account_user_id)
            if account_id != expected:
                raise PlayerokGraphQLTransportError("account_mismatch")
        # A full unfiltered scan is required for the first account baseline and
        # for status reconciliation; no active-only result may complete it.
        records = self.fetch_deals(
            account_id, statuses=None, page_size=page_size, max_pages=max_pages
        )
        deals = []
        for record in records:
            # listing_id is the exact Playerok item ID. listing_ref remains a
            # separate slug/reference and is never promoted to canonical ID.
            deals.append({
                "platform": "playerok",
                "deal_id": record.deal_id,
                "order_id": record.deal_id,
                "status": record.status.strip().upper(),
                "created_at": record.created_at or "",
                "chat_id": record.chat_id or "",
                "listing_id": record.listing_id or "",
                "source_listing_ref": record.listing_ref or "",
                "buyer_id": record.buyer_id or "",
                "buyer": "",
                "title": "",
                "amount": "",
                "deal_url": "",
                "chat_url": "",
            })
        completed = monotonic()
        return _issue_graphql_sales_snapshot(account_id, deals, started, completed)

    def fetch_listings(
        self,
        account_user_id: str,
        *,
        statuses: tuple[str, ...] = ("APPROVED",),
        page_size: int = _PAGE_SIZE,
        max_pages: int = _MAX_PAGES,
    ) -> tuple[PlayerokListingRecord, ...]:
        account_user_id = _required_id(account_user_id)
        if type(page_size) is not int or not 1 <= page_size <= _PAGE_SIZE:
            raise ValueError("page_size must be 1..24")
        if type(max_pages) is not int or not 1 <= max_pages <= _MAX_PAGES:
            raise ValueError("max_pages must be 1..1000")
        if any(type(value) is not str or not value.strip() for value in statuses):
            raise ValueError("invalid_listing_status")
        cursor = None
        seen_cursors: set[str] = set()
        records: dict[str, PlayerokListingRecord] = {}
        expected_total: int | None = None
        for _page in range(max_pages):
            variables = {
                "pagination": {"first": page_size, "after": cursor},
                "filter": {"userId": account_user_id, "status": list(statuses)},
                "showForbiddenImage": True,
            }
            payload = self.transport.execute("items", variables)
            edges, page_info, total = _connection_page(payload, "items")
            if len(edges) > page_size:
                raise PlayerokGraphQLTransportError("partial_page")
            if total is not None:
                if expected_total is None:
                    expected_total = total
                elif expected_total != total:
                    raise PlayerokGraphQLTransportError("changing_total_count")
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    raise PlayerokGraphQLTransportError("invalid_listing")
                listing_id = _required_id(node.get("id"))
                record = PlayerokListingRecord(
                    listing_id=listing_id,
                    listing_ref=_optional_string(node.get("slug"), limit=256),
                    name=_optional_string(node.get("name"), limit=1024),
                    status=_optional_string(node.get("status"), limit=64),
                )
                previous = records.get(listing_id)
                if previous is not None and previous != record:
                    raise PlayerokGraphQLTransportError("conflicting_duplicate")
                records.setdefault(listing_id, record)
            if not page_info["hasNextPage"]:
                if expected_total is not None and expected_total != len(records):
                    raise PlayerokGraphQLTransportError("incomplete_snapshot")
                return tuple(records.values())
            next_cursor = page_info.get("endCursor")
            if not edges:
                raise PlayerokGraphQLTransportError("invalid_pagination")
            if len(edges) < page_size:
                raise PlayerokGraphQLTransportError("partial_page")
            if type(next_cursor) is not str or not next_cursor.strip() or next_cursor == cursor or next_cursor in seen_cursors:
                raise PlayerokGraphQLTransportError("invalid_pagination")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise PlayerokGraphQLTransportError("page_limit")

    def get_chat(self, chat_id: str) -> PlayerokChat:
        result: dict[str, Any] = {}
        self._chat_api.get_chat(chat_id, lambda value, error: result.update(value=value, error=error))
        if result.get("error"):
            raise PlayerokGraphQLTransportError(result["error"])
        if not isinstance(result.get("value"), PlayerokChat):
            raise PlayerokGraphQLTransportError("invalid_chat")
        return result["value"]

    def fetch_chats(self, account_user_id: str) -> tuple[PlayerokChat, ...]:
        result: dict[str, Any] = {}
        self._chat_api.get_chats(account_user_id, lambda value, error: result.update(value=value, error=error))
        if result.get("error"):
            raise PlayerokGraphQLTransportError(result["error"])
        if not isinstance(result.get("value"), tuple):
            raise PlayerokGraphQLTransportError("invalid_chat_list")
        return result["value"]

    def fetch_chat_messages(self, chat_id: str) -> tuple[PlayerokChatMessage, ...]:
        result: dict[str, Any] = {}
        self._chat_api.get_chat_messages(chat_id, lambda value, error: result.update(value=value, error=error))
        if result.get("error"):
            raise PlayerokGraphQLTransportError(result["error"])
        if not isinstance(result.get("value"), tuple):
            raise PlayerokGraphQLTransportError("invalid_message_list")
        return result["value"]

    def submit(self, operation: str, *args, callback: Callable[[Any, str | None], None] | None = None, **kwargs) -> Future:
        """Submit only a named read method to a worker; callbacks run on that worker."""
        allowed = {
            "get_viewer", "fetch_deal", "fetch_deals", "fetch_sales_snapshot", "fetch_listings",
            "get_chat", "fetch_chats", "fetch_chat_messages",
        }
        if operation not in allowed:
            raise ValueError("unsupported_operation")
        if self._executor is None:
            # A curl session owns connection/cookie state; serialize its use.
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playerok-graphql")
        future = self._executor.submit(getattr(self, operation), *args, **kwargs)
        if callback is not None:
            def completed(done: Future) -> None:
                try:
                    result = done.result()
                except PlayerokGraphQLTransportError as exc:
                    callback(None, exc.code)
                    return
                except Exception:
                    callback(None, "operation_failed")
                    return
                callback(result, None)
            future.add_done_callback(completed)
        return future

    def close(self, *, wait: bool = True) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=True)
            self._executor = None
        self.transport.close()
