import unittest

from integrations.playerok.client import PlayerokAdapter, _BFF, _GRAPHQL


class MemoryKeyring:
    def get_password(self, service, account):
        return "test-session-token"

    def set_password(self, service, account, value):
        pass

    def delete_password(self, service, account):
        pass


class PlayerokReadOnlySalesTests(unittest.TestCase):
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
                                "item": {"id": "item-1", "name": "Game item"},
                                "user": {"id": "buyer-1", "username": "buyer_name"},
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
        self.assertEqual(orders, [{
            "platform": "playerok",
            "order_id": "deal-123",
            "title": "Game item",
            "buyer": "buyer_name",
            "status": "PAID",
            "amount": "125.5",
            "created_at": "2026-10-27T00:00:00Z",
            "url": "https://playerok.com/deal/deal-123",
        }])


if __name__ == "__main__":
    unittest.main()
