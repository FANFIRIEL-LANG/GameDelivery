"""Test helper that obtains snapshots by driving BrowserSalesReader to completion."""
from __future__ import annotations

import json
import re

from integrations.playerok.browser_sales import BrowserSalesReader


def reader_completed_snapshot(account_user_id: str, deals: list[dict]):
    raw_deals = []
    for index, deal in enumerate(deals):
        item = {"name": deal.get("title") or "Test listing"}
        listing_id = str(deal.get("listing_id") or "").strip()
        if listing_id:
            item["id"] = listing_id
        source_listing_ref = str(deal.get("source_listing_ref") or "").strip()
        if source_listing_ref:
            item["slug"] = source_listing_ref
        created_at = deal["created_at"] if "created_at" in deal else "2099-01-01T00:00:00Z"
        raw = {
            "id": deal["deal_id"],
            "status": deal["status"],
            "createdAt": str(created_at or ""),
            "item": item,
            "user": {"id": f"buyer-{index}", "username": f"buyer{index}"},
            "transaction": {"value": 1},
        }
        chat_id = str(deal.get("chat_id") or "").strip()
        if chat_id:
            raw["chat"] = {"id": chat_id}
        raw_deals.append(raw)
    responses = [
        {"id": account_user_id},
        {"data": {"deals": {
            "edges": [{"node": deal} for deal in raw_deals],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "totalCount": len(raw_deals),
        }}},
        {"id": account_user_id},
    ]
    results = []

    def run_javascript(script, callback):
        if "return key;}catch(error)" in script:
            match = re.search(r"const key=(\"[^\"]+\")", script)
            callback(json.loads(match.group(1)))
            return
        callback(json.dumps({"state": "done", "payload": responses.pop(0)}))

    reader = BrowserSalesReader(
        run_javascript,
        lambda snapshot, error: results.append((snapshot, error)),
        schedule=lambda callback: callback(),
    )
    reader.start()
    if len(results) != 1 or results[0][1] is not None:
        raise AssertionError(f"BrowserSalesReader test fixture did not complete: {results!r}")
    if responses:
        raise AssertionError("BrowserSalesReader fixture left unconsumed response payloads")
    return results[0][0]
