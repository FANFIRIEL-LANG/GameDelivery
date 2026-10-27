import unittest
from unittest.mock import MagicMock, patch

from integrations.playerok.client import PlayerokAdapter, _GRAPHQL


class PlayerokGraphQLHeaderTests(unittest.TestCase):
    def test_graphql_request_sets_frontend_operation_headers(self):
        response = MagicMock()
        response.read.return_value = b'{"data": {"deals": {"edges": []}}}'
        response.headers.get_all.return_value = None

        response_context = MagicMock()
        response_context.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response_context

        payload = {
            "operationName": "Deals",
            "query": "query Deals { deals { edges { node { id } } } }",
            "variables": {},
        }

        with patch(
            "integrations.playerok.client.build_opener",
            return_value=opener,
        ):
            adapter = PlayerokAdapter()
            adapter._request(
                _GRAPHQL,
                method="POST",
                payload=payload,
                token="test-token",
            )

        request = opener.open.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers.get("apollo-require-preflight"), "true")
        self.assertEqual(headers.get("x-gql-op"), "Deals")
        self.assertEqual(headers.get("x-apollo-operation-name"), "Deals")


if __name__ == "__main__":
    unittest.main()
