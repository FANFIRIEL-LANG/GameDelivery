from __future__ import annotations

import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

from integrations.playerok.browser_chat_api import WebEngineGraphQLTransport
from integrations.playerok.graphql_http import (
    PlayerokBrowserGraphQLSalesReader,
    PlayerokGraphQLSalesSnapshot,
    is_fresh_trusted_graphql_sales_snapshot,
)


def deal(deal_id, *, chat_id=None, listing_id=None, status="PAID"):
    return {
        "id": deal_id,
        "status": status,
        "createdAt": "2026-11-20T10:00:00Z",
        "chat": {"id": chat_id} if chat_id else None,
        "item": {"id": listing_id, "slug": "untrusted-slug"} if listing_id else None,
        "user": {"id": "buyer-1"},
    }


def page(nodes, *, has_next=False, cursor=None, total=None):
    connection = {
        "edges": [{"node": node} for node in nodes],
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }
    if total is not None:
        connection["totalCount"] = total
    return {"data": {"deals": connection}}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, operation, variables, *, method, callback):
        self.calls.append((operation, variables, method))
        payload, error = self.responses.pop(0)
        callback(payload, error)


class PlayerokGraphQLBrowserSalesTests(unittest.TestCase):
    def test_current_webengine_transport_reads_viewer_and_full_deal_pages(self):
        payloads = [
            {"data": {"viewer": {"id": "seller-1"}}},
            page([deal("deal-1", chat_id="chat-1", listing_id="item-1")],
                 has_next=True, cursor="cursor-1", total=2),
            page([deal("deal-2", chat_id="chat-1", listing_id="item-2", status="SENT")], total=2),
        ]
        js_responses = []

        def run_javascript(script, callback):
            if "fetch(" in script:
                js_responses.append(script)
            if "return key" in script:
                match = re.search(r'const key=("[^"]+")', script)
                callback(json.loads(match.group(1)) if match else None)
            else:
                # Each mocked fetch result is returned by the next page poll.
                operation_payload = payloads.pop(0)
                callback(json.dumps({"state": "done", "status": 200, "payload": operation_payload}))

        transport = WebEngineGraphQLTransport(run_javascript, schedule=lambda callback: callback())
        outcomes = []
        reader = PlayerokBrowserGraphQLSalesReader(transport, lambda *args: outcomes.append(args), page_size=1)
        with patch(
            "integrations.playerok.browser_chat_api.uuid4",
            side_effect=lambda: SimpleNamespace(hex=f"req{len(js_responses)}"),
        ):
            reader.start()

        self.assertEqual(len(outcomes), 1)
        snapshot, error = outcomes[0]
        self.assertIsNone(error)
        self.assertIsInstance(snapshot, PlayerokGraphQLSalesSnapshot)
        self.assertTrue(is_fresh_trusted_graphql_sales_snapshot(snapshot))
        self.assertEqual(snapshot.account_user_id, "seller-1")
        self.assertEqual(
            [(row["deal_id"], row["chat_id"], row["listing_id"]) for row in snapshot.deals],
            [("deal-1", "chat-1", "item-1"), ("deal-2", "chat-1", "item-2")],
        )
        self.assertEqual([call[0] for call in transport_calls_from_scripts(js_responses)], ["viewer", "deals", "deals"])
        self.assertIn('"credentials":"include"', "\n".join(js_responses))
        decoded_scripts = unquote("\n".join(js_responses))
        self.assertIn('"after":"cursor-1"', decoded_scripts)
        self.assertNotIn('"after":null', decoded_scripts)
        self.assertNotIn('"status":null', decoded_scripts)
        self.assertEqual(reader.pages_read, 2)
        self.assertEqual(reader.deals_read, 2)
        self.assertTrue(reader.initial_viewer_received)
        self.assertTrue(reader.viewer_matched)
        self.assertTrue(reader.pagination_complete)
        for forbidden in ("document.cookie", "localStorage", "sessionStorage", "Authorization:"):
            self.assertNotIn(forbidden, "\n".join(js_responses))

    def test_fake_transport_requires_complete_pagination_and_expected_account(self):
        transport = FakeTransport([
            ({"data": {"viewer": {"id": "other-seller"}}}, None),
        ])
        outcomes = []
        reader = PlayerokBrowserGraphQLSalesReader(
            transport, lambda *args: outcomes.append(args), expected_account_user_id="seller-1"
        )
        reader.start()
        self.assertEqual(outcomes, [(None, "account_mismatch")])
        self.assertEqual([call[0] for call in transport.calls], ["viewer"])

        transport = FakeTransport([
            ({"data": {"viewer": {"id": "seller-1"}}}, None),
            (page([deal("deal-1")], has_next=True, cursor="c1", total=2), None),
            (page([deal("deal-2")], has_next=True, cursor="c1", total=2), None),
        ])
        outcomes = []
        PlayerokBrowserGraphQLSalesReader(
            transport, lambda *args: outcomes.append(args), page_size=1
        ).start()
        self.assertEqual(outcomes, [(None, "invalid_pagination")])

    def test_auth_expiry_and_incomplete_responses_fail_closed(self):
        transport = FakeTransport([(None, "unauthenticated")])
        outcomes = []
        reader = PlayerokBrowserGraphQLSalesReader(transport, lambda *args: outcomes.append(args))
        reader.start()
        self.assertEqual(outcomes, [(None, "session_expired")])
        self.assertTrue(reader.session_lost)

        transport = FakeTransport([
            ({"data": {"viewer": {"id": "seller-1"}}}, None),
            (page([deal("deal-1")], total=2), None),
        ])
        outcomes = []
        PlayerokBrowserGraphQLSalesReader(
            transport, lambda *args: outcomes.append(args)
        ).start()
        self.assertEqual(outcomes, [(None, "incomplete_snapshot")])

    def test_reader_cancel_completes_once_without_starting_more_pages(self):
        transport = FakeTransport([])
        outcomes = []
        reader = PlayerokBrowserGraphQLSalesReader(transport, lambda *args: outcomes.append(args))
        reader.cancel(stage="page_destroyed")
        reader.start()
        self.assertEqual(outcomes, [(None, "page_lost")])
        self.assertEqual(transport.calls, [])


def transport_calls_from_scripts(scripts):
    operations = []
    for script in scripts:
        if "/rest-api/public/viewer" in script:
            operations.append("viewer")
        elif "operationName=deals" in script:
            operations.append("deals")
    return [(operation,) for operation in operations]


if __name__ == "__main__":
    unittest.main()
