import json
import unittest
from integrations.playerok.client import KEYRING_ACCOUNT, KEYRING_SERVICE, PlayerokAdapter, _USER_AGENT
from integrations.playerok.graphql_http import (
    PlayerokAuthContext,
    PlayerokGraphQLHTTPTransport,
    PlayerokGraphQLTransportError,
    PlayerokReadOnlyGraphQLClient,
)


TOKEN = "synthetic-token-sentinel"


class MemoryKeyring:
    def __init__(self, value):
        self.value = value

    def get_password(self, service, account):
        assert service == KEYRING_SERVICE
        assert account == KEYRING_ACCOUNT
        return self.value

    def set_password(self, service, account, value):
        assert service == KEYRING_SERVICE
        assert account == KEYRING_ACCOUNT
        self.value = value


class FakeResponse:
    def __init__(self, payload=None, *, status=200, text=None, content_type="application/json"):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("synthetic malformed payload")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def post(self, **kwargs):
        self.calls.append(("post", kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


def deal_node(deal_id, *, status="PAID", chat_id="chat-1", listing_id="listing-1", slug="untrusted-slug"):
    return {
        "id": deal_id,
        "status": status,
        "createdAt": "2026-11-01T00:00:00Z",
        "chat": {"id": chat_id} if chat_id else None,
        "item": {"id": listing_id, "slug": slug} if listing_id or slug else None,
        "user": {"id": "buyer-1"},
    }


def connection(key, nodes, *, has_next=False, cursor=None, total=None):
    info = {"hasNextPage": has_next, "endCursor": cursor}
    result = {"edges": [{"node": node} for node in nodes], "pageInfo": info}
    if total is not None:
        result["totalCount"] = total
    return {"data": {key: result}}


class PlayerokGraphQLHTTPTests(unittest.TestCase):
    def make_client(self, responses, **kwargs):
        session = FakeSession(responses)
        transport = PlayerokGraphQLHTTPTransport(
            PlayerokAuthContext(TOKEN), session=session, sleeper=lambda _delay: None, **kwargs
        )
        return PlayerokReadOnlyGraphQLClient(transport), transport, session

    def test_headers_cookie_and_matching_user_agent(self):
        client, _transport, session = self.make_client([FakeResponse({"data": {"viewer": {"id": "seller-1", "username": "shop"}}})])
        self.assertEqual(client.get_viewer(), {"user_id": "seller-1", "username": "shop"})
        method, call = session.calls[0]
        self.assertEqual(method, "post")
        headers = {key.lower(): value for key, value in call["headers"].items()}
        self.assertEqual(headers["user-agent"], _USER_AGENT)
        self.assertEqual(headers["cookie"], "token=" + TOKEN)
        self.assertEqual(headers["authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["origin"], "https://playerok.com")
        self.assertEqual(headers["referer"], "https://playerok.com/")
        self.assertEqual(headers["apollo-require-preflight"], "true")
        self.assertEqual(headers["apollographql-client-name"], "web")
        self.assertEqual(headers["x-gql-op"], "viewer")
        self.assertEqual(call["json"]["operationName"], "viewer")
        self.assertEqual(call["json"]["query"], "query viewer { viewer { id username __typename } }")

    def test_default_backend_uses_matching_curl_impersonation(self):
        captured = []

        class EmptySession:
            def close(self):
                pass

        transport = PlayerokGraphQLHTTPTransport(
            PlayerokAuthContext(TOKEN),
            session_factory=lambda **kwargs: captured.append(kwargs) or EmptySession(),
        )
        self.assertEqual(captured, [{"impersonate": "chrome131"}])
        transport.close()

    def test_saved_cookie_context_is_keyring_only_and_sent_allowlisted(self):
        keyring = MemoryKeyring(None)
        PlayerokReadOnlyGraphQLClient.save_browser_context({
            "token": TOKEN, "__ddg5_": "synthetic-ddg-cookie", "ignored": "not-stored",
        }, _USER_AGENT, keyring_backend=keyring)
        raw = keyring.value
        self.assertNotIn("ignored", raw)
        context = PlayerokAuthContext.from_keyring(keyring)
        self.assertEqual(dict(context.cookies), {"token": TOKEN, "__ddg5_": "synthetic-ddg-cookie"})
        self.assertNotIn(TOKEN, repr(context))
        transport = PlayerokGraphQLHTTPTransport(context, session=FakeSession([]))
        self.assertEqual(
            transport._headers("viewer")["cookie"],
            "__ddg5_=synthetic-ddg-cookie; token=" + TOKEN,
        )

    def test_saved_context_is_not_reported_successful_without_keyring_readback(self):
        class DroppedWriteKeyring:
            def get_password(self, service, account):
                self.last_lookup = (service, account)
                return None

            def set_password(self, service, account, value):
                self.last_write = (service, account)

        keyring = DroppedWriteKeyring()
        with self.assertRaises(PlayerokGraphQLTransportError) as caught:
            PlayerokReadOnlyGraphQLClient.save_browser_context(
                {"token": TOKEN}, _USER_AGENT, keyring_backend=keyring
            )
        self.assertEqual(caught.exception.code, "keyring_readback_failed")
        self.assertEqual(keyring.last_write, (KEYRING_SERVICE, KEYRING_ACCOUNT))
        self.assertEqual(keyring.last_lookup, (KEYRING_SERVICE, KEYRING_ACCOUNT))

    def test_user_agent_selects_supported_not_newer_curl_fingerprint(self):
        context = PlayerokAuthContext(TOKEN, user_agent=_USER_AGENT.replace("Chrome/131", "Chrome/134"))
        self.assertEqual(context.impersonate, "chrome133")
        with self.assertRaises(ValueError):
            PlayerokAuthContext(TOKEN, user_agent="non-browser-agent")

    def test_keyring_context_and_legacy_token_are_supported_without_repr_leak(self):
        encoded = json.dumps({
            "schema": "gd-playerok-auth-context-v1", "token": TOKEN,
            "user_agent": _USER_AGENT,
            "cookies": {"token": TOKEN},
        })
        context = PlayerokAuthContext.from_keyring(MemoryKeyring(encoded))
        self.assertEqual(context.user_agent, _USER_AGENT)
        self.assertNotIn(TOKEN, repr(context))
        self.assertEqual(PlayerokAuthContext.from_keyring(MemoryKeyring(TOKEN)).token, TOKEN)

        keyring = MemoryKeyring(None)
        adapter = PlayerokAdapter(keyring_backend=keyring)
        self.assertTrue(adapter._save_token(TOKEN))
        restored = PlayerokAuthContext.from_keyring(keyring)
        self.assertEqual(restored.token, TOKEN)
        self.assertEqual(restored.user_agent, _USER_AGENT)

    def test_existing_adapter_can_create_reader_without_second_auth_store(self):
        keyring = MemoryKeyring(TOKEN)
        adapter = PlayerokAdapter(keyring_backend=keyring)
        session = FakeSession([FakeResponse({"data": {"viewer": {"id": "seller", "username": "shop"}}})])
        client = adapter.create_read_only_graphql_client(session=session)
        self.assertEqual(client.get_viewer()["user_id"], "seller")
        client.close()
        self.assertTrue(session.closed)

    def test_full_deal_pagination_and_separate_canonical_ids(self):
        client, _transport, session = self.make_client([
            FakeResponse(connection("deals", [deal_node("deal-1")], has_next=True, cursor="c1", total=2)),
            FakeResponse(connection("deals", [deal_node("deal-2", chat_id="chat-2", listing_id=None)], total=2)),
        ])
        result = client.fetch_deals("seller-1", page_size=1)
        self.assertEqual([item.deal_id for item in result], ["deal-1", "deal-2"])
        self.assertEqual((result[0].chat_id, result[0].listing_id, result[0].listing_ref), ("chat-1", "listing-1", "untrusted-slug"))
        self.assertEqual((result[1].deal_id, result[1].chat_id, result[1].listing_id), ("deal-2", "chat-2", None))
        first = session.calls[0][1]["params"]
        second = session.calls[1][1]["params"]
        self.assertEqual(first["operationName"], "deals")
        self.assertIsNone(json.loads(first["variables"])["pagination"]["after"])
        self.assertEqual(json.loads(second["variables"])["pagination"]["after"], "c1")
        self.assertEqual(json.loads(first["variables"])["filter"]["status"], ["PAID", "SENT", "CONFIRMED"])

    def test_repeated_cursor_and_incomplete_total_are_rejected(self):
        client, _transport, _session = self.make_client([
            FakeResponse(connection("deals", [deal_node("d1")], has_next=True, cursor="same")),
            FakeResponse(connection("deals", [deal_node("d2")], has_next=True, cursor="same")),
        ])
        with self.assertRaisesRegex(PlayerokGraphQLTransportError, "invalid_pagination"):
            client.fetch_deals("seller-1", page_size=1)

        client, _transport, _session = self.make_client([
            FakeResponse(connection("deals", [deal_node("d1")], total=2)),
        ])
        with self.assertRaisesRegex(PlayerokGraphQLTransportError, "incomplete_snapshot"):
            client.fetch_deals("seller-1")

    def test_empty_page_with_next_cursor_is_rejected(self):
        client, _transport, _session = self.make_client([
            FakeResponse(connection("deals", [], has_next=True, cursor="next")),
        ])
        with self.assertRaisesRegex(PlayerokGraphQLTransportError, "invalid_pagination"):
            client.fetch_deals("seller-1")

    def test_listing_id_never_falls_back_to_slug(self):
        client, _transport, session = self.make_client([
            FakeResponse(connection("items", [{"id": "item-1", "slug": "slug-1", "name": "Listing", "status": "APPROVED"}]))
        ])
        result = client.fetch_listings("seller-1")
        self.assertEqual(result[0].listing_id, "item-1")
        params = session.calls[0][1]["params"]
        self.assertEqual(params["operationName"], "items")

    def test_chat_and_message_operations_reuse_existing_normalizers(self):
        client, _transport, _session = self.make_client([
            FakeResponse({"data": {"chats": {"edges": [{"node": {
                "id": "chat-1", "type": "DEFAULT", "status": "ACTIVE",
                "unreadMessagesCounter": 7, "startedAt": None, "finishedAt": None,
                "participants": [], "deals": [], "lastMessage": None,
            }}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}),
            FakeResponse({"data": {"chatMessages": {"edges": [{"node": {
                "id": "message-1", "text": "synthetic message", "createdAt": "2026-11-01T00:00:00Z",
                "user": {"id": "buyer-1", "username": "buyer"}, "deal": {"id": "deal-1"},
                "file": None, "images": [],
            }}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}),
        ])
        chats = client.fetch_chats("seller-1")
        messages = client.fetch_chat_messages("chat-1")
        self.assertEqual(chats[0].id, "chat-1")
        self.assertEqual(chats[0].unread_messages_counter, 7)
        self.assertEqual(messages[0].id, "message-1")
        self.assertEqual(messages[0].deal_id, "deal-1")
        self.assertEqual(messages[0].chat_id, "chat-1")

    def test_partial_page_with_next_page_is_rejected(self):
        client, _transport, _session = self.make_client([
            FakeResponse(connection("deals", [deal_node("d1")], has_next=True, cursor="next")),
        ])
        with self.assertRaisesRegex(PlayerokGraphQLTransportError, "partial_page"):
            client.fetch_deals("seller-1", page_size=2)

    def test_auth_http_graphql_and_cloudflare_errors_are_safe(self):
        for response, code in (
            (FakeResponse({}, status=401), "session_expired"),
            (FakeResponse({}, status=403), "session_expired"),
            (FakeResponse({"errors": [{"message": TOKEN, "extensions": {"code": "UNAUTHENTICATED"}}]}), "session_expired"),
            (FakeResponse({}, status=503, text="<html>private response</html>", content_type="text/html"), "cloudflare_challenge"),
        ):
            client, _transport, _session = self.make_client([response], retries=0)
            with self.assertRaises(PlayerokGraphQLTransportError) as raised:
                client.get_viewer()
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertNotIn("private response", str(raised.exception))

    def test_transient_read_retries_are_bounded(self):
        client, _transport, session = self.make_client([
            RuntimeError(TOKEN), FakeResponse({"data": {"viewer": {"id": "seller", "username": "s"}}}),
        ], retries=1)
        self.assertEqual(client.get_viewer()["user_id"], "seller")
        self.assertEqual(len(session.calls), 2)

    def test_mutations_and_unknown_operations_are_not_exposed(self):
        client, _transport, session = self.make_client([])
        with self.assertRaises(ValueError):
            client.submit("send_text_message", "chat", "text")
        with self.assertRaises(PlayerokGraphQLTransportError):
            client.transport.execute("createChatMessage", {})
        self.assertEqual(session.calls, [])

    def test_session_context_errors_do_not_expose_keyring_data(self):
        class BrokenKeyring:
            def get_password(self, *_args):
                raise RuntimeError(TOKEN)
        with self.assertRaises(PlayerokGraphQLTransportError) as raised:
            PlayerokAuthContext.from_keyring(BrokenKeyring())
        self.assertNotIn(TOKEN, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
