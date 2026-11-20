from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.database.db import Database
from integrations.playerok.graphql_http import (
    PlayerokAuthContext,
    PlayerokGraphQLHTTPTransport,
    PlayerokGraphQLTransportError,
    PlayerokReadOnlyGraphQLClient,
    PlayerokGraphQLSalesSnapshot,
)
from integrations.playerok.sync import PlayerokDealSyncService


def _node(deal_id, status, *, chat_id="chat-shared", listing_id="listing-exact"):
    return {
        "id": deal_id,
        "status": status,
        "createdAt": "2099-01-01T00:00:00Z",
        "chat": {"id": chat_id},
        "item": {"id": listing_id, "slug": "not-canonical"} if listing_id else {"slug": "not-canonical"},
        "user": {"id": "buyer-1"},
    }


def _response(payload):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = json.dumps(payload)

        def json(self):
            return payload

    return Response()


class _Session:
    def __init__(self, pages):
        self.pages = list(pages)

    def post(self, **_kwargs):
        return _response({"data": {"viewer": {"id": "seller-1", "username": "seller"}}})

    def get(self, **_kwargs):
        return _response(self.pages.pop(0))

    def close(self):
        pass


def _connection(nodes, *, has_next=False, cursor=None, total=None):
    page = {
        "edges": [{"node": node} for node in nodes],
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }
    if total is not None:
        page["totalCount"] = total
    return {"data": {"deals": page}}


def _snapshot(pages, *, page_size=24):
    client = PlayerokReadOnlyGraphQLClient(PlayerokGraphQLHTTPTransport(
        PlayerokAuthContext("synthetic-token"), session=_Session(pages), retries=0,
    ))
    return client.fetch_sales_snapshot(page_size=page_size)


class PlayerokGraphQLSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "graphql-sync.sqlite3")
        self.sync = PlayerokDealSyncService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_graphql_snapshots_share_account_baseline_and_deal_idempotency(self):
        baseline = _snapshot([_connection([
            _node("deal-old", "SENT"),
            _node("same-chat-second-deal", "CONFIRMED"),
        ], total=2)])
        first = self.sync.sync_graphql_snapshot(baseline)
        self.assertTrue(first["baseline"])
        self.assertEqual(first["historical_deals"], 2)
        self.assertEqual(first["new_deals"], 0)

        later = _snapshot([_connection([
            _node("deal-old", "PAID"),
            _node("same-chat-second-deal", "CONFIRMED"),
            _node("deal-new-paid", "PAID", chat_id="chat-shared", listing_id=None),
            _node("deal-no-config", "PAID", chat_id="chat-shared", listing_id="exact-unconfigured-listing"),
        ], total=4)])
        second = self.sync.sync_graphql_snapshot(later)
        self.assertFalse(second["baseline"])
        self.assertEqual(second["new_deals"], 2)
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT platform_status FROM gd_deals WHERE external_deal_id='deal-old'"
            ).fetchone()[0], "PAID")
        repeated = self.sync.sync_graphql_snapshot(
            _snapshot([_connection([
                _node("deal-old", "CONFIRMED"),
                _node("same-chat-second-deal", "CONFIRMED"),
                _node("deal-new-paid", "PAID", chat_id="chat-shared", listing_id=None),
                _node("deal-no-config", "PAID", chat_id="chat-shared", listing_id="exact-unconfigured-listing"),
            ], total=4)])
        )
        self.assertEqual(repeated["new_deals"], 0)

        with self.database.connect() as db:
            rows = db.execute(
                "SELECT external_deal_id,chat_id,listing_id,config_version_id,platform_status "
                "FROM gd_deals WHERE playerok_account_id='seller-1' ORDER BY external_deal_id"
            ).fetchall()
            self.assertEqual(len(rows), 4)
            by_id = {row[0]: row for row in rows}
            self.assertEqual(by_id["deal-new-paid"][1], "chat-shared")
            self.assertIsNone(by_id["deal-new-paid"][2])
            self.assertIsNone(by_id["deal-new-paid"][3])
            self.assertIsNotNone(by_id["deal-no-config"][2])
            self.assertIsNone(by_id["deal-no-config"][3])
            self.assertEqual(db.execute(
                "SELECT external_listing_id FROM gd_listings WHERE listing_id=?",
                (by_id["deal-no-config"][2],),
            ).fetchone()[0], "exact-unconfigured-listing")
            self.assertEqual(by_id["deal-old"][4], "CONFIRMED")
            for table in ("gd_orders", "gd_order_lines", "gd_delivery_legs", "gd_resource_reservations"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_first_complete_unfiltered_pages_create_one_historical_baseline(self):
        pages = [
            _connection([_node("d1", "PAID")], has_next=True, cursor="cursor-1", total=2),
            _connection([_node("d2", "CONFIRMED")], total=2),
        ]
        snapshot = _snapshot(pages, page_size=1)
        self.assertIsInstance(snapshot, PlayerokGraphQLSalesSnapshot)
        self.assertEqual(len(snapshot.deals), 2)
        result = self.sync.sync_graphql_snapshot(snapshot)
        self.assertTrue(result["baseline"])
        with self.database.connect() as db:
            self.assertEqual(db.execute(
                "SELECT baseline_completed FROM playerok_sync_state WHERE account_user_id='seller-1'"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals WHERE is_historical=1").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_config_versions").fetchone()[0], 0)

    def test_incomplete_pagination_does_not_issue_snapshot_or_complete_baseline(self):
        client = PlayerokReadOnlyGraphQLClient(PlayerokGraphQLHTTPTransport(
            PlayerokAuthContext("synthetic-token"),
            session=_Session([_connection([_node("d1", "PAID")], has_next=True, cursor="repeat")]),
            retries=0,
        ))
        with self.assertRaises(PlayerokGraphQLTransportError):
            client.fetch_sales_snapshot()
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_sync_state").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)

    def test_untrusted_or_wrong_account_snapshot_is_rejected(self):
        untrusted = PlayerokGraphQLSalesSnapshot("seller-1", [])
        with self.assertRaisesRegex(ValueError, "untrusted"):
            self.sync.sync_graphql_snapshot(untrusted)

        client = PlayerokReadOnlyGraphQLClient(PlayerokGraphQLHTTPTransport(
            PlayerokAuthContext("synthetic-token"),
            session=_Session([_connection([])]), retries=0,
        ))
        with self.assertRaisesRegex(PlayerokGraphQLTransportError, "account_mismatch"):
            client.fetch_sales_snapshot(expected_account_user_id="seller-other")

    def test_expired_session_never_completes_or_writes_baseline(self):
        class ExpiredSession(_Session):
            def post(self, **_kwargs):
                class UnauthorizedResponse:
                    status_code = 401
                    headers = {"content-type": "application/json"}
                    text = "{}"

                    @staticmethod
                    def json():
                        return {}

                return UnauthorizedResponse()

        client = PlayerokReadOnlyGraphQLClient(PlayerokGraphQLHTTPTransport(
            PlayerokAuthContext("synthetic-token"), session=ExpiredSession([]), retries=0,
        ))
        with self.assertRaisesRegex(PlayerokGraphQLTransportError, "session_expired"):
            client.fetch_sales_snapshot()
        with self.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playerok_sync_state").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gd_deals").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
