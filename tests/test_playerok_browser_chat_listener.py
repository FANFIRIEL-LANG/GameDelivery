from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from integrations.playerok.browser_chat_api import PlayerokDealReference, normalize_chat, normalize_message
from integrations.playerok.browser_chat_listener import PlayerokBrowserChatListener


def message(message_id, *, deal_id=None, text="hello", created_at=None):
    return {
        "id": message_id, "text": text,
        "createdAt": created_at or datetime.now(timezone.utc).isoformat(),
        "user": {"id": "buyer-1", "username": "buyer"},
        "deal": {"id": deal_id} if deal_id else None,
    }


def chat(chat_id="chat-1", last=None):
    return {"id": chat_id, "type": "ORDER", "status": "ACTIVE", "participants": [],
            "deals": [], "lastMessage": last}


class FakeChatAPI:
    def __init__(self, initial_chats=(), catchup_messages=()):
        self.initial_chats = tuple(normalize_chat(item) for item in initial_chats)
        self.catchup_chats = self.initial_chats
        self.catchup_messages = tuple(normalize_message(item, chat_id="chat-1") for item in catchup_messages)
        self.chat_list_calls = 0
        self.message_calls = []
        self.deal_calls = []
        self.transport = object()

    def get_viewer(self, callback):
        callback("account-1", None)

    def get_chats(self, user_id, callback):
        self.chat_list_calls += 1
        callback(self.initial_chats if self.chat_list_calls == 1 else self.catchup_chats, None)

    def get_chat_messages(self, chat_id, callback):
        self.message_calls.append(chat_id)
        callback(self.catchup_messages, None)

    def get_deal(self, deal_id, callback):
        self.deal_calls.append(deal_id)
        callback(PlayerokDealReference(deal_id, "PAID", "chat-1", "listing-distinct"), None)


class BrowserChatListenerTests(unittest.TestCase):
    def setUp(self):
        self.scripts = []
        self.listener = None

    def tearDown(self):
        if self.listener is not None:
            self.listener.stop()

    def _run(self, script, callback):
        self.scripts.append(script)
        if "wss://ws.playerok.com/graphql" in script:
            callback(True)
        elif "bridge_missing" in script:
            callback(json.dumps({"error": "bridge_missing"}))
        else:
            callback(True)

    def _listener(self, api):
        self.listener = PlayerokBrowserChatListener(api, self._run, poll_interval_ms=100_000)
        return self.listener

    def test_start_uses_current_page_websocket_bridge_and_no_cookie_export(self):
        api = FakeChatAPI([chat(last=message("baseline-1"))])
        listener = self._listener(api)
        states = []
        listener.connection_state.connect(states.append)
        listener.start()
        self.assertTrue(listener.active)
        self.assertEqual(api.chat_list_calls, 1)
        script = next(script for script in self.scripts if "wss://ws.playerok.com/graphql" in script)
        self.assertIn("graphql-transport-ws", script)
        self.assertIn("connection_init", script)
        self.assertIn("chatUpdated", script)
        self.assertIn("chatMessageCreated", script)
        self.assertIn("userUpdated", script)
        for forbidden in ("document.cookie", "cookieStore", "Authorization", "localStorage", "sessionStorage"):
            self.assertNotIn(forbidden, script)
        self.assertNotIn("credentials", script)
        self.assertIn("connecting", states)

    def test_read_only_diagnostic_starts_current_chat_websocket_without_catchup(self):
        api = FakeChatAPI([chat()])
        listener = self._listener(api)
        states = []
        listener.connection_state.connect(states.append)
        listener.start_for_read_only_diagnostic("account-1", "chat-current")
        script = next(script for script in self.scripts if "wss://ws.playerok.com/graphql" in script)
        self.assertIn("chat-current", script)
        self.assertIn("chatUpdated", script)
        self.assertIn("chatMessageCreated", script)
        self.assertNotIn("fetch(", script)
        self.assertIn("diagnostic_starting", states)
        self.assertIn("connecting", states)
        listener._process_bridge_event({"kind": "connected", "reconnected": False})
        self.assertEqual(api.chat_list_calls, 0)
        self.assertEqual(api.message_calls, [])
        self.assertIn("connected", states)

    def test_chat_list_failure_keeps_only_safe_error_category_for_diagnosis(self):
        api = FakeChatAPI()
        listener = self._listener(api)
        states = []
        listener.connection_state.connect(states.append)
        listener.start()
        listener._initial_chats(None, "http_400")
        self.assertIn("chat_list_error_http_400", states)
        self.assertIn("stopped", states)
        self.assertEqual(listener.account_user_id, "account-1")
        self.assertFalse(listener.active)

    def test_chat_list_failure_redacts_unrecognized_error_text(self):
        api = FakeChatAPI()
        listener = self._listener(api)
        states = []
        listener.connection_state.connect(states.append)
        listener.start()
        listener._initial_chats(None, "raw server details with private data")
        self.assertIn("chat_list_error_api_error", states)
        self.assertNotIn("raw server details with private data", states)

    def test_duplicate_websocket_message_is_emitted_once_and_system_event_keeps_exact_ids(self):
        api = FakeChatAPI([chat()])
        listener = self._listener(api)
        received, events = [], []
        listener.message_received.connect(received.append)
        listener.system_event.connect(events.append)
        listener.start()
        frame = {"kind": "message", "chatId": "chat-1", "message": message(
            "paid-message", deal_id="deal-exact", text="{{ITEM_PAID}}")}
        listener._process_bridge_event(frame)
        listener._process_bridge_event(frame)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].chat_id, "chat-1")
        self.assertEqual(received[0].deal_id, "deal-exact")
        self.assertEqual(events, [{"kind": "item_paid", "chat_id": "chat-1",
                                   "deal_id": "deal-exact", "listing_id": "listing-distinct",
                                   "deal_status": "PAID", "message_id": "paid-message"}])
        self.assertEqual(api.deal_calls, ["deal-exact"])

    def test_new_chat_and_last_message_are_emitted_with_structured_ids(self):
        api = FakeChatAPI([chat()])
        listener = self._listener(api)
        discovered, received = [], []
        listener.chat_discovered.connect(discovered.append)
        listener.message_received.connect(received.append)
        listener.start()
        listener._process_bridge_event({"kind": "chat_updated", "chat": chat(
            "chat-new", message("new-message", deal_id="deal-new"))})
        self.assertEqual([item.id for item in discovered], ["chat-new"])
        self.assertEqual(received[0].chat_id, "chat-new")
        self.assertEqual(received[0].deal_id, "deal-new")

    def test_subscription_event_signal_reports_only_validated_event_kind(self):
        listener = self._listener(FakeChatAPI([chat()]))
        observed = []
        listener.subscription_event.connect(observed.append)
        listener.start()
        listener._process_bridge_event({"kind": "chat_updated", "chat": chat("chat-1")})
        listener._process_bridge_event({"kind": "message", "chatId": "chat-1",
                                        "message": message("message-1")})
        listener._process_bridge_event({"kind": "message", "chatId": "chat-1", "message": {"bad": True}})
        self.assertEqual(observed, ["chatUpdated", "chatMessageCreated"])

    def test_reconnect_catches_up_messages_and_clears_old_subscription_generation(self):
        last = message("last-known", created_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        missed = message("missed-during-gap", deal_id="deal-missed")
        api = FakeChatAPI([chat(last=last)], [missed])
        listener = self._listener(api)
        received, states = [], []
        listener.message_received.connect(received.append)
        listener.connection_state.connect(states.append)
        listener.start()
        listener._process_bridge_event({"kind": "disconnected"})
        listener._process_bridge_event({"kind": "connected", "reconnected": True})
        self.assertIn("chat-1", api.message_calls)
        self.assertIn("missed-during-gap", [item.id for item in received])
        self.assertNotIn("last-known", [item.id for item in received])
        script = next(script for script in self.scripts if "wss://ws.playerok.com/graphql" in script)
        self.assertIn("subscriptions.clear()", script)
        self.assertIn("myGeneration!==generation", script)
        self.assertIn("caught_up", states)

    def test_user_updated_refreshes_chat_ids_without_coupling_deal_id_to_chat_id(self):
        api = FakeChatAPI([chat("chat-a")])
        api.catchup_chats = (normalize_chat(chat("chat-a")), normalize_chat(chat("chat-b")))
        listener = self._listener(api)
        discovered = []
        listener.chat_discovered.connect(discovered.append)
        listener.start()
        listener._process_bridge_event({"kind": "user_updated", "user": {"id": "account-1"}})
        self.assertEqual([item.id for item in discovered], ["chat-b"])
        add_script = next(script for script in self.scripts if "addChats" in script and "chat-b" in script)
        self.assertIn("chat-b", add_script)

    def test_system_status_events_do_not_trigger_mutations(self):
        api = FakeChatAPI([chat()])
        listener = self._listener(api)
        events = []
        listener.system_event.connect(events.append)
        listener.start()
        for token, expected in (
            ("{{ITEM_SENT}}", "item_sent"),
            ("{{DEAL_CONFIRMED_AUTOMATICALLY}}", "deal_confirmed"),
            ("{{DEAL_ROLLED_BACK}}", "deal_rolled_back"),
            ("{{DEAL_HAS_PROBLEM}}", "deal_has_problem"),
            ("{{DEAL_PROBLEM_RESOLVED}}", "deal_problem_resolved"),
        ):
            listener._process_bridge_event({"kind": "message", "chatId": "chat-1",
                                            "message": message("msg-" + expected, deal_id="deal-1", text=token)})
        self.assertEqual([event["kind"] for event in events], [
            "item_sent", "deal_confirmed", "deal_rolled_back", "deal_has_problem", "deal_problem_resolved",
        ])
        self.assertEqual(api.message_calls, [])
        self.assertEqual(api.deal_calls, ["deal-1"] * 5)


if __name__ == "__main__":
    unittest.main()
