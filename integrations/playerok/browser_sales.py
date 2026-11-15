"""Read-only Playerok sales reader using the active Qt WebEngine page session."""
from __future__ import annotations

import json
from typing import Any, Callable
from uuid import uuid4

from integrations.playerok.client import PlayerokAdapter


_VIEWER_URL = "https://bff.playerok.com/rest-api/public/viewer"
_GRAPHQL_URL = "https://playerok.com/graphql"
_PAGE_SIZE = 50
_MAX_PAGES = 10_000
_POLL_INTERVAL_MS = 50
_DEALS_QUERY = """
query Deals($pagination: Pagination, $filter: ItemDealFilter!, $sort: Sort) {
  deals(pagination: $pagination, filter: $filter, sort: $sort) {
    edges { node { id status createdAt item { id name } user { id username } chat { id } transaction { value } } }
    pageInfo { hasNextPage endCursor }
    totalCount
  }
}
"""


def _start_fetch_script(
    url: str,
    request_id: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    operation: str = "",
    result_kind: str = "deals",
) -> str:
    options: dict[str, Any] = {"method": method, "credentials": "include"}
    if body is not None:
        options["headers"] = {
            "Accept": "application/json", "Content-Type": "application/json",
            "apollo-require-preflight": "true", "x-gql-op": operation,
            "x-apollo-operation-name": operation,
        }
        options["body"] = json.dumps(body, separators=(",", ":"))
    safe_result = (
        "payload=>{if(payload&&(payload.__http_status!==undefined||payload.__transport_error))return payload;"
        "if(payload&&payload.errors)return {__graphql_error:true};"
        "const data=payload&&payload.data;const viewer=(data&&data.viewer)||data||payload;"
        "return {id:viewer&&viewer.id};}"
        if result_kind == "viewer"
        else "payload=>payload&&(payload.__http_status!==undefined||payload.__transport_error)?payload:"
             "(payload&&payload.errors?{__graphql_error:true}:payload)"
    )
    return (
        "(()=>{const key=" + json.dumps(request_id) + ";window[key]={state:'pending'};"
        "fetch(" + json.dumps(url) + "," + json.dumps(options, separators=(",", ":")) + ")"
        ".then(r=>{if(!r.ok)return {__http_status:r.status};"
        "return r.json().catch(()=>({__transport_error:true}));})"
        ".then(payload=>{window[key]={state:'done',payload:(" + safe_result + ")(payload)};})"
        ".catch(()=>{window[key]={state:'done',payload:{__transport_error:true}};});"
        "return key;})()"
    )


def _poll_script(request_id: str) -> str:
    key = json.dumps(request_id)
    return (
        "(()=>{const key=" + key + ";const value=window[key];"
        "if(!value)return {state:'missing'};"
        "if(value.state==='pending')return {state:'pending'};"
        "delete window[key];return value;})()"
    )


class BrowserSalesReader:
    """Fetch a complete, validated sales snapshot through page.runJavaScript."""

    def __init__(
        self,
        run_javascript: Callable[[str, Callable[[Any], None]], None],
        finished: Callable[[dict[str, Any] | None, str | None], None],
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

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._active:
            raise RuntimeError("Playerok browser sales read is already active.")
        self._active = True
        self._fetch(_VIEWER_URL, self._on_viewer, result_kind="viewer")

    def cancel(self, reason: str = "Страница браузера закрыта во время чтения продаж.") -> None:
        self._complete(None, reason)

    def _complete(self, result: dict[str, Any] | None, error: str | None) -> None:
        if not self._active:
            return
        self._active = False
        self._finished(result, error)

    def _fetch(
        self,
        url: str,
        callback: Callable[[Any], None],
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        operation: str = "",
        result_kind: str = "deals",
    ) -> None:
        request_id = "__gamedelivery_playerok_" + uuid4().hex
        script = _start_fetch_script(
            url, request_id, method=method, body=body, operation=operation,
            result_kind=result_kind,
        )
        if self._on_request_started is not None:
            self._on_request_started()

        def started(value: Any) -> None:
            if not self._active:
                return
            if value != request_id:
                callback({"__transport_error": True})
                return
            self._poll(request_id, callback)

        self._run_javascript(script, started)

    def _poll(self, request_id: str, callback: Callable[[Any], None]) -> None:
        if not self._active:
            return

        def received(value: Any) -> None:
            if not self._active:
                return
            if not isinstance(value, dict):
                callback({"__transport_error": True})
                return
            state = value.get("state")
            if state == "pending":
                self._schedule(lambda: self._poll(request_id, callback))
            elif state == "done":
                callback(value.get("payload"))
            else:
                callback({"__transport_error": True})

        self._run_javascript(_poll_script(request_id), received)

    @staticmethod
    def _response_error(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return "Playerok вернул пустой или некорректный ответ браузеру."
        if payload.get("__transport_error") is True:
            return "Не удалось выполнить read-only запрос из страницы Playerok."
        if payload.get("__graphql_error") is True:
            return "GraphQL Playerok вернул ошибку; снимок продаж не сохранён."
        status = payload.get("__http_status")
        if type(status) is int:
            return f"Playerok ответил HTTP {status}."
        if payload.get("errors"):
            return "GraphQL Playerok вернул ошибку; снимок продаж не сохранён."
        return None

    def _on_viewer(self, payload: Any) -> None:
        if not self._active:
            return
        error = self._response_error(payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        viewer = data if isinstance(data, dict) else payload
        account_id = viewer.get("id") if isinstance(viewer, dict) else None
        if error or not isinstance(account_id, (str, int)) or isinstance(account_id, bool) or not str(account_id).strip():
            self._complete(None, error or "Не удалось определить ID текущего аккаунта Playerok.")
            return
        self._account_id = str(account_id).strip()
        self._fetch_page()

    def _fetch_page(self) -> None:
        if not self._active:
            return
        self._page_count += 1
        if self._page_count > self.max_pages:
            self._complete(None, "Превышен защитный лимит страниц продаж Playerok.")
            return
        pagination: dict[str, Any] = {"first": self.page_size}
        if self._cursor is not None:
            pagination["after"] = self._cursor
        body = {
            "operationName": "Deals", "query": _DEALS_QUERY,
            "variables": {"filter": {"direction": "OUT"}, "sort": None, "pagination": pagination},
        }
        self._fetch(_GRAPHQL_URL, self._on_page, method="POST", body=body, operation="Deals")

    def _on_page(self, payload: Any) -> None:
        if not self._active:
            return
        error = self._response_error(payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        connection = data.get("deals") if isinstance(data, dict) else None
        if error or not isinstance(connection, dict) or not isinstance(connection.get("edges"), list):
            self._complete(None, error or "Playerok вернул неполную страницу продаж.")
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
        self._complete({"account_user_id": self._account_id, "deals": self._deals}, None)
