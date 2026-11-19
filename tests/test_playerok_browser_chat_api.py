from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from integrations.playerok.browser_chat_api import (
    MessageDeduplicator, PlayerokBrowserChatAPI,
    WebEngineGraphQLTransport, _get_url, normalize_chat, normalize_message,
)
from integrations.playerok.universal_chat_contract import PERSISTED_QUERIES, QUERIES


class FakeTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def request(self, operation, variables, *, method, callback):
        self.requests.append((operation, dict(variables), method))
        if self.responses:
            payload, error = self.responses.pop(0)
            callback(payload, error)
        else:
            callback(None, "no_fake_response")


def raw_chat(chat_id="chat-1", *, last=None, deals=None):
    return {
        "id": chat_id, "type": "ORDER", "status": "ACTIVE", "unreadMessagesCounter": 2,
        "startedAt": "2026-11-18T10:00:00Z", "finishedAt": None,
        "participants": [{"id": "buyer-1", "username": "buyer"}],
        "lastMessage": last,
        "deals": deals or [],
    }


def raw_message(message_id, *, chat_id=None, text="hello", deal_id=None):
    return {
        "id": message_id, "text": text, "createdAt": "2026-11-18T10:01:00Z",
        "user": {"id": "buyer-1", "username": "buyer"},
        "deal": {"id": deal_id} if deal_id else None,
        "file": {"id": "file-1", "url": "https://cdn.example/file", "filename": "x.png", "mime": "image/png"},
        "images": [{"id": "image-1", "url": "https://cdn.example/image", "filename": "y.png", "mime": "image/png"}],
    }


class BrowserChatAPITests(unittest.TestCase):
    def test_persisted_query_url_uses_universal_hash_and_exact_chat_identity(self):
        url = _get_url("chatMessages", {
            "pagination": {"first": 24, "after": None},
            "filter": {"chatId": "chat-canonical"},
            "hasSupportAccess": False,
            "showForbiddenImage": True,
        })
        parsed = parse_qs(urlsplit(url).query)
        self.assertEqual(parsed["operationName"], ["chatMessages"])
        self.assertEqual(json.loads(parsed["extensions"][0])["persistedQuery"]["sha256Hash"],
                         PERSISTED_QUERIES["chatMessages"])
        variables = json.loads(parsed["variables"][0])
        self.assertEqual(variables["filter"]["chatId"], "chat-canonical")
        self.assertNotIn("deal_id", variables)

    def test_get_chats_paginates_and_deduplicates_by_chat_id(self):
        first = {"data": {"chats": {
            "edges": [{"node": raw_chat("chat-a")}, {"node": raw_chat("chat-b")}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        }}}
        second = {"data": {"chats": {
            "edges": [{"node": raw_chat("chat-b")}, {"node": raw_chat("chat-c")}],
            "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
        }}}
        transport = FakeTransport([(first, None), (second, None)])
        outcomes = []
        observed_pages = []
        PlayerokBrowserChatAPI(transport).get_chats("account-1", lambda value, error: outcomes.append((value, error)))
        self.assertIsNone(outcomes[0][1])
        self.assertEqual([chat.id for chat in outcomes[0][0]], ["chat-a", "chat-b", "chat-c"])
        self.assertEqual(transport.requests[0][1]["filter"]["userId"], "account-1")
        self.assertEqual(transport.requests[1][1]["pagination"]["after"], "cursor-1")

        transport = FakeTransport([(first, None), (second, None)])
        PlayerokBrowserChatAPI(transport).get_chats(
            "account-1", lambda value, error: None,
            page_observer=lambda has_cursor, error: observed_pages.append((has_cursor, error)),
        )
        self.assertEqual(observed_pages, [(False, None), (True, None)])

    def test_get_chat_and_get_deal_use_their_canonical_ids(self):
        transport = FakeTransport([
            ({"data": {"chat": raw_chat("chat-canonical")}}, None),
            ({"data": {"deal": {"id": "deal-canonical", "status": "SENT",
                "chat": {"id": "chat-canonical"}, "item": {"id": "listing-canonical"}}}}, None),
        ])
        api = PlayerokBrowserChatAPI(transport)
        chat_result, deal_result = [], []
        api.get_chat("chat-canonical", lambda value, error: chat_result.append((value, error)))
        api.get_deal("deal-canonical", lambda value, error: deal_result.append((value, error)))
        self.assertEqual(chat_result[0][0].id, "chat-canonical")
        self.assertIsNone(chat_result[0][1])
        self.assertEqual((deal_result[0][0].id, deal_result[0][0].chat_id,
                          deal_result[0][0].listing_id, deal_result[0][0].status),
                         ("deal-canonical", "chat-canonical", "listing-canonical", "SENT"))
        self.assertEqual(transport.requests[1][1]["id"], "deal-canonical")
        self.assertEqual(transport.requests[1][1]["showForbiddenImage"], True)

    def test_messages_paginate_and_preserve_user_deal_and_attachment_data(self):
        first = {"data": {"chatMessages": {
            "edges": [{"node": raw_message("message-1", deal_id="deal-1")}],
            "pageInfo": {"hasNextPage": True, "endCursor": "m-cursor-1"},
        }}}
        second = {"data": {"chatMessages": {
            "edges": [{"node": raw_message("message-1", deal_id="deal-1")},
                       {"node": raw_message("message-2", deal_id="deal-2")}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}
        transport = FakeTransport([(first, None), (second, None)])
        outcomes = []
        observed_pages = []
        PlayerokBrowserChatAPI(transport).get_chat_messages(
            "chat-1", lambda value, error: outcomes.append((value, error)),
            page_observer=lambda has_cursor, error: observed_pages.append((has_cursor, error)),
        )
        messages, error = outcomes[0]
        self.assertIsNone(error)
        self.assertEqual([message.id for message in messages], ["message-1", "message-2"])
        self.assertEqual(messages[0].chat_id, "chat-1")
        self.assertEqual(messages[0].user.id, "buyer-1")
        self.assertEqual(messages[0].user.username, "buyer")
        self.assertEqual(messages[0].deal_id, "deal-1")
        self.assertEqual(messages[0].text, "hello")
        self.assertEqual(messages[0].file.id, "file-1")
        self.assertEqual(messages[0].images[0].id, "image-1")
        self.assertEqual(transport.requests[1][1]["pagination"]["after"], "m-cursor-1")
        self.assertEqual(observed_pages, [(False, None), (True, None)])

    def test_repeated_cursor_fails_closed_for_message_and_chat_pages(self):
        response = {"data": {"chatMessages": {
            "edges": [], "pageInfo": {"hasNextPage": True, "endCursor": "same"},
        }}}
        transport = FakeTransport([(response, None), (response, None)])
        outcomes = []
        PlayerokBrowserChatAPI(transport).get_chat_messages("chat-1", lambda value, error: outcomes.append((value, error)))
        self.assertEqual(outcomes, [(None, "repeated_cursor")])
        self.assertEqual(len(transport.requests), 2)

        chats = {"data": {"chats": {"edges": [], "pageInfo": {"hasNextPage": True, "endCursor": "c"}}}}
        transport = FakeTransport([(chats, None), (chats, None)])
        outcomes = []
        PlayerokBrowserChatAPI(transport).get_chats("account-1", lambda value, error: outcomes.append((value, error)))
        self.assertEqual(outcomes, [(None, "repeated_cursor")])

    def test_chat_and_deal_ids_remain_independent_when_one_chat_has_multiple_deals(self):
        chat = normalize_chat(raw_chat("chat-one", deals=[{"id": "deal-a"}, {"id": "deal-b"}]))
        self.assertEqual(chat.id, "chat-one")
        self.assertEqual(chat.deal_ids, ("deal-a", "deal-b"))
        message = normalize_message(raw_message("message-1", deal_id="deal-b"), chat_id="chat-one")
        self.assertEqual((message.chat_id, message.deal_id), ("chat-one", "deal-b"))

    def test_send_text_uses_real_mutation_and_does_not_repeat_unknown_result(self):
        response = {"data": {"createChatMessage": raw_message("sent-1", chat_id="chat-1")}}
        transport = FakeTransport([(response, None)])
        outcomes = []
        PlayerokBrowserChatAPI(transport).send_text_message(
            "chat-1", "hello buyer", lambda value, error: outcomes.append((value, error))
        )
        self.assertEqual(len(transport.requests), 1)
        operation, variables, method = transport.requests[0]
        self.assertEqual((operation, method), ("createChatMessage", "POST"))
        self.assertEqual(variables["input"], {"chatId": "chat-1", "imagesIds": [], "text": "hello buyer"})
        self.assertIn("mutation createChatMessage", QUERIES["createChatMessage"])
        self.assertEqual(outcomes[0][0].id, "sent-1")

        unknown_transport = FakeTransport([(None, "unknown_result")])
        unknown = []
        PlayerokBrowserChatAPI(unknown_transport).send_text_message(
            "chat-1", "hello buyer", lambda value, error: unknown.append((value, error))
        )
        self.assertEqual(unknown, [(None, "unknown_result")])
        self.assertEqual(len(unknown_transport.requests), 1)

    def test_invalid_ids_and_empty_message_are_rejected_before_transport(self):
        transport = FakeTransport()
        api = PlayerokBrowserChatAPI(transport)
        outcomes = []
        api.get_chat_messages(123, lambda value, error: outcomes.append(error))
        api.send_text_message("chat-1", "  ", lambda value, error: outcomes.append(error))
        self.assertEqual(outcomes, ["invalid_identifier", "invalid_message_text"])
        self.assertEqual(transport.requests, [])

    def test_webengine_transport_stays_in_page_and_never_exports_cookie_values(self):
        scripts = []
        responses = [
            "__gd_playerok_chat_test",
            json.dumps({"state": "done", "status": 200, "payload": {"data": {"viewer": {"id": "account-1"}}}}),
        ]

        def run_javascript(script, callback):
            scripts.append(script)
            callback(responses.pop(0))

        outcomes = []
        transport = WebEngineGraphQLTransport(run_javascript, schedule=lambda callback: callback())
        # Make the generated request ID deterministic without exposing credentials.
        with patch("integrations.playerok.browser_chat_api.uuid4") as uuid_factory:
            uuid_factory.return_value.hex = "test"
            responses[0] = "__gd_playerok_chat_test"
            transport.request("viewer", {}, method="GET", callback=lambda value, error: outcomes.append((value, error)))
        self.assertEqual(outcomes[0][0]["data"]["viewer"]["id"], "account-1")
        all_scripts = "\n".join(scripts)
        self.assertIn('"credentials":"include"', all_scripts)
        for forbidden in ("document.cookie", "cookieStore", "localStorage", "sessionStorage", "Authorization:"):
            self.assertNotIn(forbidden, all_scripts)

    def test_graphql_transport_matches_pinned_universal_request_context(self):
        scripts = []
        responses = [
            "__gd_playerok_chat_test",
            json.dumps({"state": "done", "status": 200, "payload": {"data": {"chats": {}}}}),
        ]

        def run_javascript(script, callback):
            scripts.append(script)
            callback(responses.pop(0))

        transport = WebEngineGraphQLTransport(run_javascript, schedule=lambda callback: callback())
        with patch("integrations.playerok.browser_chat_api.uuid4") as uuid_factory:
            uuid_factory.return_value.hex = "test"
            transport.request("userChats", {}, method="GET", callback=lambda _value, _error: None)

        request_script = scripts[0]
        self.assertIn('"x-gql-op":"userChats"', request_script)
        self.assertIn('"x-gql-path":"/"', request_script)
        self.assertIn('"x-timezone-offset":"-240"', request_script)
        self.assertIn('"x-apollo-operation-name":"userChats"', request_script)
        self.assertIn('"apollo-require-preflight":"true"', request_script)
        self.assertIn('"apollographql-client-name":"web"', request_script)
        for forbidden in ("document.cookie", "cookieStore", "localStorage", "sessionStorage", "Authorization:"):
            self.assertNotIn(forbidden, request_script)

    def test_webengine_transport_surfaces_only_allowlisted_graphql_error_code(self):
        responses = [
            "__gd_playerok_chat_test",
            json.dumps({"state": "done", "status": 400, "payload": {
                "errors": [{"message": "private server detail", "extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"}}],
            }}),
        ]
        outcomes = []
        transport = WebEngineGraphQLTransport(
            lambda _script, callback: callback(responses.pop(0)), schedule=lambda callback: callback()
        )
        with patch("integrations.playerok.browser_chat_api.uuid4") as uuid_factory:
            uuid_factory.return_value.hex = "test"
            transport.request("userChats", {}, method="GET", callback=lambda value, error: outcomes.append(error))
        self.assertEqual(outcomes, ["persisted_query_not_found"])
        self.assertNotIn("private server detail", str(outcomes))

    def test_message_id_deduplicator_is_bounded(self):
        dedupe = MessageDeduplicator(2)
        self.assertTrue(dedupe.claim("m1"))
        self.assertTrue(dedupe.claim("m2"))
        self.assertFalse(dedupe.claim("m1"))
        self.assertTrue(dedupe.claim("m3"))
        self.assertEqual(len(dedupe._ids), 2)


if __name__ == "__main__":
    unittest.main()
