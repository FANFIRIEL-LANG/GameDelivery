from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtWidgets import QApplication

import app.playerok_browser as playerok_browser
from app.playerok_browser import (
    PlayerokBrowserPanel,
    _DOM_DIAGNOSTIC_CALLBACK_MAX_BYTES,
    _DOM_DIAGNOSTIC_MAX_BYTES,
    _fit_dom_summary,
    _sanitize_dom_summary,
)
from core.database.db import Database


def _node(**overrides):
    value = {
        "tag": "div",
        "id": "chat-scrollable-container",
        "class": "chat-list wrapper",
        "role": "log",
        "aria": {"aria-live": "polite", "aria-label": "Messages"},
        "dataAttributes": ["data-state", "data-message-id"],
        "childElementCount": 4,
    }
    value.update(overrides)
    return value


def _struct_node(**overrides):
    value = {
        "tag": "article",
        "class": "message-bubble",
        "role": None,
        "aria": {"aria-describedby": "private-message-id"},
        "dataAttributes": ["data-message-id"],
        "childElementCount": 2,
        "stableAttributePresence": {"id": True, "data-message-id": True},
        "id": "private-message-id",
        "text": "private message text",
    }
    value.update(overrides)
    return value


def _struct_tree(*, class_name="message-bubble", text="private message text"):
    return {
        "node": _struct_node(**{"class": class_name, "text": text}),
        "children": [
            {"node": _struct_node(tag="span", **{"class": "avatar", "dataAttributes": [],
                "childElementCount": 0, "stableAttributePresence": {}, "text": "private user name"}),
             "children": [], "childrenTruncated": False},
            {"node": _struct_node(tag="div", **{"class": "message-content", "childElementCount": 1}),
             "children": [
                 {"node": _struct_node(tag="time", **{"class": "timestamp", "childElementCount": 0,
                    "stableAttributePresence": {"datetime": True}, "datetime": "private timestamp"}),
                  "children": [], "childrenTruncated": False},
             ], "childrenTruncated": False},
        ],
        "childrenTruncated": False,
    }


def _summary_payload():
    return json.dumps({
        "page": {"origin": "https://playerok.com", "pathname": "/chats/chat-id?token=private#fragment"},
        "body": {"elementCount": 8, "maxDepth": 5, "scanTruncated": False},
        "counts": {"containers": 1, "messages": 1, "interactive": 2, "textareas": 1,
            "dealLinks": 1, "repeatedSiblingGroups": 0, "dynamicAreas": 1,
            "links": 2, "scannedElements": 8},
        "candidateContainers": [_node()],
        "candidateMessages": [_node(id="message-1")],
        "interactiveElements": [
            _node(tag="textarea", id="composer", **{"class": "composer", "type": None,
                "value": "unsent draft", "text": "private message"}),
            _node(tag="button", id="send", **{"aria": {"aria-label": "Send"}}),
        ],
        "linkCount": 2,
        "links": [
            {"hostname": "steamcommunity.com", "pathname": "/tradeoffer/new/"},
            {"hostname": "playerok.com", "pathname": "/deal/example"},
        ],
        "linksTruncated": False,
        "dynamicAreas": [_node(kind="dynamic")],
        "textareaContexts": [{
            "textarea": _node(tag="textarea", id="composer"),
            "ancestors": [{
                "depth": 1,
                "node": _node(id="composer-row"),
                "childIndex": 1,
                "childElementCount": 3,
                "siblings": [
                    {"index": 0, "node": _node(tag="input", id="attachment", type="file",
                        **{"value": "must-not-escape"})},
                    {"index": 2, "node": _node(tag="button", id="send-candidate",
                        **{"aria": {"aria-label": "Send"}, "text": "secret text"})},
                ],
            }],
        }],
        "composerScopes": [{
            "textareaIndex": 0,
            "scopeDepth": 1,
            "scope": _node(id="composer-row"),
            "controls": [
                {"node": _node(tag="input", id="attachment", type="file"), "signals": ["file-input"]},
                {"node": _node(tag="button", id="send-candidate", type="submit"),
                 "signals": ["explicit-submit-type"]},
                {"node": _node(tag="button", id="unclassified"), "signals": ["secret-classification"]},
            ],
        }],
        "dealLinkContexts": [{
            "hostname": "playerok.com",
            "pathname": "/deal/1f1e3492-1d63-6440-2410-b0fe4e19cfc4",
            "link": _node(tag="a", id="deal-link"),
            "ancestors": [{
                "depth": 1,
                "node": _node(id="deal-card"),
                "childIndex": 0,
                "childElementCount": 2,
                "siblings": [{"index": 1, "node": _node(id="other-deal-link")}],
            }],
            "href": "https://playerok.com/deal/id?secret=query",
            "text": "secret message text",
        }],
        "candidateContainersTruncated": False,
        "candidateMessagesTruncated": False,
        "interactiveElementsTruncated": False,
        "dynamicAreasTruncated": False,
        "messageText": "private message text",
        "tradeUrl": "https://steamcommunity.com/tradeoffer/new/?token=secret",
    }, ensure_ascii=False)


class _SignalStub:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback

    def disconnect(self, callback):
        if self.callback == callback:
            self.callback = None

    def emit(self, *args):
        callback = self.callback
        if callback is not None:
            callback(*args)


class _PageStub:
    def __init__(self, url="https://playerok.com/chats/chat-id"):
        self.destroyed = _SignalStub()
        self.valid = True
        self.current_url = url
        self.scripts = []
        self.callback = None
        self.url_calls = 0

    def url(self):
        self.url_calls += 1
        return QUrl(self.current_url)

    def runJavaScript(self, script, callback):
        self.scripts.append(script)
        self.callback = callback


class _WebStub:
    def __init__(self, page):
        self.current_page = page

    def page(self):
        return self.current_page


class PlayerokDomDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "dom-diagnostic.sqlite3")
        self.panel = PlayerokBrowserPanel(database=self.db, enable_webengine=False)

    def tearDown(self):
        self.panel.close()
        self.tmp.cleanup()

    def _attach_page(self, page):
        self.panel.web = _WebStub(page)
        self.panel._page_ready = True

    def _patch_validity(self):
        return patch.object(
            playerok_browser.shiboken6,
            "isValid",
            side_effect=lambda item: getattr(item, "valid", True),
        )

    def test_result_is_allowlisted_and_contains_no_messages_trade_url_or_input_values(self):
        result = _sanitize_dom_summary(_summary_payload())
        self.assertIsNotNone(result)
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in ("private message", "unsent draft", "tradeUrl", "token=secret", "messageText"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(result["page"], {"site": "playerok"})
        self.assertEqual(result["candidate_containers"][0]["data_attribute_names"], [
            "data-state", "data-message-id",
        ])
        self.assertNotIn("unsent private draft", rendered)
        self.assertEqual(result["counts"]["links"], 2)
        self.assertNotIn("links", result)
        self.assertNotIn("deal_link_contexts", result)
        self.assertNotIn("1f1e3492-1d63-6440-2410-b0fe4e19cfc4", rendered)
        self.assertNotIn("playerok.com", rendered)
        self.assertNotIn("steamcommunity.com", rendered)
        self.assertNotIn("/tradeoffer/new/", rendered)
        self.assertEqual(result["textarea_contexts"][0]["ancestors"][0]["child_index"], 1)
        self.assertEqual(result["composer_scopes"][0]["controls"][0]["signals"], ["file-input"])
        self.assertEqual(result["composer_scopes"][0]["controls"][1]["signals"], ["explicit-submit-type"])
        self.assertEqual(result["composer_scopes"][0]["controls"][2]["signals"], [])
        self.assertNotIn("deal_link_contexts", result)
        for secret in ("must-not-escape", "secret text", "secret-classification", "secret=query"):
            self.assertNotIn(secret, rendered)

    def test_dom_context_depth_and_collection_counts_are_bounded(self):
        payload = json.loads(_summary_payload())
        context = payload["textareaContexts"][0]["ancestors"][0]
        payload["textareaContexts"] *= 8
        payload["textareaContexts"][0]["ancestors"] = [context] * 20
        payload["composerScopes"] *= 8
        result = _sanitize_dom_summary(json.dumps(payload))
        self.assertEqual(len(result["textarea_contexts"]), 2)
        self.assertEqual(len(result["textarea_contexts"][0]["ancestors"]), 4)
        self.assertEqual(len(result["composer_scopes"]), 2)
        self.assertNotIn("deal_link_contexts", result)

    def test_synthetic_repeated_message_shapes_remain_unconfirmed_structural_candidates(self):
        payload = json.loads(_summary_payload())
        tree_a = _struct_tree()
        tree_b = _struct_tree(text="another private message")
        outside_a = _struct_tree(class_name="suggestion-card", text="private product title")
        outside_b = _struct_tree(class_name="suggestion-card", text="another private product title")
        nav_tree_a = _struct_tree(class_name="nav-link")
        nav_tree_b = _struct_tree(class_name="nav-link")
        payload["repeatedSiblingGroups"] = [
            {"count": 3, "parent": _struct_node(tag="div", **{"class": "thread-list"}),
             "ancestors": [{"depth": 1, "node": _struct_node(tag="main", **{"class": "chat-page"}),
                 "childIndex": 1, "childElementCount": 3, "siblings": []}],
             "examples": [tree_a, tree_b]},
            {"count": 2, "parent": _struct_node(tag="div", **{"class": "suggestions"}),
             "ancestors": [], "examples": [outside_a, outside_b]},
            {"count": 2, "parent": _struct_node(tag="nav", **{"class": "navigation"}),
             "ancestors": [], "examples": [nav_tree_a, nav_tree_b], "excluded": True},
            {"count": 2, "parent": _struct_node(tag="div"), "ancestors": [],
             "examples": [tree_a, _struct_tree(class_name="different-shape")]},
        ]
        result = _sanitize_dom_summary(json.dumps(payload))
        groups = result["repeated_sibling_candidates"]
        self.assertEqual(len(groups), 2)
        self.assertEqual([group["classification"] for group in groups], [
            "structural-candidate", "structural-candidate",
        ])
        self.assertEqual(groups[0]["examples"][0]["children"][1]["children"][0]["node"]["tag"], "time")
        self.assertNotIn("id", groups[0]["examples"][0]["node"])
        self.assertTrue(groups[0]["examples"][0]["node"]["stable_attribute_presence"]["data-message-id"])
        self.assertEqual(groups[0]["examples"][0]["node"]["aria"]["aria-describedby"], "[present]")
        rendered = json.dumps(groups, ensure_ascii=False)
        for private_value in ("private-message-id", "private message text", "private user name",
                              "private timestamp", "private product title"):
            self.assertNotIn(private_value, rendered)
        self.assertNotIn("message", groups[0]["classification"])

    def test_empty_or_nonrepeating_synthetic_dom_has_no_message_candidates(self):
        payload = json.loads(_summary_payload())
        payload["repeatedSiblingGroups"] = [
            {"count": 1, "parent": _struct_node(), "examples": [_struct_tree()], "ancestors": []},
            {"count": 2, "parent": _struct_node(), "examples": [_struct_tree(), _struct_tree(class_name="other")],
             "ancestors": []},
        ]
        result = _sanitize_dom_summary(json.dumps(payload))
        self.assertEqual(result["repeated_sibling_candidates"], [])

    def test_no_candidates_returns_valid_report_with_empty_sections(self):
        payload = json.loads(_summary_payload())
        payload["candidateContainers"] = []
        payload["candidateMessages"] = []
        payload["interactiveElements"] = []
        payload["textareaContexts"] = []
        payload["composerScopes"] = []
        payload["repeatedSiblingGroups"] = []
        payload["dynamicAreas"] = []
        payload["counts"] = {key: 0 for key in payload["counts"]}
        payload["linkCount"] = 0
        payload["body"]["elementCount"] = 0
        result = _sanitize_dom_summary(json.dumps(payload))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidate_messages"], [])
        self.assertEqual(result["repeated_sibling_candidates"], [])
        self.assertEqual(json.loads(json.dumps(result))["page"], {"site": "playerok"})

    def test_repeated_candidate_count_is_capped(self):
        payload = json.loads(_summary_payload())
        group = {"count": 2, "parent": _struct_node(tag="div"), "ancestors": [],
                 "examples": [_struct_tree(), _struct_tree()]}
        payload["repeatedSiblingGroups"] = [group] * 16
        result = _sanitize_dom_summary(json.dumps(payload))
        self.assertEqual(len(result["repeated_sibling_candidates"]), 10)
        payload["repeatedSiblingGroups"] = []
        result = _sanitize_dom_summary(json.dumps(payload))
        self.assertEqual(result["repeated_sibling_candidates"], [])

    def test_global_json_budget_trims_large_synthetic_report_and_keeps_valid_json(self):
        payload = json.loads(_summary_payload())
        payload["candidateMessages"] = [
            _node(id=f"private-id-{index}", **{
                "class": "message " + "x" * 180,
                "dataAttributes": [f"data-{'x' * 52}-{attr_index}" for attr_index in range(8)],
            })
            for index in range(100)
        ]
        payload["counts"]["messages"] = 100
        payload["repeatedSiblingGroups"] = [
            {"count": 12, "parent": _struct_node(tag="div", **{"class": f"list-{index}"}),
             "ancestors": [{"depth": 1, "node": _struct_node(tag="main"),
                 "childIndex": 1, "childElementCount": 100, "siblings": [
                     {"index": sibling, "node": _struct_node(**{"class": "wrapper " + "y" * 180})}
                     for sibling in range(4)], "siblingsTruncated": True}],
             "examples": [_struct_tree(class_name="message-bubble " + "z" * 180) for _ in range(4)],
             "examplesTruncated": True}
            for index in range(10)
        ]
        payload["counts"]["repeatedSiblingGroups"] = 10
        result = _sanitize_dom_summary(json.dumps(payload, ensure_ascii=False))
        initial_render = json.dumps(result, ensure_ascii=False, indent=2)
        self.assertLessEqual(len(initial_render.encode("utf-8")), _DOM_DIAGNOSTIC_MAX_BYTES)
        self.assertIn("total-json-byte-limit", result["truncated"]["reasons"])
        compact = _fit_dom_summary(result, max_bytes=25000)
        rendered = json.dumps(compact, ensure_ascii=False, indent=2)
        self.assertLessEqual(len(rendered.encode("utf-8")), 25000)
        self.assertEqual(json.loads(rendered)["schema"], "playerok-dom-structure-v1")
        self.assertIn("total-json-byte-limit", compact["truncated"]["reasons"])
        self.assertEqual(compact["counts"]["messages"], 100)
        self.assertNotIn("private-id-", rendered)

    def test_callback_byte_limit_returns_compact_structured_error(self):
        raw = " " * (_DOM_DIAGNOSTIC_CALLBACK_MAX_BYTES + 1)
        result = _sanitize_dom_summary(raw)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "callback-byte-limit")
        rendered = json.dumps(result)
        self.assertLess(len(rendered), 2000)
        self.assertEqual(json.loads(rendered)["schema"], "playerok-dom-structure-v1")

    def test_invalid_serialization_gets_specific_compact_reason(self):
        result = _sanitize_dom_summary("{broken-json")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid-json")
        self.assertEqual(json.loads(json.dumps(result))["truncated"]["reasons"], ["invalid-json"])

    def test_unexpected_sanitizer_failure_returns_compact_schema_error(self):
        page = _PageStub()
        self._attach_page(page)
        completed = []
        with self._patch_validity(), patch.object(
            playerok_browser, "_sanitize_dom_summary", side_effect=RecursionError("deep JSON")
        ):
            self.assertTrue(self.panel.read_dom_structure(lambda result, error: completed.append((result, error))))
            page.callback("{}")
            QCoreApplication.processEvents()
        self.assertEqual(len(completed), 1)
        result, error = completed[0]
        self.assertIsNone(error)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "serialization-error")
        self.assertEqual(json.loads(json.dumps(result))["schema"], "playerok-dom-structure-v1")

    def test_aria_free_text_and_origin_query_are_removed_before_display(self):
        payload = json.loads(_summary_payload())
        payload["page"]["origin"] = "https://playerok.com/private-path?token=private#fragment"
        payload["candidateContainers"][0]["aria"]["aria-label"] = "unsent private draft"
        result = _sanitize_dom_summary(json.dumps(payload))
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["page"], {"site": "playerok"})
        self.assertNotIn("private-path", rendered)
        self.assertNotIn("token=private", rendered)
        self.assertNotIn("unsent private draft", rendered)
        self.assertEqual(result["candidate_containers"][0]["aria"]["aria-label"], "[redacted-label]")

    def test_node_count_and_attribute_lengths_are_bounded(self):
        payload = json.loads(_summary_payload())
        payload["body"] = {"elementCount": 99_999_999, "maxDepth": 50_000, "scanTruncated": True}
        payload["counts"]["scannedElements"] = 99_999_999
        payload["counts"]["messages"] = 300
        payload["candidateMessages"] = [
            _node(id="x" * 180, **{"class": "c" * 180}) for _ in range(300)
        ]
        result = _sanitize_dom_summary(json.dumps(payload))
        self.assertEqual(result["body"]["element_count_scanned"], 12000)
        self.assertEqual(result["body"]["max_depth_scanned"], 12000)
        self.assertEqual(len(result["candidate_messages"]), 100)
        self.assertEqual(result["counts"]["messages"], 300)
        self.assertIn("candidate-messages-python-section-limit", result["truncated"]["reasons"])
        self.assertTrue(result["truncated"]["occurred"])
        self.assertTrue(result["candidate_messages"][0]["id_attribute_present"])
        self.assertNotIn("id", result["candidate_messages"][0])
        self.assertEqual(len(result["candidate_messages"][0]["class"]), 120)
        self.assertTrue(result["body"]["scan_truncated"])

    def test_unrecognized_role_and_impossible_link_count_are_removed_or_bounded(self):
        payload = json.loads(_summary_payload())
        payload["candidateContainers"][0]["role"] = "session-token-secret"
        payload["linkCount"] = 99_999_999
        payload["counts"]["links"] = 99_999_999
        result = _sanitize_dom_summary(json.dumps(payload))
        self.assertIsNone(result["candidate_containers"][0]["role"])
        self.assertEqual(result["counts"]["links"], 12000)

    def test_read_uses_latest_page_and_runs_only_read_only_dom_script(self):
        first_page = _PageStub()
        current_page = _PageStub()
        self._attach_page(first_page)
        self.panel.web.current_page = current_page
        completed = []
        with self._patch_validity():
            self.assertTrue(self.panel.read_dom_structure(lambda result, error: completed.append((result, error))))
            self.assertEqual(first_page.scripts, [])
            self.assertEqual(len(current_page.scripts), 1)
            script = current_page.scripts[0]
            for forbidden in (
                "fetch(", "XMLHttpRequest", "outerHTML", "textContent", "innerText",
                "localStorage", "sessionStorage", "dispatchEvent", ".click(",
            ):
                self.assertNotIn(forbidden, script)
            self.assertIn("repeatedSiblingGroups", script)
            self.assertIn("excludedRoots", script)
            self.assertIn("MAX_REPEAT_GROUPS = 10", script)
            self.assertIn("MAX_REPORT_BYTES = 192 * 1024", script)
            self.assertIn("new TextEncoder()", script)
            self.assertIn("json-byte-budget-unmet", script)
            self.assertNotRegex(script, r"(?:element|input|textarea|control)\s*\.\s*value\b")
            current_page.callback(_summary_payload())
            QCoreApplication.processEvents()
        self.assertEqual(len(completed), 1)
        self.assertIsNone(completed[0][1])
        self.assertEqual(completed[0][0]["schema"], "playerok-dom-structure-v1")
        self.assertEqual(current_page.url_calls, 1)
        self.assertIsNone(current_page.destroyed.callback)

    def test_graphql_bridge_runner_uses_current_page_and_rejects_late_stale_page_result(self):
        first_page = _PageStub()
        current_page = _PageStub()
        self._attach_page(first_page)
        self.panel.web.current_page = current_page
        results = []
        with self._patch_validity():
            self.assertTrue(self.panel._run_current_page_javascript(
                "window.__gamedelivery_test__ = true", results.append
            ))
            self.assertEqual(first_page.scripts, [])
            self.assertEqual(current_page.scripts, ["window.__gamedelivery_test__ = true"])
            callback = current_page.callback
            self.panel.web.current_page = first_page
            callback(True)
        self.assertEqual(results, [None])

    def test_not_ready_clearing_and_destroyed_page_fail_without_reading_dom(self):
        page = _PageStub()
        self._attach_page(page)
        completed = []
        with self._patch_validity():
            self.panel._page_ready = False
            self.assertFalse(self.panel.read_dom_structure(lambda result, error: completed.append((result, error))))
            self.panel._page_ready = True
            self.panel._clear_pending = True
            self.assertFalse(self.panel.read_dom_structure(lambda result, error: completed.append((result, error))))
            self.panel._clear_pending = False
            page.valid = False
            self.assertFalse(self.panel.read_dom_structure(lambda result, error: completed.append((result, error))))
        self.assertEqual(page.scripts, [])
        self.assertEqual(len(completed), 3)
        self.assertTrue(all(result is None and error for result, error in completed))

    def test_destroyed_page_completes_once_and_late_javascript_callback_is_ignored(self):
        page = _PageStub()
        self._attach_page(page)
        completed = []
        with self._patch_validity():
            self.assertTrue(self.panel.read_dom_structure(lambda result, error: completed.append((result, error))))
            callback = page.callback
            page.valid = False
            page.destroyed.emit(page)
            callback(_summary_payload())
        QCoreApplication.processEvents()
        self.assertEqual(len(completed), 1)
        self.assertIsNone(completed[0][0])
        self.assertIn("уничтожена", completed[0][1])
        self.assertFalse(self.panel._dom_diagnostic_active)

    def test_diagnostic_button_is_manual_and_does_not_call_sales_reader(self):
        page = _PageStub()
        self._attach_page(page)
        calls = []
        shown = []
        self.panel.read_dom_structure = lambda callback: (calls.append("dom") or callback({"safe": True}, None))
        self.panel._show_dom_diagnostic_text = shown.append
        with self._patch_validity(), patch.object(
            self.panel, "read_sales", side_effect=AssertionError("sales reader must not be called")
        ):
            self.assertTrue(self.panel.dom_diagnostic_button.isEnabled())
            # Merely constructing the panel does not trigger a diagnostic or a page script.
            self.assertEqual(page.scripts, [])
            self.assertEqual(page.callback, None)
            self.assertEqual(calls, [])
            self.panel.dom_diagnostic_button.click()
        self.assertEqual(calls, ["dom"])
        self.assertEqual(len(shown), 1)


if __name__ == "__main__":
    unittest.main()
