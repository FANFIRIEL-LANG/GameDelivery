"""Read-only Playerok sales reader using the active Qt WebEngine page session."""
from __future__ import annotations

import json
import weakref
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlencode
from uuid import uuid4

from integrations.playerok.client import PlayerokAdapter


_VIEWER_URL = "https://bff.playerok.com/rest-api/public/viewer"
_GRAPHQL_URL = "https://playerok.com/graphql"
_PAGE_SIZE = 24
_MAX_PAGES = 10_000
_DEALS_PERSISTED_QUERY_SHA256 = "591b0e6d036c2120c8f95b97dbfdf5635df3747cd901f4895e009935229417ef"
_POLL_INTERVAL_MS = 50
_SAFE_ERROR_CAUSES = frozenset({
    "AbortError", "NetworkError", "NotAllowedError", "RangeError", "ReferenceError",
    "RuntimeError", "SecurityError", "SyntaxError", "TypeError", "URIError", "ValueError",
    "QtError", "OtherError",
})
_DIAGNOSTIC_MARKERS = frozenset({
    "javascript_start_error", "request_id_error", "poll_state_error", "fetch_error",
    "http_error", "json_parse_error", "viewer_parse_error",
})
_SNAPSHOT_LOCK = Lock()
_TRUSTED_SNAPSHOTS = weakref.WeakKeyDictionary()


def _deals_page_url(account_user_id: str, page_size: int, cursor: str | None) -> str:
    """Build the observed read-only Playerok deals GET used by its paginated client."""
    pagination: dict[str, Any] = {"first": page_size, "after": cursor}
    variables = {
        "pagination": pagination,
        "filter": {"userId": account_user_id, "direction": "Out", "status": None},
        "showForbiddenImage": True,
    }
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": _DEALS_PERSISTED_QUERY_SHA256,
        }
    }
    query = urlencode({
        "operationName": "deals",
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(extensions, separators=(",", ":")),
    })
    return f"{_GRAPHQL_URL}?{query}"


class BrowserSalesSnapshot:
    """Immutable data container; construction alone never establishes provenance."""

    __slots__ = ("_account_user_id", "_deals_json", "_provenance_token", "__weakref__")

    def __init__(self, account_user_id: str, deals: list[dict[str, Any]]):
        object.__setattr__(self, "_account_user_id", str(account_user_id))
        object.__setattr__(self, "_deals_json", json.dumps(deals, ensure_ascii=False, separators=(",", ":")))
        object.__setattr__(self, "_provenance_token", None)

    def __setattr__(self, name, value):
        raise AttributeError("BrowserSalesSnapshot неизменяемый")

    @property
    def complete(self) -> bool:
        return is_trusted_browser_sales_snapshot(self)

    @property
    def account_user_id(self) -> str:
        return self._account_user_id

    @property
    def deals(self) -> list[dict[str, Any]]:
        return json.loads(self._deals_json)

    def get(self, key: str, default=None):
        if key == "complete":
            return self.complete
        if key == "account_user_id":
            return self._account_user_id
        if key == "deals":
            return self.deals
        return default

    def __getitem__(self, key: str):
        value = self.get(key, None)
        if value is None:
            raise KeyError(key)
        return value


def is_trusted_browser_sales_snapshot(value: object) -> bool:
    if not isinstance(value, BrowserSalesSnapshot):
        return False
    with _SNAPSHOT_LOCK:
        token = _TRUSTED_SNAPSHOTS.get(value)
        return token is not None and token is value._provenance_token


def _complete_browser_snapshot(account_user_id: str, deals: list[dict[str, Any]]) -> BrowserSalesSnapshot:
    """Build untrusted snapshot-shaped data; this helper cannot attest reader completion."""
    return BrowserSalesSnapshot(account_user_id, deals)


def consume_trusted_browser_sales_snapshot(value: object) -> dict[str, Any] | None:
    """Consume a reader-issued snapshot once, returning detached read-only data."""
    if not isinstance(value, BrowserSalesSnapshot):
        return None
    with _SNAPSHOT_LOCK:
        token = _TRUSTED_SNAPSHOTS.pop(value, None)
        if token is None or token is not value._provenance_token:
            return None
    return {
        "complete": True,
        "account_user_id": value.account_user_id,
        "deals": value.deals,
    }


def _start_fetch_script(
    url: str,
    request_id: str,
    *,
    result_kind: str = "deals",
) -> str:
    options = {"method": "GET", "credentials": "include"}
    diagnostic_guard = (
        "const diagnosticPayload=payload=>{if(payload===null||typeof payload!=='object'||"
        "!Object.prototype.hasOwnProperty.call(payload,'__diagnostic'))return {found:false};"
        "const marker=payload.__diagnostic;if(typeof marker!=='string'||"
        + json.dumps(sorted(_DIAGNOSTIC_MARKERS))
        + ".indexOf(marker)<0)return {found:true,payload:{__diagnostic:'poll_state_error',"
        "cause:'invalid_state'}};return {found:true,payload};};"
    )
    safe_result = (
        "payload=>{const diagnostic=diagnosticPayload(payload);if(diagnostic.found)return diagnostic.payload;"
        "if(payload&&payload.errors)return {__graphql_error:true};"
        "const data=payload&&payload.data;const viewer=(data&&data.viewer)||data||payload;"
        "return {id:viewer&&viewer.id};}"
        if result_kind == "viewer"
        else "payload=>{const diagnostic=diagnosticPayload(payload);if(diagnostic.found)return diagnostic.payload;"
             "return payload&&payload.errors?{__graphql_error:true}:payload;}"
    )
    safe_cause = _safe_cause_javascript()
    deals_operation = json.dumps(result_kind == "deals")
    return (
        "(()=>{const key=" + json.dumps(request_id) + ";" + diagnostic_guard + safe_cause
        + "const isDealsOperation=" + deals_operation + ";"
        + "const finish=(payload,httpStatus)=>{try{const result=(" + safe_result + ")(payload);"
        "if(isDealsOperation&&result&&typeof result==='object'&&!Array.isArray(result)&&"
        "Number.isInteger(httpStatus)&&httpStatus>=100&&httpStatus<=599)"
        "result.__gamedelivery_graphql_http_status=httpStatus;"
        "window[key]={state:'done',payload:result};}"
        "catch(error){window[key]={state:'done',payload:{__diagnostic:'javascript_start_error',"
        "cause:safeCause(error)}};}};"
        "try{window[key]={state:'pending'};"
        "fetch(" + json.dumps(url) + "," + json.dumps(options, separators=(",", ":")) + ")"
        ".then(r=>{if(!r.ok){finish({__diagnostic:'http_error',status:r.status},r.status);return;}"
        "try{r.json().then(payload=>finish(payload,r.status),"
        "()=>finish({__diagnostic:'json_parse_error'},r.status));}"
        "catch(_){finish({__diagnostic:'json_parse_error'},r.status);}},"
        "error=>finish({__diagnostic:'fetch_error',cause:safeCause(error)}));"
        "return key;}catch(error){return {__diagnostic:'javascript_start_error',"
        "cause:safeCause(error)};}})()"
    )


def _safe_cause_javascript() -> str:
    allowed = sorted(_SAFE_ERROR_CAUSES - {"RuntimeError", "ValueError", "QtError"})
    return (
        "const safeCause=e=>{try{const n=e&&typeof e.name==='string'?e.name:'';return "
        + json.dumps(allowed) + ".indexOf(n)>=0?n:'OtherError';}catch(_){return 'OtherError';}};"
    )


def _poll_script(request_id: str) -> str:
    key = json.dumps(request_id)
    poll_expression = (
        "(()=>{const key=" + key + ";" + _safe_cause_javascript()
        + "try{const value=window[key];if(!value)return {state:'missing'};"
        "if(typeof value!=='object'||Array.isArray(value))return {state:'invalid'};"
        "if(value.state==='pending')return {state:'pending'};"
        "if(value.state!=='done'||!Object.prototype.hasOwnProperty.call(value,'payload'))"
        "return {state:'invalid'};delete window[key];return value;}"
        "catch(error){return {state:'error',cause:safeCause(error)};}})()"
    )
    return (
        "(()=>{try{const envelope=(" + poll_expression + ");"
        "const serialized=JSON.stringify(envelope);"
        "if(typeof serialized==='string')return serialized;"
        "return JSON.stringify({state:'serialization_error'});"
        "}catch(_){return JSON.stringify({state:'serialization_error'});}})()"
    )


def _viewer_poll_script(request_id: str) -> str:
    """Use the same JSON-string envelope contract at both Qt callback boundaries."""
    return _poll_script(request_id)


def _decode_poll_envelope(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Strictly normalize a JSON-string runJavaScript polling result."""
    if type(value) is not str:
        return None, "invalid_callback"
    try:
        envelope = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(envelope, dict):
        return None, "invalid_envelope"

    state = envelope.get("state")
    states = {"pending", "done", "missing", "invalid", "error", "serialization_error"}
    if type(state) is not str or state not in states:
        return None, "unknown_state"
    expected_keys = {
        "pending": {"state"},
        "done": {"state", "payload"},
        "missing": {"state"},
        "invalid": {"state"},
        "error": {"state", "cause"},
        "serialization_error": {"state"},
    }[state]
    if set(envelope) != expected_keys:
        return None, "invalid_envelope"
    if state == "error" and type(envelope.get("cause")) is not str:
        return None, "invalid_envelope"
    if state == "done":
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return None, "invalid_payload"
        if "__diagnostic" in payload:
            marker = payload["__diagnostic"]
            if type(marker) is not str or marker not in _DIAGNOSTIC_MARKERS:
                return None, "invalid_diagnostic"
    return envelope, None


class BrowserSalesReader:
    """Fetch a complete, validated sales snapshot through page.runJavaScript."""

    def __init__(
        self,
        run_javascript: Callable[[str, Callable[[Any], None]], None],
        finished: Callable[[Any | None, str | None], None],
        *,
        schedule: Callable[[Callable[[], None]], None],
        on_request_started: Callable[[], None] | None = None,
        page_size: int = _PAGE_SIZE,
        max_pages: int = _MAX_PAGES,
    ):
        self._run_javascript = run_javascript
        self._finished = finished
        self._schedule = schedule
        self._on_request_started = on_request_started
        self.page_size = page_size
        self.max_pages = max_pages
        self._active = False
        self._account_id = ""
        self._deals: list[dict[str, Any]] = []
        self._seen: dict[str, dict[str, Any]] = {}
        self._cursors: set[str] = set()
        self._cursor: str | None = None
        self._total: int | None = None
        self._page_count = 0
        self._validated_page_count = 0
        self._validated_deal_count = 0
        self._pagination_complete = False
        self._initial_viewer_received = False
        self._viewer_matched: bool | None = None
        self._last_graphql_http_status: int | None = None
        self._provenance_token = object()

    @property
    def active(self) -> bool:
        return self._active

    @property
    def pages_read(self) -> int:
        """Number of sales pages fully validated (excludes a failed partial page)."""
        return self._validated_page_count

    @property
    def deals_read(self) -> int:
        """Unique deals from fully validated sales pages, without exposing their data."""
        return self._validated_deal_count

    @property
    def pagination_complete(self) -> bool:
        """True only after a valid terminal page and totalCount check."""
        return self._pagination_complete

    @property
    def initial_viewer_received(self) -> bool:
        return self._initial_viewer_received

    @property
    def viewer_matched(self) -> bool | None:
        """Final viewer comparison; None means the final viewer was not validated."""
        return self._viewer_matched

    @property
    def last_graphql_http_status(self) -> int | None:
        """Validated status of the latest GraphQL Deals response, if available."""
        return self._last_graphql_http_status

    def start(self) -> None:
        if self._active:
            raise RuntimeError("Playerok browser sales read is already active.")
        self._active = True
        self._fetch(_VIEWER_URL, self._on_viewer, result_kind="viewer")

    def cancel(self, *, stage: str = "cancelled") -> None:
        # Never expose arbitrary caller text: it might contain page/user data.
        messages = {
            "timeout": "poll_state_error: истекло время ожидания ответа WebEngine.",
            "page_destroyed": "poll_state_error: страница браузера закрыта или уничтожена во время чтения.",
            "cancelled": "poll_state_error: чтение браузерной страницы отменено.",
        }
        self._complete(None, messages.get(stage, messages["cancelled"]))

    def _complete(self, result: Any | None, error: str | None) -> None:
        if not self._active:
            return
        self._active = False
        self._finished(result, error)

    def _fetch(
        self,
        url: str,
        callback: Callable[[Any], None],
        *,
        result_kind: str = "deals",
    ) -> None:
        request_id = "__gamedelivery_playerok_" + uuid4().hex
        script = _start_fetch_script(url, request_id, result_kind=result_kind)
        if self._on_request_started is not None:
            try:
                self._on_request_started()
            except Exception as exc:
                callback({
                    "__diagnostic": "javascript_start_error",
                    "cause": self._safe_exception_cause(exc),
                })
                return

        def started(value: Any) -> None:
            if not self._active:
                return
            if isinstance(value, dict) and value.get("__diagnostic") == "javascript_start_error":
                callback(value)
                return
            if value != request_id:
                callback({"__diagnostic": "request_id_error"})
                return
            self._poll(request_id, callback)

        try:
            self._run_javascript(script, started)
        except Exception as exc:
            callback({
                "__diagnostic": "javascript_start_error",
                "cause": self._safe_exception_cause(exc),
            })

    def _poll(self, request_id: str, callback: Callable[[Any], None]) -> None:
        if not self._active:
            return

        def received(value: Any) -> None:
            if not self._active:
                return
            envelope, parse_error = _decode_poll_envelope(value)
            if parse_error is not None:
                callback({"__diagnostic": "poll_state_error", "cause": parse_error})
                return
            state = envelope["state"]
            if state == "pending":
                try:
                    self._schedule(lambda: self._poll(request_id, callback))
                except Exception as exc:
                    callback({
                        "__diagnostic": "poll_state_error",
                        "cause": self._safe_exception_cause(exc),
                    })
            elif state == "done":
                callback(envelope["payload"])
            elif state == "error":
                callback({
                    "__diagnostic": "poll_state_error",
                    "cause": self._safe_diagnostic_cause(envelope.get("cause")),
                })
            elif state == "serialization_error":
                callback({"__diagnostic": "poll_state_error", "cause": "serialization_error"})
            else:
                cause = "missing_state" if state == "missing" else "invalid_state"
                callback({"__diagnostic": "poll_state_error", "cause": cause})

        try:
            self._run_javascript(_poll_script(request_id), received)
        except Exception as exc:
            callback({
                "__diagnostic": "poll_state_error",
                "cause": self._safe_exception_cause(exc),
            })

    @staticmethod
    def _safe_diagnostic_cause(value: Any) -> str:
        if not isinstance(value, str):
            return "unknown"
        if value in _SAFE_ERROR_CAUSES or value in {
            "invalid_callback", "invalid_json", "invalid_envelope", "invalid_payload",
            "invalid_diagnostic", "unknown_state", "missing_state", "invalid_state",
            "serialization_error",
        }:
            return value
        return "unknown"

    @classmethod
    def _safe_exception_cause(cls, exc: Exception) -> str:
        name = type(exc).__name__
        if name in _SAFE_ERROR_CAUSES:
            return name
        return "QtError"

    @staticmethod
    def _response_error(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return "poll_state_error: callback вернул ответ неверного формата."
        if "__diagnostic" in payload:
            diagnostic = payload["__diagnostic"]
            if type(diagnostic) is not str or diagnostic not in _DIAGNOSTIC_MARKERS:
                return "poll_state_error: получен неизвестный или некорректный диагностический маркер."
        else:
            diagnostic = None
        if diagnostic == "javascript_start_error":
            cause = BrowserSalesReader._safe_diagnostic_cause(payload.get("cause"))
            return f"javascript_start_error: JavaScript/Qt WebEngine не запустил запрос ({cause})."
        if diagnostic == "request_id_error":
            return "request_id_error: callback JavaScript не вернул ожидаемый идентификатор запроса."
        if diagnostic == "poll_state_error":
            cause = BrowserSalesReader._safe_diagnostic_cause(payload.get("cause"))
            return f"poll_state_error: состояние запроса WebEngine потеряно или повреждено ({cause})."
        if diagnostic == "fetch_error":
            cause = BrowserSalesReader._safe_diagnostic_cause(payload.get("cause"))
            return f"fetch_error: браузерный fetch завершился исключением ({cause})."
        if diagnostic == "json_parse_error":
            return "json_parse_error: ответ получен, но не является корректным JSON."
        if diagnostic == "http_error":
            status = payload.get("status")
            if type(status) is int and 100 <= status <= 599:
                return f"http_error: Playerok ответил HTTP {status}."
            return "http_error: получен некорректный HTTP-статус."
        if diagnostic == "viewer_parse_error":
            return "viewer_parse_error: структура viewer некорректна."
        if payload.get("__graphql_error") is True:
            return "GraphQL Playerok вернул ошибку; снимок продаж не сохранён."
        if payload.get("errors"):
            return "GraphQL Playerok вернул ошибку; снимок продаж не сохранён."
        return None

    def _on_viewer(self, payload: Any) -> None:
        if not self._active:
            return
        error = self._response_error(payload)
        if error:
            self._complete(None, error)
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        viewer = data if isinstance(data, dict) else payload
        account_id = viewer.get("id") if isinstance(viewer, dict) else None
        if not isinstance(account_id, (str, int)) or isinstance(account_id, bool) or not str(account_id).strip():
            self._complete(None, "viewer_parse_error: JSON не содержит корректный viewer.id.")
            return
        self._account_id = str(account_id).strip()
        self._initial_viewer_received = True
        self._fetch_page()

    def _fetch_page(self) -> None:
        if not self._active:
            return
        self._page_count += 1
        if self._page_count > self.max_pages:
            self._complete(None, "Превышен защитный лимит страниц продаж Playerok.")
            return
        self._fetch(
            _deals_page_url(self._account_id, self.page_size, self._cursor),
            self._on_page,
        )

    def _on_page(self, payload: Any) -> None:
        if not self._active:
            return
        # A missing status on this latest response must not leave an older page's code visible.
        self._last_graphql_http_status = None
        if isinstance(payload, dict):
            status = payload.get("__gamedelivery_graphql_http_status")
            if status is None and payload.get("__diagnostic") == "http_error":
                status = payload.get("status")
            if type(status) is int and 100 <= status <= 599:
                self._last_graphql_http_status = status
        error = self._response_error(payload)
        if error:
            self._complete(None, error)
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        connection = data.get("deals") if isinstance(data, dict) else None
        if not isinstance(connection, dict) or not isinstance(connection.get("edges"), list):
            self._complete(None, "Playerok вернул неполную страницу продаж.")
            return
        for edge in connection["edges"]:
            if not isinstance(edge, dict) or not isinstance(edge.get("node"), dict):
                self._complete(None, "Playerok вернул некорректную запись продажи.")
                return
            try:
                mapped = PlayerokAdapter._map_deal(edge["node"])
            except (RuntimeError, TypeError, ValueError):
                self._complete(None, "В продаже отсутствуют обязательные идентификаторы.")
                return
            previous = self._seen.get(mapped["deal_id"])
            if previous is not None and previous != mapped:
                self._complete(None, "Playerok вернул конфликтующие записи одной сделки.")
                return
            if previous is None:
                self._seen[mapped["deal_id"]] = mapped
                self._deals.append(mapped)

        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict) or type(page_info.get("hasNextPage")) is not bool:
            self._complete(None, "Playerok вернул некорректный pageInfo.")
            return
        total_count = connection.get("totalCount")
        if "totalCount" in connection:
            if type(total_count) is not int or total_count < 0:
                self._complete(None, "Playerok вернул некорректный totalCount.")
                return
            if self._total is not None and self._total != total_count:
                self._complete(None, "Playerok изменил totalCount во время пагинации.")
                return
            self._total = total_count
        if page_info["hasNextPage"]:
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor.strip() or cursor in self._cursors or cursor == self._cursor:
                self._complete(None, "Playerok вернул пустой или повторный курсор пагинации.")
                return
            self._validated_page_count += 1
            self._validated_deal_count = len(self._seen)
            self._cursors.add(cursor)
            self._cursor = cursor
            self._fetch_page()
            return
        end_cursor = page_info.get("endCursor")
        if end_cursor is not None and not isinstance(end_cursor, str):
            self._complete(None, "Playerok вернул некорректный pageInfo.endCursor.")
            return
        if self._total is not None and self._total != len(self._seen):
            self._complete(None, "Количество сделок не совпало с totalCount; снимок неполный.")
            return
        self._validated_page_count += 1
        self._validated_deal_count = len(self._seen)
        self._pagination_complete = True
        # Verify viewer again after pagination so all pages are bound to one account.
        self._fetch(_VIEWER_URL, self._on_final_viewer, result_kind="viewer")

    @staticmethod
    def _viewer_id(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        viewer = data if isinstance(data, dict) else payload
        value = viewer.get("id") if isinstance(viewer, dict) else None
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
            return None
        return str(value).strip()

    def _on_final_viewer(self, payload: Any) -> None:
        if not self._active:
            return
        error = self._response_error(payload)
        if error:
            self._complete(None, error)
            return
        viewer_id = self._viewer_id(payload)
        if viewer_id is None:
            self._complete(None, "viewer_parse_error: JSON не содержит корректный viewer.id.")
            return
        if viewer_id != self._account_id:
            self._viewer_matched = False
            self._complete(None, "Аккаунт Playerok изменился во время чтения продаж; снимок отклонён.")
            return
        self._viewer_matched = True
        snapshot = BrowserSalesSnapshot(self._account_id, self._deals)
        object.__setattr__(snapshot, "_provenance_token", self._provenance_token)
        with _SNAPSHOT_LOCK:
            _TRUSTED_SNAPSHOTS[snapshot] = self._provenance_token
        self._complete(snapshot, None)


class BrowserViewerChecker:
    """Check only the current browser session's viewer identity; never expose the ID."""

    def __init__(
        self,
        run_javascript: Callable[[str, Callable[[Any], None]], None],
        finished: Callable[[bool, str | None], None],
        *,
        schedule: Callable[[Callable[[], None]], None],
        is_page_alive: Callable[[], bool] | None = None,
    ):
        self._run_javascript = run_javascript
        self._finished = finished
        self._schedule = schedule
        self._is_page_alive = is_page_alive or (lambda: True)
        self._active = False
        self._request_id = ""

    def _page_is_alive(self) -> bool:
        try:
            return bool(self._is_page_alive())
        except Exception:
            return False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._active:
            raise RuntimeError("Playerok viewer check is already active.")
        self._active = True
        self._request_id = "__gamedelivery_playerok_viewer_" + uuid4().hex
        script = _start_fetch_script(
            _VIEWER_URL, self._request_id, result_kind="viewer"
        )
        try:
            self._run_javascript(script, self._on_started)
        except Exception as exc:
            if not self._page_is_alive():
                self.cancel(stage="page_destroyed")
                return
            self._complete(
                False,
                "javascript_start_error: JavaScript/Qt WebEngine не запустил проверку "
                f"({BrowserSalesReader._safe_exception_cause(exc)}).",
            )

    def cancel(self, *, stage: str = "cancelled") -> None:
        messages = {
            "timeout": "timeout_error: истекло время проверки авторизации Playerok.",
            "page_destroyed": "page_destroyed_error: страница Playerok уничтожена во время проверки.",
            "cancelled": "poll_state_error: проверка авторизации Playerok отменена.",
        }
        self._complete(False, messages.get(stage, messages["cancelled"]))

    def _complete(self, authorized: bool, error: str | None) -> None:
        if not self._active:
            return
        self._active = False
        self._finished(bool(authorized), error)

    def _on_started(self, value: Any) -> None:
        if not self._active:
            return
        if not self._page_is_alive():
            self.cancel(stage="page_destroyed")
            return
        if isinstance(value, dict) and value.get("__diagnostic") == "javascript_start_error":
            self._complete(False, BrowserSalesReader._response_error(value))
            return
        if value != self._request_id:
            self._complete(
                False,
                "request_id_error: callback JavaScript не вернул ожидаемый идентификатор проверки.",
            )
            return
        self._poll()

    def _poll(self) -> None:
        if not self._active:
            return

        def received(value: Any) -> None:
            if not self._active:
                return
            if not self._page_is_alive():
                self.cancel(stage="page_destroyed")
                return
            envelope, parse_error = _decode_poll_envelope(value)
            if parse_error is not None:
                messages = {
                    "invalid_callback": "callback WebEngine вернул значение неверного типа.",
                    "invalid_json": "JSON-envelope опроса некорректен.",
                    "unknown_state": "получено неизвестное состояние опроса.",
                }
                detail = messages.get(parse_error, "структура envelope опроса некорректна.")
                self._complete(False, f"poll_state_error: {detail}")
                return
            state = envelope["state"]
            if state == "pending":
                try:
                    self._schedule(self._poll)
                except Exception as exc:
                    self._complete(
                        False,
                        "poll_state_error: ошибка опроса WebEngine "
                        f"({BrowserSalesReader._safe_exception_cause(exc)}).",
                    )
                return
            if state == "error":
                cause = BrowserSalesReader._safe_diagnostic_cause(envelope.get("cause"))
                self._complete(False, f"poll_state_error: состояние проверки WebEngine повреждено ({cause}).")
                return
            if state == "serialization_error":
                self._complete(False, "poll_state_error: JavaScript не смог сериализовать состояние проверки.")
                return
            if state != "done":
                self._complete(False, "poll_state_error: состояние проверки WebEngine отсутствует или повреждено.")
                return

            payload = envelope["payload"]
            error = BrowserSalesReader._response_error(payload)
            if error:
                self._complete(False, error)
                return
            viewer_id = payload.get("id") if isinstance(payload, dict) else None
            if (
                not isinstance(viewer_id, (str, int))
                or isinstance(viewer_id, bool)
                or not str(viewer_id).strip()
            ):
                self._complete(
                    False,
                    "viewer_parse_error: ответ Playerok не содержит корректный viewer.id.",
                )
                return
            # The identifier is deliberately not retained, logged, or returned.
            self._complete(True, None)

        try:
            self._run_javascript(_viewer_poll_script(self._request_id), received)
        except Exception as exc:
            if not self._page_is_alive():
                self.cancel(stage="page_destroyed")
                return
            self._complete(
                False,
                "poll_state_error: ошибка JavaScript/Qt WebEngine при чтении состояния "
                f"({BrowserSalesReader._safe_exception_cause(exc)}).",
            )
