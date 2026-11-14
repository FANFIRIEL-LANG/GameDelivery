import unittest

from integrations.playerok.client import PlayerokAdapter, _BFF, _GRAPHQL


_MISSING = object()


class MemoryKeyring:
    def get_password(self, service, account):
        return "test-session-token"

    def set_password(self, service, account, value):
        pass

    def delete_password(self, service, account):
        pass


class PlayerokReadOnlySalesTests(unittest.TestCase):
    @staticmethod
    def _deal_node(deal_id, *, status="PAID"):
        return {
            "id": deal_id,
            "status": status,
            "createdAt": "2026-11-01T00:00:00Z",
            "item": {"id": "listing-1", "name": "Game item"},
            "user": {"id": "buyer-1", "username": "buyer"},
            "chat": {"id": "chat-1"},
            "transaction": {"value": 10},
        }

    @classmethod
    def _sales_page(cls, deal_ids=(), *, has_next=False, end_cursor=_MISSING, total_count=_MISSING):
        page_info = {"hasNextPage": has_next}
        if end_cursor is not _MISSING:
            page_info["endCursor"] = end_cursor
        connection = {
            "edges": [{"node": cls._deal_node(deal_id)} for deal_id in deal_ids],
            "pageInfo": page_info,
        }
        if total_count is not _MISSING:
            connection["totalCount"] = total_count
        return {"data": {"deals": connection}}

    @staticmethod
    def _adapter_for_pages(pages):
        requests = []

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            requests.append(payload)
            return pages[len(requests) - 1], None

        return PlayerokAdapter(
            keyring_backend=MemoryKeyring(), request_func=fake_request,
        ), requests

    def test_profile_uses_bff_bearer_and_parses_identity(self):
        requests = []

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            requests.append((url, method, payload, token, bearer))
            return ({"id": "user-42", "username": "seller_name"}, None)

        adapter = PlayerokAdapter(
            keyring_backend=MemoryKeyring(),
            request_func=fake_request,
        )
        profile = adapter._profile("test-session-token", True)

        self.assertEqual(requests[0][0], _BFF + "/viewer")
        self.assertEqual(requests[0][3], "test-session-token")
        self.assertTrue(requests[0][4])
        self.assertEqual(profile["user_id"], "user-42")
        self.assertEqual(profile["username"], "seller_name")
        self.assertTrue(profile["connected"])

    def test_sales_query_uses_out_paid_sent_and_maps_deal_fields(self):
        requests = []

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            requests.append((url, method, payload, token, bearer))
            return ({
                "data": {
                    "deals": {
                        "edges": [{
                            "node": {
                                "id": "deal-123",
                                "status": "PAID",
                                "createdAt": "2026-10-27T00:00:00Z",
                                "item": {"id": "listing-1", "name": "Game item"},
                                "user": {"id": "buyer-1", "username": "buyer_name"},
                                "chat": {"id": "chat-123", "unreadMessagesCounter": 8},
                                "transaction": {"value": 125.5},
                            }
                        }],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": 1,
                    }
                }
            }, None)

        adapter = PlayerokAdapter(
            keyring_backend=MemoryKeyring(),
            request_func=fake_request,
        )
        orders = adapter.fetch_orders()

        self.assertEqual(len(requests), 1)
        url, method, payload, token, bearer = requests[0]
        self.assertEqual(url, _GRAPHQL)
        self.assertEqual(method, "POST")
        self.assertEqual(token, "test-session-token")
        self.assertFalse(bearer)
        self.assertEqual(payload["operationName"], "Deals")
        self.assertEqual(
            payload["variables"]["filter"],
            {"direction": "OUT", "status": ["PAID", "SENT"]},
        )
        self.assertIn("deals(pagination: $pagination, filter: $filter, sort: $sort)", payload["query"])
        self.assertIn("chat { id }", payload["query"])
        self.assertNotIn("unreadMessagesCounter", payload["query"])
        self.assertEqual(orders, [{
            "platform": "playerok",
            "deal_id": "deal-123",
            "order_id": "deal-123",
            "chat_id": "chat-123",
            "listing_id": "listing-1",
            "title": "Game item",
            "buyer": "buyer_name",
            "buyer_id": "buyer-1",
            "status": "PAID",
            "amount": "125.5",
            "created_at": "2026-10-27T00:00:00Z",
            "deal_url": "https://playerok.com/deal/deal-123",
            "chat_url": "https://playerok.com/chats/chat-123",
            "url": "https://playerok.com/deal/deal-123",
        }])


    def test_sales_fetches_every_cursor_page_and_never_uses_unread_counts(self):
        requests = []
        pages = [
            {
                "data": {"deals": {
                    "edges": [{"node": {
                        "id": "deal-1", "status": "PAID", "createdAt": "2026-11-01",
                        "item": {"id": "listing-1", "name": "One"},
                        "user": {"id": "buyer-1", "username": "buyer"},
                        "chat": {"id": "chat-1", "unreadMessagesCounter": 99},
                        "transaction": {"value": 10},
                    }}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                }}
            },
            {
                "data": {"deals": {
                    "edges": [{"node": {
                        "id": "deal-2", "status": "SENT", "createdAt": "2026-11-02",
                        "item": {"id": "listing-2", "name": "Two"},
                        "user": {"id": "buyer-2", "username": "buyer2"},
                        "chat": {"id": "chat-2", "unreadMessagesCounter": 1},
                        "transaction": {"value": 20},
                    }}],
                    "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
                }}
            },
        ]

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            requests.append(payload)
            return pages[len(requests) - 1], None

        adapter = PlayerokAdapter(keyring_backend=MemoryKeyring(), request_func=fake_request)
        deals = adapter.fetch_sales(statuses=None, first=1)

        self.assertEqual([deal["deal_id"] for deal in deals], ["deal-1", "deal-2"])
        self.assertEqual([deal["listing_id"] for deal in deals], ["listing-1", "listing-2"])
        self.assertEqual([deal["chat_id"] for deal in deals], ["chat-1", "chat-2"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["variables"]["pagination"], {"first": 1})
        self.assertEqual(
            requests[1]["variables"]["pagination"],
            {"first": 1, "after": "cursor-1"},
        )
        self.assertEqual(requests[0]["variables"]["filter"], {"direction": "OUT"})

    def test_page_info_requires_boolean_has_next_page(self):
        for has_next in (_MISSING, None, "false", 0, 1):
            with self.subTest(has_next=has_next):
                page = self._sales_page()
                page_info = page["data"]["deals"]["pageInfo"]
                if has_next is _MISSING:
                    page_info.pop("hasNextPage")
                else:
                    page_info["hasNextPage"] = has_next
                adapter, _ = self._adapter_for_pages([page])
                with self.assertRaisesRegex(RuntimeError, "hasNextPage"):
                    adapter.fetch_sales(statuses=None)

    def test_page_info_must_be_an_object(self):
        for page_info in (None, "done", []):
            with self.subTest(page_info=page_info):
                page = self._sales_page()
                page["data"]["deals"]["pageInfo"] = page_info
                adapter, _ = self._adapter_for_pages([page])
                with self.assertRaisesRegex(RuntimeError, "pageInfo"):
                    adapter.fetch_sales(statuses=None)

    def test_next_page_requires_nonempty_cursor(self):
        for cursor in (_MISSING, None, "", "   ", 123):
            with self.subTest(cursor=cursor):
                page = self._sales_page(has_next=True, end_cursor=cursor)
                adapter, _ = self._adapter_for_pages([page])
                with self.assertRaisesRegex(RuntimeError, "endCursor"):
                    adapter.fetch_sales(statuses=None)

    def test_repeated_cursor_fails_closed(self):
        pages = [
            self._sales_page(["deal-1"], has_next=True, end_cursor="cursor-1"),
            self._sales_page(["deal-2"], has_next=True, end_cursor="cursor-1"),
        ]
        adapter, _ = self._adapter_for_pages(pages)
        with self.assertRaisesRegex(RuntimeError, "курсор"):
            adapter.fetch_sales(statuses=None)

    def test_total_count_must_be_a_nonnegative_integer_not_bool(self):
        for total_count in (True, False, None, "1", 1.0, -1):
            with self.subTest(total_count=total_count):
                page = self._sales_page(total_count=total_count)
                adapter, _ = self._adapter_for_pages([page])
                with self.assertRaisesRegex(RuntimeError, "totalCount"):
                    adapter.fetch_sales(statuses=None)

    def test_total_count_must_match_unique_deal_count(self):
        page = self._sales_page(["deal-1"], total_count=2)
        adapter, _ = self._adapter_for_pages([page])
        with self.assertRaisesRegex(RuntimeError, "totalCount"):
            adapter.fetch_sales(statuses=None)

    def test_total_count_must_not_change_between_pages(self):
        pages = [
            self._sales_page(["deal-1"], has_next=True, end_cursor="cursor-1", total_count=2),
            self._sales_page(["deal-2"], total_count=3),
        ]
        adapter, _ = self._adapter_for_pages(pages)
        with self.assertRaisesRegex(RuntimeError, "totalCount"):
            adapter.fetch_sales(statuses=None)

    def test_last_page_may_omit_end_cursor(self):
        adapter, _ = self._adapter_for_pages([self._sales_page(["deal-1"], total_count=1)])
        deals = adapter.fetch_sales(statuses=None)
        self.assertEqual([deal["deal_id"] for deal in deals], ["deal-1"])

    def test_identical_duplicate_deal_is_deduplicated(self):
        node = self._deal_node("deal-1")
        first = self._sales_page(["deal-1"], has_next=True, end_cursor="cursor-1", total_count=2)
        second = self._sales_page(["deal-2"], total_count=2)
        second["data"]["deals"]["edges"].insert(0, {"node": node})
        adapter, _ = self._adapter_for_pages([first, second])
        deals = adapter.fetch_sales(statuses=None)
        self.assertEqual([deal["deal_id"] for deal in deals], ["deal-1", "deal-2"])

    def test_conflicting_duplicate_deal_fails(self):
        first = self._sales_page(["deal-1"], has_next=True, end_cursor="cursor-1")
        second = self._sales_page(["deal-1"], total_count=1)
        second["data"]["deals"]["edges"][0]["node"]["status"] = "SENT"
        adapter, _ = self._adapter_for_pages([first, second])
        with self.assertRaisesRegex(RuntimeError, "конфликтующие записи"):
            adapter.fetch_sales(statuses=None)

    def test_update_deal_status_only_allows_sent_and_uses_update_deal_input(self):
        requests = []

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            requests.append(payload)
            if payload["operationName"] == "Deal":
                return {"data": {"deal": {
                    "id": "deal-123", "status": "PAID", "createdAt": "",
                    "item": {"id": "listing-1", "name": "Game item"},
                    "user": {"id": "buyer-1", "username": "buyer"},
                    "chat": {"id": "chat-1"},
                    "transaction": {"value": 12},
                }}}, None
            return {"data": {"updateDeal": {
                "id": "deal-123", "status": "SENT", "createdAt": "",
                "item": {"id": "listing-1", "name": "Game item"},
                "user": {"id": "buyer-1", "username": "buyer"},
                "chat": {"id": "chat-1"},
                "transaction": {"value": 12},
            }}}, None

        adapter = PlayerokAdapter(keyring_backend=MemoryKeyring(), request_func=fake_request)
        result = adapter.update_deal_status("deal-123", "SENT")
        self.assertEqual(result["status"], "SENT")
        self.assertEqual(requests[0]["operationName"], "Deal")
        self.assertEqual(requests[1]["operationName"], "UpdateDeal")
        self.assertEqual(
            requests[1]["variables"]["input"],
            {"id": "deal-123", "status": "SENT"},
        )
        with self.assertRaises(ValueError):
            adapter.update_deal_status("deal-123", "CONFIRMED")


    def test_missing_page_info_fails_closed_instead_of_completing_baseline(self):
        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            return {"data": {"deals": {"edges": []}}}, None

        adapter = PlayerokAdapter(keyring_backend=MemoryKeyring(), request_func=fake_request)
        with self.assertRaisesRegex(RuntimeError, "pageInfo"):
            adapter.fetch_sales(statuses=None)


if __name__ == "__main__":
    unittest.main()
