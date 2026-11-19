"""Playerok chat GraphQL API over the already-authenticated Qt WebEngine page.

The request shapes and selected GraphQL contract constants are adapted from
playerok-universal/playerokapi at commit deabd093cdf2cc9fa936d8e6aa1d5dd665e548a6.
MIT License, Copyright (c) 2026 alleexxeeyy; see universal_chat_contract.py.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlencode
from uuid import uuid4

from integrations.playerok.universal_chat_contract import PERSISTED_QUERIES, QUERIES

_GRAPHQL_URL = "https://playerok.com/graphql"
_VIEWER_URL = "https://bff.playerok.com/rest-api/public/viewer"
_PAGE_SIZE = 24
_MAX_PAGES = 1000
_SAFE_CALLBACK_BYTES = 2 * 1024 * 1024
_SAFE_GRAPHQL_ERROR_CODES = {
    "PERSISTED_QUERY_NOT_FOUND": "persisted_query_not_found",
    "PERSISTED_QUERY_NOT_SUPPORTED": "persisted_query_not_supported",
    "GRAPHQL_VALIDATION_FAILED": "graphql_validation_failed",
    "UNAUTHENTICATED": "unauthenticated",
    "FORBIDDEN": "forbidden",
    "BAD_USER_INPUT": "bad_user_input",
    "NOT_FOUND": "not_found",
    "INTERNAL_SERVER_ERROR": "internal_server_error",
}


def _safe_graphql_error_code(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return None
    for error in errors[:5]:
        extensions = error.get("extensions") if isinstance(error, dict) else None
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if type(code) is str and code in _SAFE_GRAPHQL_ERROR_CODES:
            return _SAFE_GRAPHQL_ERROR_CODES[code]
    return None


class ChatTransport(Protocol):
    def request(
        self, operation: str, variables: dict[str, Any], *, method: str,
        callback: Callable[[dict[str, Any] | None, str | None], None],
    ) -> None: ...


@dataclass(frozen=True)
class PlayerokChatUser:
    id: str
    username: str | None


@dataclass(frozen=True)
class PlayerokChatFile:
    id: str | None
    url: str | None
    filename: str | None
    mime: str | None


@dataclass(frozen=True)
class PlayerokChatMessage:
    id: str
    chat_id: str | None
    text: str | None
    created_at: str | None
    user: PlayerokChatUser | None
    deal_id: str | None
    file: PlayerokChatFile | None
    images: tuple[PlayerokChatFile, ...]
    event: str | None = None


@dataclass(frozen=True)
class PlayerokChat:
    id: str
    type: str | None
    status: str | None
    unread_messages_counter: int | None
    started_at: str | None
    finished_at: str | None
    participants: tuple[PlayerokChatUser, ...]
    last_message: PlayerokChatMessage | None
    deal_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlayerokDealReference:
    id: str
    status: str | None
    chat_id: str | None
    listing_id: str | None


class ChatAPIError(RuntimeError):
    """Safe error with no raw response, cookie, token, or message text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Playerok chat API error: {code}")


class ChatSendResultUnknown(ChatAPIError):
    """Mutation may have reached Playerok; callers must not retry automatically."""


def _canonical_id(value: object, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str or not value.strip() or len(value) > 256:
        raise ChatAPIError("invalid_identifier")
    return value


def _optional_text(value: object, *, limit: int = 8192) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > limit:
        raise ChatAPIError("invalid_field")
    return value


def _user(raw: object) -> PlayerokChatUser | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ChatAPIError("invalid_user")
    return PlayerokChatUser(
        id=_canonical_id(raw.get("id")),
        username=_optional_text(raw.get("username"), limit=256),
    )


def _file(raw: object) -> PlayerokChatFile | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ChatAPIError("invalid_attachment")
    return PlayerokChatFile(
        id=_canonical_id(raw.get("id"), required=False),
        url=_optional_text(raw.get("url"), limit=4096),
        filename=_optional_text(raw.get("filename"), limit=512),
        mime=_optional_text(raw.get("mime"), limit=256),
    )


def normalize_message(raw: object, *, chat_id: str | None = None) -> PlayerokChatMessage:
    if not isinstance(raw, dict):
        raise ChatAPIError("invalid_message")
    deal = raw.get("deal")
    if deal is not None and not isinstance(deal, dict):
        raise ChatAPIError("invalid_deal_reference")
    image_values = raw.get("images") or []
    if not isinstance(image_values, list) or len(image_values) > 100:
        raise ChatAPIError("invalid_attachments")
    images = tuple(item for item in (_file(value) for value in image_values) if item is not None)
    return PlayerokChatMessage(
        id=_canonical_id(raw.get("id")),
        chat_id=_canonical_id(chat_id, required=False),
        text=_optional_text(raw.get("text")),
        created_at=_optional_text(raw.get("createdAt"), limit=128),
        user=_user(raw.get("user")),
        deal_id=_canonical_id(deal.get("id"), required=False) if deal else None,
        file=_file(raw.get("file")),
        images=images,
        event=_optional_text(raw.get("event"), limit=128),
    )


def normalize_chat(raw: object) -> PlayerokChat:
    if not isinstance(raw, dict):
        raise ChatAPIError("invalid_chat")
    participants = raw.get("participants") or []
    deals = raw.get("deals") or []
    if not isinstance(participants, list) or len(participants) > 100:
        raise ChatAPIError("invalid_participants")
    if not isinstance(deals, list) or len(deals) > 100:
        raise ChatAPIError("invalid_deals")
    deal_ids = tuple(
        _canonical_id(deal.get("id")) for deal in deals if isinstance(deal, dict)
    )
    if len(deal_ids) != len(deals):
        raise ChatAPIError("invalid_deal_reference")
    unread = raw.get("unreadMessagesCounter")
    if unread is not None and (type(unread) is not int or unread < 0):
        raise ChatAPIError("invalid_chat_counter")
    chat_id = _canonical_id(raw.get("id"))
    last = raw.get("lastMessage")
    return PlayerokChat(
        id=chat_id,
        type=_optional_text(raw.get("type"), limit=64),
        status=_optional_text(raw.get("status"), limit=64),
        unread_messages_counter=unread,
        started_at=_optional_text(raw.get("startedAt"), limit=128),
        finished_at=_optional_text(raw.get("finishedAt"), limit=128),
        participants=tuple(user for user in (_user(value) for value in participants) if user),
        last_message=normalize_message(last, chat_id=chat_id) if last else None,
        deal_ids=deal_ids,
    )


def _get_url(operation: str, variables: dict[str, Any]) -> str:
    if operation not in PERSISTED_QUERIES:
        raise ChatAPIError("unsupported_operation")
    query = urlencode({
        "operationName": operation,
        "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
        "extensions": json.dumps({"persistedQuery": {
            "version": 1, "sha256Hash": PERSISTED_QUERIES[operation],
        }}, separators=(",", ":")),
    })
    return f"{_GRAPHQL_URL}?{query}"


class PlayerokBrowserChatAPI:
    """Callback API; all transport requests stay inside the supplied WebEngine page."""

    def __init__(self, transport: ChatTransport, *, page_size: int = _PAGE_SIZE, max_pages: int = _MAX_PAGES):
        if type(page_size) is not int or not 1 <= page_size <= 24:
            raise ValueError("page_size must be 1..24")
        if type(max_pages) is not int or not 1 <= max_pages <= _MAX_PAGES:
            raise ValueError("max_pages must be 1..1000")
        self.transport = transport
        self.page_size = page_size
        self.max_pages = max_pages

    def get_viewer(self, callback) -> None:
        self.transport.request("viewer", {}, method="GET", callback=lambda raw, err: self._viewer_result(raw, err, callback))

    @staticmethod
    def _viewer_result(raw, error, callback):
        if error:
            callback(None, error)
            return
        try:
            data = raw.get("data", raw) if isinstance(raw, dict) else None
            viewer = data.get("viewer", data) if isinstance(data, dict) else None
            user_id = _canonical_id(viewer.get("id")) if isinstance(viewer, dict) else None
            callback(user_id, None)
        except ChatAPIError as exc:
            callback(None, exc.code)

    def get_chat(self, chat_id: str, callback) -> None:
        try:
            chat_id = _canonical_id(chat_id)
        except ChatAPIError as exc:
            callback(None, exc.code)
            return
        self.transport.request("chat", {"id": chat_id, "hasSupportAccess": False}, method="GET",
                               callback=lambda raw, err: self._single_chat(raw, err, callback))

    def get_deal(self, deal_id: str, callback) -> None:
        try:
            deal_id = _canonical_id(deal_id)
        except ChatAPIError as exc:
            callback(None, exc.code)
            return
        self.transport.request("deal", {
            "id": deal_id, "hasSupportAccess": False, "showForbiddenImage": True,
        }, method="GET", callback=lambda raw, err: self._single_deal(raw, err, callback))

    @staticmethod
    def _single_deal(raw, error, callback):
        if error:
            callback(None, error)
            return
        try:
            data = raw.get("data") if isinstance(raw, dict) else None
            deal = data.get("deal") if isinstance(data, dict) else None
            if not isinstance(deal, dict):
                raise ChatAPIError("invalid_deal")
            chat = deal.get("chat")
            item = deal.get("item")
            if chat is not None and not isinstance(chat, dict):
                raise ChatAPIError("invalid_deal_chat")
            if item is not None and not isinstance(item, dict):
                raise ChatAPIError("invalid_deal_item")
            callback(PlayerokDealReference(
                id=_canonical_id(deal.get("id")),
                status=_optional_text(deal.get("status"), limit=64),
                chat_id=_canonical_id(chat.get("id"), required=False) if chat else None,
                listing_id=_canonical_id(item.get("id"), required=False) if item else None,
            ), None)
        except ChatAPIError as exc:
            callback(None, exc.code)

    @staticmethod
    def _single_chat(raw, error, callback):
        if error:
            callback(None, error)
            return
        try:
            data = raw.get("data") if isinstance(raw, dict) else None
            callback(normalize_chat(data.get("chat")) if isinstance(data, dict) else None, None)
        except ChatAPIError as exc:
            callback(None, exc.code)

    @staticmethod
    def _page_observed(observer, has_cursor: bool, error: str | None) -> None:
        if callable(observer):
            try:
                observer(bool(has_cursor), error)
            except Exception:
                pass

    def get_chats(
        self, user_id: str, callback, *, page_size: int | None = None, page_observer=None,
    ) -> None:
        try:
            account_id = _canonical_id(user_id)
            size = self.page_size if page_size is None else page_size
            if type(size) is not int or not 1 <= size <= 24:
                raise ChatAPIError("invalid_page_size")
        except ChatAPIError as exc:
            callback(None, exc.code)
            return
        state = {"cursor": None, "seen": set(), "items": {}, "pages": 0}

        def fetch_page():
            state["pages"] += 1
            if state["pages"] > self.max_pages:
                callback(None, "page_limit")
                return
            request_cursor = state["cursor"]
            variables = {
                "pagination": {"first": size, "after": request_cursor},
                "filter": {"userId": account_id, "type": None, "status": None},
                "hasSupportAccess": False,
            }
            self.transport.request("userChats", variables, method="GET",
                                   callback=lambda raw, err: receive(raw, err, fetch_page, request_cursor))

        def receive(raw, error, next_page, request_cursor):
            if error:
                self._page_observed(page_observer, request_cursor is not None, error)
                callback(None, error)
                return
            try:
                connection = raw["data"]["chats"]
                edges = connection["edges"]
                page_info = connection["pageInfo"]
                if not isinstance(edges, list) or not isinstance(page_info, dict) or type(page_info.get("hasNextPage")) is not bool:
                    raise ChatAPIError("invalid_chat_page")
                for edge in edges:
                    chat = normalize_chat(edge.get("node") if isinstance(edge, dict) else None)
                    previous = state["items"].get(chat.id)
                    if previous is None:
                        state["items"][chat.id] = chat
                if page_info["hasNextPage"]:
                    cursor = page_info.get("endCursor")
                    if type(cursor) is not str or not cursor.strip() or cursor == state["cursor"] or cursor in state["seen"]:
                        raise ChatAPIError("repeated_cursor")
                    self._page_observed(page_observer, request_cursor is not None, None)
                    state["seen"].add(cursor)
                    state["cursor"] = cursor
                    next_page()
                else:
                    self._page_observed(page_observer, request_cursor is not None, None)
                    callback(tuple(state["items"].values()), None)
            except (KeyError, TypeError, ChatAPIError) as exc:
                code = exc.code if isinstance(exc, ChatAPIError) else "invalid_chat_page"
                self._page_observed(page_observer, request_cursor is not None, code)
                callback(None, code)

        fetch_page()

    def get_chat_messages(
        self, chat_id: str, callback, *, page_size: int | None = None, page_observer=None,
    ) -> None:
        try:
            chat_id = _canonical_id(chat_id)
            size = self.page_size if page_size is None else page_size
            if type(size) is not int or not 1 <= size <= 24:
                raise ChatAPIError("invalid_page_size")
        except ChatAPIError as exc:
            callback(None, exc.code)
            return
        state = {"cursor": None, "seen": set(), "items": {}, "pages": 0}

        def fetch_page():
            state["pages"] += 1
            if state["pages"] > self.max_pages:
                callback(None, "page_limit")
                return
            request_cursor = state["cursor"]
            variables = {
                "pagination": {"first": size, "after": request_cursor},
                "filter": {"chatId": chat_id},
                "hasSupportAccess": False,
                "showForbiddenImage": True,
            }
            self.transport.request("chatMessages", variables, method="GET",
                                   callback=lambda raw, err: receive(raw, err, request_cursor))

        def receive(raw, error, request_cursor):
            if error:
                self._page_observed(page_observer, request_cursor is not None, error)
                callback(None, error)
                return
            try:
                connection = raw["data"]["chatMessages"]
                edges = connection["edges"]
                page_info = connection["pageInfo"]
                if not isinstance(edges, list) or not isinstance(page_info, dict) or type(page_info.get("hasNextPage")) is not bool:
                    raise ChatAPIError("invalid_message_page")
                for edge in edges:
                    message = normalize_message(edge.get("node") if isinstance(edge, dict) else None, chat_id=chat_id)
                    state["items"].setdefault(message.id, message)
                if page_info["hasNextPage"]:
                    cursor = page_info.get("endCursor")
                    if type(cursor) is not str or not cursor.strip() or cursor == state["cursor"] or cursor in state["seen"]:
                        raise ChatAPIError("repeated_cursor")
                    self._page_observed(page_observer, request_cursor is not None, None)
                    state["seen"].add(cursor)
                    state["cursor"] = cursor
                    fetch_page()
                else:
                    self._page_observed(page_observer, request_cursor is not None, None)
                    callback(tuple(state["items"].values()), None)
            except (KeyError, TypeError, ChatAPIError) as exc:
                code = exc.code if isinstance(exc, ChatAPIError) else "invalid_message_page"
                self._page_observed(page_observer, request_cursor is not None, code)
                callback(None, code)

        fetch_page()

    def send_text_message(self, chat_id: str, text: str, callback) -> None:
        try:
            chat_id = _canonical_id(chat_id)
            if type(text) is not str or not text.strip() or len(text) > 8000:
                raise ChatAPIError("invalid_message_text")
        except ChatAPIError as exc:
            callback(None, exc.code)
            return
        variables = {"input": {"chatId": chat_id, "imagesIds": [], "text": text}}

        def received(raw, error):
            if error:
                # The request might have reached the server; mutations are never retried.
                callback(None, "unknown_result" if error in {"timeout", "network_error", "page_lost"} else error)
                return
            try:
                message = raw["data"]["createChatMessage"]
                callback(normalize_message(message, chat_id=chat_id), None)
            except (KeyError, TypeError, ChatAPIError):
                callback(None, "unknown_result")

        self.transport.request("createChatMessage", variables, method="POST", callback=received)


class WebEngineGraphQLTransport:
    """GraphQL fetch transport using the current page's credentials; exports no cookies."""

    def __init__(self, run_javascript, *, schedule, timeout_ms: int = 30_000):
        self._run_javascript = run_javascript
        self._schedule = schedule
        self.timeout_ms = timeout_ms
        self._active: dict[str, dict[str, Any]] = {}

    def _request_url_body(self, operation: str, variables: dict[str, Any], method: str):
        if operation == "viewer":
            if method != "GET":
                raise ChatAPIError("unsupported_method")
            return _VIEWER_URL, None
        if method == "GET":
            return _get_url(operation, variables), None
        if method != "POST" or operation != "createChatMessage":
            raise ChatAPIError("unsupported_method")
        return _GRAPHQL_URL, json.dumps({
            "operationName": operation,
            "query": QUERIES[operation],
            "variables": variables,
        }, separators=(",", ":"), ensure_ascii=False)

    def request(self, operation, variables, *, method, callback):
        try:
            url, body = self._request_url_body(operation, variables, method)
        except ChatAPIError as exc:
            callback(None, exc.code)
            return
        request_id = "__gd_playerok_chat_" + uuid4().hex
        mutation = method == "POST"
        state = {"callback": callback, "mutation": mutation, "completed": False, "steps": 0}
        self._active[request_id] = state
        options = {"method": method, "credentials": "include", "redirect": "error"}
        if operation != "viewer":
            # Match the non-secret GraphQL request context used by the pinned
            # Playerok Universal client. The persisted-query document and
            # variables are not sufficient on their own for Playerok's current
            # web GraphQL gateway.
            options["headers"] = {
                "accept": "*/*",
                "content-type": "application/json",
                "apollo-require-preflight": "true",
                "apollographql-client-name": "web",
                "x-gql-op": operation,
                "x-gql-path": "/",
                "x-timezone-offset": "-240",
                "x-apollo-operation-name": operation,
            }
        if body is not None:
            options["headers"].update({"accept": "application/json"})
            options["body"] = body
        options["signal"] = "__ABORT_SIGNAL__"
        options_json = json.dumps(options, separators=(",", ":"), ensure_ascii=False).replace(
            '"__ABORT_SIGNAL__"', "controller.signal"
        )
        script = (
            "(()=>{const key=" + json.dumps(request_id) + ";try{"
            "const controller=new AbortController();window[key]={state:'pending'};"
            "const timer=setTimeout(()=>controller.abort()," + str(self.timeout_ms) + ");"
            "fetch(" + json.dumps(url) + "," + options_json + ").then(async r=>{clearTimeout(timer);"
            "let payload;try{payload=await r.json()}catch(_){payload=null;}"
            "window[key]={state:'done',status:r.status,payload};},()=>{clearTimeout(timer);"
            "window[key]={state:'transport_error',reason:" + json.dumps("unknown_result" if mutation else "network_error") + "};});"
            "return key;}catch(_){return null;}})()"
        )
        try:
            self._run_javascript(script, lambda value: self._started(request_id, value))
        except Exception:
            self._finish(request_id, None, "unknown_result" if mutation else "page_lost")

    def _started(self, request_id: str, value: Any):
        if request_id not in self._active:
            return
        if value != request_id:
            state = self._active[request_id]
            self._finish(request_id, None, "unknown_result" if state["mutation"] else "page_lost")
            return
        self._poll(request_id)

    def _poll(self, request_id: str):
        state = self._active.get(request_id)
        if state is None or state["completed"]:
            return
        state["steps"] += 1
        if state["steps"] * 50 >= self.timeout_ms + 5000:
            self._finish(request_id, None, "unknown_result" if state["mutation"] else "timeout")
            return
        script = (
            "(()=>{try{const k=" + json.dumps(request_id) + ";const v=window[k];"
            "if(!v)return JSON.stringify({state:'missing'});if(v.state==='pending')return JSON.stringify({state:'pending'});"
            "delete window[k];return JSON.stringify(v);}catch(_){return JSON.stringify({state:'invalid'});}})()"
        )
        def received(value):
            if request_id not in self._active:
                return
            try:
                if type(value) is not str or len(value.encode("utf-8")) > _SAFE_CALLBACK_BYTES:
                    raise ValueError
                envelope = json.loads(value)
                if not isinstance(envelope, dict):
                    raise ValueError
            except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                current = self._active[request_id]
                self._finish(request_id, None, "unknown_result" if current["mutation"] else "invalid_response")
                return
            if envelope.get("state") == "pending":
                try:
                    self._schedule(lambda: self._poll(request_id))
                except Exception:
                    current = self._active.get(request_id)
                    if current:
                        self._finish(request_id, None, "unknown_result" if current["mutation"] else "page_lost")
                return
            if envelope.get("state") == "transport_error":
                self._finish(request_id, None, envelope.get("reason") if envelope.get("reason") in {"network_error", "unknown_result"} else "network_error")
                return
            if envelope.get("state") != "done" or type(envelope.get("status")) is not int:
                current = self._active[request_id]
                self._finish(request_id, None, "unknown_result" if current["mutation"] else "invalid_response")
                return
            if not 200 <= envelope["status"] < 300:
                current = self._active[request_id]
                error_code = _safe_graphql_error_code(envelope.get("payload"))
                category = error_code or f"http_{envelope['status']}"
                self._finish(request_id, None, "unknown_result" if current["mutation"] else category)
                return
            payload = envelope.get("payload")
            if not isinstance(payload, dict) or payload.get("errors"):
                current = self._active[request_id]
                category = _safe_graphql_error_code(payload) or "graphql_error"
                self._finish(request_id, None, "unknown_result" if current["mutation"] else category)
                return
            self._finish(request_id, payload, None)
        try:
            self._run_javascript(script, received)
        except Exception:
            current = self._active.get(request_id)
            if current:
                self._finish(request_id, None, "unknown_result" if current["mutation"] else "page_lost")

    def _finish(self, request_id: str, payload, error):
        state = self._active.pop(request_id, None)
        if state is None or state["completed"]:
            return
        state["completed"] = True
        state["callback"](payload, error)

    def cancel_all(self):
        for request_id in tuple(self._active):
            state = self._active.get(request_id)
            if state:
                self._finish(request_id, None, "unknown_result" if state["mutation"] else "page_lost")


def system_event_kind(message: PlayerokChatMessage) -> str | None:
    text = message.text or ""
    if text == "{{ITEM_PAID}}":
        return "item_paid"
    if text == "{{ITEM_SENT}}":
        return "item_sent"
    if text.startswith("{{DEAL_CONFIRMED") and text.endswith("}}"):
        return "deal_confirmed"
    return {
        "{{DEAL_ROLLED_BACK}}": "deal_rolled_back",
        "{{DEAL_HAS_PROBLEM}}": "deal_has_problem",
        "{{DEAL_PROBLEM_RESOLVED}}": "deal_problem_resolved",
    }.get(text)


class MessageDeduplicator:
    def __init__(self, max_ids: int = 10_000):
        self.max_ids = max_ids
        self._ids: OrderedDict[str, None] = OrderedDict()

    def claim(self, message_id: str) -> bool:
        if message_id in self._ids:
            self._ids.move_to_end(message_id)
            return False
        self._ids[message_id] = None
        if len(self._ids) > self.max_ids:
            self._ids.popitem(last=False)
        return True
