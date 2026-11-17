from __future__ import annotations

import json
import re
import unittest

from integrations.playerok.browser_sales import (
    BrowserSalesReader,
    BrowserViewerChecker,
    is_trusted_browser_sales_snapshot,
)


def raw_deal(deal_id, listing_id, chat_id):
    return {
        "id": deal_id,
        "status": "PAID",
        "createdAt": "2026-11-15T10:00:00Z",
        "item": {"id": listing_id, "name": "Listing"},
        "user": {"id": "buyer-id", "username": "buyer"},
        "chat": {"id": chat_id},
        "transaction": {"value": 5},
    }


def page(edges, *, has_next=False, cursor=None, total=1):
    return {"data": {"deals": {
        "edges": [{"node": deal} for deal in edges],
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "totalCount": total,
    }}}


class BrowserSalesReaderTests(unittest.TestCase):
    def make_reader(self):
        requests, scripts, results, scheduled = [], [], [], []

        def run(script, callback):
            scripts.append(script)
            requests.append((script, callback))

        reader = BrowserSalesReader(
            run,
            lambda result, error: results.append((result, error)),
            schedule=scheduled.append,
            page_size=2,
        )
        return reader, requests, scripts, results, scheduled

    def complete_fetch(self, requests, scheduled, payload):
        start_script, start_callback = requests.pop(0)
        match = re.search(r"const key=(\"[^\"]+\")", start_script)
        self.assertIsNotNone(match)
        key = json.loads(match.group(1))
        start_callback(key)

        poll_script, poll_callback = requests.pop(0)
        self.assertIn("JSON.stringify(envelope)", poll_script)
        poll_callback(json.dumps({"state": "pending"}))
        self.assertTrue(scheduled)
        scheduled.pop(0)()
        _poll_script, poll_callback = requests.pop(0)
        poll_callback(json.dumps({"state": "done", "payload": payload}))

    def test_multiple_pages_keep_deal_chat_listing_ids_independent(self):
        reader, requests, scripts, results, scheduled = self.make_reader()
        reader.start()
        self.assertIn("credentials", requests[0][0])
        self.assertNotIn("async()=>", requests[0][0])
        self.complete_fetch(requests, scheduled, {"id": "seller-id"})
        self.complete_fetch(
            requests, scheduled,
            page([raw_deal("d1", "l1", "c1")], has_next=True, cursor="next", total=2),
        )
        self.assertEqual(results, [])
        self.assertIn(r'\"after\":\"next\"', requests[0][0])
        self.complete_fetch(requests, scheduled, page([raw_deal("d2", "l2", "c2")], total=2))
        self.assertTrue(requests, "Reader должен повторно проверить viewer после последней страницы")
        self.complete_fetch(requests, scheduled, {"id": "seller-id"})

        result, error = results[0]
        self.assertIsNone(error)
        self.assertTrue(is_trusted_browser_sales_snapshot(result))
        self.assertEqual(reader.pages_read, 2)
        self.assertEqual(reader.deals_read, 2)
        self.assertTrue(reader.initial_viewer_received)
        self.assertTrue(reader.pagination_complete)
        self.assertTrue(reader.viewer_matched)
        self.assertEqual(result["account_user_id"], "seller-id")
        self.assertEqual(
            [(d["deal_id"], d["chat_id"], d["listing_id"]) for d in result["deals"]],
            [("d1", "c1", "l1"), ("d2", "c2", "l2")],
        )
        self.assertTrue(all("mutation" not in script.lower() for script in scripts))

    def test_missing_and_repeated_cursor_fail_without_snapshot(self):
        for cursor in (None, ""):
            with self.subTest(cursor=cursor):
                reader, requests, _scripts, results, scheduled = self.make_reader()
                reader.start()
                self.complete_fetch(requests, scheduled, {"id": "seller"})
                self.complete_fetch(
                    requests, scheduled,
                    page([raw_deal("d1", "l1", "c1")], has_next=True, cursor=cursor),
                )
                self.assertIsNone(results[0][0])
                self.assertIn("курсор", results[0][1].lower())

        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(
            requests, scheduled,
            page([raw_deal("d1", "l1", "c1")], has_next=True, cursor="c1", total=2),
        )
        self.complete_fetch(
            requests, scheduled,
            page([raw_deal("d2", "l2", "c2")], has_next=True, cursor="c1", total=2),
        )
        self.assertIsNone(results[0][0])
        self.assertIn("курсор", results[0][1].lower())

    def test_second_page_error_and_invalid_page_info_never_return_partial_results(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(
            requests, scheduled,
            page([raw_deal("d1", "l1", "c1")], has_next=True, cursor="c1", total=2),
        )
        self.complete_fetch(requests, scheduled, {"__diagnostic": "http_error", "status": 500})
        self.assertIsNone(results[0][0])
        self.assertEqual(reader.pages_read, 1)
        self.assertEqual(reader.deals_read, 1)
        self.assertFalse(reader.pagination_complete)

        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(
            requests, scheduled,
            {"data": {"deals": {"edges": [], "pageInfo": {"hasNextPage": "false"}}}},
        )
        self.assertIsNone(results[0][0])

    def test_viewer_failure_is_reported_without_fetching_deals(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"__diagnostic": "http_error", "status": 401})
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][0])
        self.assertIn("HTTP 401", results[0][1])
        self.assertEqual(requests, [])

    def test_unknown_diagnostic_marker_with_valid_viewer_is_rejected_without_snapshot(self):
        reader, requests, scripts, results, scheduled = self.make_reader()
        reader.start()
        self.assertIn("hasOwnProperty.call(payload,'__diagnostic')", scripts[0])
        self.assertIn("typeof marker!=='string'", scripts[0])
        self.assertIn("cause:'invalid_state'", scripts[0])
        self.complete_fetch(
            requests, scheduled,
            {"__diagnostic": "future_private_marker", "id": "valid-looking-viewer"},
        )
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][0])
        self.assertIn("invalid_diagnostic", results[0][1])
        self.assertNotIn("future_private_marker", results[0][1])
        self.assertFalse(is_trusted_browser_sales_snapshot(results[0][0]))
        self.assertEqual(requests, [])

    def test_unknown_diagnostic_marker_with_valid_deals_page_is_rejected_before_pagination(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        payload = page([raw_deal("d1", "l1", "c1")])
        payload["__diagnostic"] = "future_private_marker"
        self.complete_fetch(requests, scheduled, payload)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][0])
        self.assertIn("invalid_diagnostic", results[0][1])
        self.assertNotIn("future_private_marker", results[0][1])
        self.assertFalse(is_trusted_browser_sales_snapshot(results[0][0]))
        self.assertEqual(requests, [])

    def test_invalid_type_diagnostic_marker_is_rejected_without_parsing_viewer(self):
        for marker in (None, 1, True, [], {}):
            with self.subTest(marker_type=type(marker).__name__):
                reader, requests, _scripts, results, scheduled = self.make_reader()
                reader.start()
                self.complete_fetch(requests, scheduled, {"__diagnostic": marker, "id": "viewer"})
                self.assertIsNone(results[0][0])
                self.assertIn("invalid_diagnostic", results[0][1])
                self.assertEqual(requests, [])

    def test_known_diagnostic_markers_remain_failures_and_marker_absence_succeeds(self):
        known = (
            ("javascript_start_error", {"cause": "SyntaxError"}),
            ("request_id_error", {}),
            ("poll_state_error", {"cause": "missing_state"}),
            ("fetch_error", {"cause": "TypeError"}),
            ("http_error", {"status": 503}),
            ("json_parse_error", {}),
            ("viewer_parse_error", {}),
        )
        for marker, extra in known:
            with self.subTest(marker=marker):
                error = BrowserSalesReader._response_error({"__diagnostic": marker, **extra})
                self.assertIsNotNone(error)
                self.assertIn(marker, error)

        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(requests, scheduled, page([raw_deal("d1", "l1", "c1")]))
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.assertIsNone(results[0][1])
        self.assertTrue(is_trusted_browser_sales_snapshot(results[0][0]))

    def test_fetch_rejection_reports_only_safe_exception_name(self):
        reader, requests, scripts, results, scheduled = self.make_reader()
        reader.start()
        self.assertIn("__diagnostic:'fetch_error'", scripts[0])
        self.complete_fetch(
            requests, scheduled,
            {"__diagnostic": "fetch_error", "cause": "TypeError", "message": "private response detail"},
        )
        self.assertIsNone(results[0][0])
        self.assertIn("fetch_error", results[0][1])
        self.assertIn("TypeError", results[0][1])
        self.assertNotIn("private response detail", results[0][1])

    def test_invalid_json_response_has_separate_diagnostic(self):
        reader, requests, scripts, results, scheduled = self.make_reader()
        reader.start()
        self.assertIn("__diagnostic:'json_parse_error'", scripts[0])
        self.complete_fetch(requests, scheduled, {"__diagnostic": "json_parse_error"})
        self.assertIsNone(results[0][0])
        self.assertIn("json_parse_error", results[0][1])

    def test_http_error_has_status_without_response_body(self):
        reader, requests, scripts, results, scheduled = self.make_reader()
        reader.start()
        self.assertIn("__diagnostic:'http_error',status:r.status", scripts[0])
        self.complete_fetch(
            requests, scheduled,
            {"__diagnostic": "http_error", "status": 503, "body": "must not be shown"},
        )
        self.assertIsNone(results[0][0])
        self.assertIn("http_error", results[0][1])
        self.assertIn("503", results[0][1])
        self.assertNotIn("must not be shown", results[0][1])

    def test_last_graphql_http_status_is_captured_only_when_valid(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                reader, requests, _scripts, results, scheduled = self.make_reader()
                reader.start()
                self.complete_fetch(requests, scheduled, {"id": "seller-id"})
                self.assertTrue(requests)
                graphql_start_script = requests[0][0]
                self.assertIn("Number.isInteger(httpStatus)", graphql_start_script)
                self.assertIn("result.__gamedelivery_graphql_http_status=httpStatus", graphql_start_script)
                self.complete_fetch(
                    requests, scheduled,
                    {
                        "__diagnostic": "http_error", "status": status,
                        "__gamedelivery_graphql_http_status": status,
                    },
                )
                self.assertEqual(reader.last_graphql_http_status, status)
                self.assertIsNone(results[0][0])

        for status in (None, True, 99, 600, "403"):
            with self.subTest(invalid_status=status):
                reader, requests, _scripts, results, scheduled = self.make_reader()
                reader.start()
                self.complete_fetch(requests, scheduled, {"id": "seller-id"})
                self.complete_fetch(
                    requests, scheduled,
                    {
                        "__diagnostic": "http_error", "status": status,
                        "__gamedelivery_graphql_http_status": status,
                    },
                )
                self.assertIsNone(reader.last_graphql_http_status)
                self.assertIsNone(results[0][0])

    def test_graphql_status_is_not_taken_from_viewer_response(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(
            requests, scheduled,
            {"id": "seller-id", "__gamedelivery_graphql_http_status": 403},
        )
        self.assertIsNone(reader.last_graphql_http_status)
        self.assertTrue(requests, "Viewer success should proceed to the GraphQL request")

    def test_missing_latest_graphql_status_does_not_reuse_previous_page_status(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller-id"})
        first_page = page(
            [raw_deal("d1", "l1", "c1")], has_next=True, cursor="next", total=2
        )
        first_page["__gamedelivery_graphql_http_status"] = 200
        self.complete_fetch(requests, scheduled, first_page)
        self.assertEqual(reader.last_graphql_http_status, 200)
        self.complete_fetch(
            requests, scheduled,
            {"__diagnostic": "http_error", "message": "must not be surfaced"},
        )
        self.assertIsNone(reader.last_graphql_http_status)
        self.assertIsNone(results[0][0])

    def test_unexpected_javascript_callback_value_is_request_id_error(self):
        reader, requests, _scripts, results, _scheduled = self.make_reader()
        reader.start()
        _script, callback = requests.pop(0)
        callback("different-request")
        self.assertIsNone(results[0][0])
        self.assertIn("request_id_error", results[0][1])
        self.assertEqual(requests, [])

    def test_missing_and_malformed_poll_state_have_safe_diagnostics(self):
        for poll_value, expected_cause in (
            (json.dumps({"state": "missing"}), "missing_state"),
            (json.dumps({"state": "done"}), "invalid_envelope"),
            (json.dumps({"state": "serialization_error"}), "serialization_error"),
            (["invalid"], "invalid_callback"),
            (json.dumps({"state": "error", "cause": "SecurityError"}), "SecurityError"),
        ):
            with self.subTest(poll_value=poll_value):
                reader, requests, _scripts, results, _scheduled = self.make_reader()
                reader.start()
                _start_script, started = requests.pop(0)
                match = re.search(r"const key=(\"[^\"]+\")", _start_script)
                self.assertIsNotNone(match)
                started(json.loads(match.group(1)))
                _poll_script, polled = requests.pop(0)
                polled(poll_value)
                self.assertIsNone(results[0][0])
                self.assertIn("poll_state_error", results[0][1])
                self.assertIn(expected_cause, results[0][1])

    def test_poll_uses_strict_json_string_envelopes_and_never_trusts_invalid_results(self):
        invalid_values = (
            (None, "invalid_callback"),
            ("not-json", "invalid_json"),
            ({"state": "done", "payload": {"id": "seller"}}, "invalid_callback"),
            (42, "invalid_callback"),
            (json.dumps(["state", "done"]), "invalid_envelope"),
            (json.dumps({"state": "unexpected"}), "unknown_state"),
            (json.dumps({"state": "done"}), "invalid_envelope"),
            (json.dumps({"state": "done", "payload": None}), "invalid_payload"),
            (
                json.dumps({"state": "done", "payload": {"id": "seller", "__diagnostic": "future_marker"}}),
                "invalid_diagnostic",
            ),
            (
                json.dumps({"state": "done", "payload": {"id": "seller", "__diagnostic": 7}}),
                "invalid_diagnostic",
            ),
            (json.dumps({"state": "error", "cause": 7}), "invalid_envelope"),
        )
        for value, cause in invalid_values:
            with self.subTest(value=value):
                reader, requests, _scripts, results, _scheduled = self.make_reader()
                reader.start()
                _start_script, start_callback = requests.pop(0)
                request_id = json.loads(re.search(r"const key=(\"[^\"]+\")", _start_script).group(1))
                start_callback(request_id)
                _poll_script, poll_callback = requests.pop(0)
                poll_callback(value)
                self.assertEqual(len(results), 1)
                self.assertIsNone(results[0][0])
                self.assertIn("poll_state_error", results[0][1])
                self.assertIn(cause, results[0][1])
                self.assertFalse(is_trusted_browser_sales_snapshot(results[0][0]))
                self.assertEqual(reader.pages_read, 0)
                self.assertFalse(reader.initial_viewer_received)
                self.assertNotIn("future_marker", results[0][1])

    def test_reader_timeout_and_page_destruction_ignore_late_poll_callback(self):
        for stage in ("timeout", "page_destroyed"):
            with self.subTest(stage=stage):
                reader, requests, _scripts, results, _scheduled = self.make_reader()
                reader.start()
                _start_script, start_callback = requests.pop(0)
                match = re.search(r"const key=(\"[^\"]+\")", _start_script)
                start_callback(json.loads(match.group(1)))
                _poll_script, poll_callback = requests.pop(0)
                reader.cancel(stage=stage)
                poll_callback(json.dumps({"state": "done", "payload": {"id": "seller"}}))
                self.assertEqual(len(results), 1)
                self.assertIsNone(results[0][0])
                self.assertIn("poll_state_error", results[0][1])
                self.assertFalse(is_trusted_browser_sales_snapshot(results[0][0]))

    def test_javascript_start_exception_is_sanitized(self):
        results = []

        def run(_script, _callback):
            raise RuntimeError("sensitive Qt diagnostic detail")

        reader = BrowserSalesReader(run, lambda result, error: results.append((result, error)), schedule=lambda _cb: None)
        reader.start()
        self.assertIsNone(results[0][0])
        self.assertIn("javascript_start_error", results[0][1])
        self.assertIn("RuntimeError", results[0][1])
        self.assertNotIn("sensitive Qt diagnostic detail", results[0][1])

        reader, requests, _scripts, results, _scheduled = self.make_reader()
        reader.start()
        _script, callback = requests.pop(0)
        callback({
            "__diagnostic": "javascript_start_error",
            "cause": "SyntaxError",
            "message": "private page details",
        })
        self.assertIsNone(results[0][0])
        self.assertIn("javascript_start_error", results[0][1])
        self.assertIn("SyntaxError", results[0][1])
        self.assertNotIn("private page details", results[0][1])

    def test_valid_viewer_json_advances_to_sales_query(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "viewer-123"})
        self.assertEqual(results, [])
        self.assertTrue(requests)
        self.assertIn("/graphql", requests[0][0])

    def test_missing_viewer_id_has_viewer_parse_category(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"displayName": "not an ID"})
        self.assertIsNone(results[0][0])
        self.assertIn("viewer_parse_error", results[0][1])

    def test_timeout_and_page_destruction_report_poll_stage(self):
        for stage, marker in (("timeout", "истекло время"), ("page_destroyed", "уничтожена")):
            with self.subTest(stage=stage):
                reader, _requests, _scripts, results, _scheduled = self.make_reader()
                reader.start()
                reader.cancel(stage=stage)
                self.assertIsNone(results[0][0])
                self.assertIn("poll_state_error", results[0][1])
                self.assertIn(marker, results[0][1])

    def test_total_count_mismatch_and_conflicting_duplicate_fail_without_snapshot(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(
            requests, scheduled,
            page([raw_deal("d1", "l1", "c1")], has_next=True, cursor="c1", total=3),
        )
        self.complete_fetch(requests, scheduled, page([raw_deal("d2", "l2", "c2")], total=2))
        self.assertIsNone(results[0][0])
        self.assertIn("totalCount", results[0][1])

        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(
            requests, scheduled,
            page([raw_deal("d1", "l1", "c1")], has_next=True, cursor="c1", total=3),
        )
        self.complete_fetch(requests, scheduled, page([raw_deal("d2", "l2", "c2")], total=3))
        self.assertIsNone(results[0][0])
        self.assertIn("не совпало", results[0][1])

        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(
            requests, scheduled,
            page([raw_deal("d1", "l1", "c1")], has_next=True, cursor="c1", total=2),
        )
        self.complete_fetch(requests, scheduled, page([raw_deal("d1", "l1", "changed-chat")], total=2))
        self.assertIsNone(results[0][0])
        self.assertIn("конфликт", results[0][1].lower())

    def test_identical_duplicate_is_deduplicated_by_deal_id(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        duplicate = raw_deal("same-deal", "same-listing", "same-chat")
        self.complete_fetch(
            requests, scheduled,
            page([duplicate], has_next=True, cursor="next", total=1),
        )
        self.complete_fetch(requests, scheduled, page([duplicate], total=1))
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.assertIsNone(results[0][1])
        self.assertIs(results[0][0]["complete"], True)
        self.assertEqual(results[0][0]["account_user_id"], "seller")
        self.assertEqual([d["deal_id"] for d in results[0][0]["deals"]], ["same-deal"])

    def test_viewer_change_after_pagination_rejects_snapshot(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller-a"})
        self.complete_fetch(requests, scheduled, page([raw_deal("d1", "l1", "c1")]))
        self.complete_fetch(requests, scheduled, {"id": "seller-b"})
        self.assertIsNone(results[0][0])
        self.assertIn("изменился", results[0][1])

    def test_final_viewer_error_rejects_snapshot(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(requests, scheduled, page([raw_deal("d1", "l1", "c1")]))
        self.complete_fetch(requests, scheduled, {"__diagnostic": "http_error", "status": 401})
        self.assertIsNone(results[0][0])
        self.assertIn("HTTP 401", results[0][1])

    def test_missing_final_viewer_id_rejects_snapshot(self):
        reader, requests, _scripts, results, scheduled = self.make_reader()
        reader.start()
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        self.complete_fetch(requests, scheduled, page([raw_deal("d1", "l1", "c1")]))
        self.complete_fetch(requests, scheduled, {})
        self.assertIsNone(results[0][0])
        self.assertIn("viewer_parse_error", results[0][1])

    def test_cancelled_page_does_not_complete_again_when_callback_arrives(self):
        reader, requests, _scripts, results, _scheduled = self.make_reader()
        reader.start()
        late_callback = requests[0][1]
        reader.cancel()
        late_callback("__gamedelivery_sales_pending")
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][0])

    def test_browser_scripts_never_return_or_embed_cookie_or_token_values(self):
        reader, requests, scripts, results, scheduled = self.make_reader()
        reader.start()
        self.assertIn('"credentials":"include"', scripts[0])
        self.assertIn("return {id:viewer&&viewer.id}", scripts[0])
        self.assertNotIn("Cookie", scripts[0])
        self.assertNotIn("Authorization", scripts[0])
        self.complete_fetch(requests, scheduled, {"id": "seller"})
        script = scripts[-1]
        self.assertIn(r'\"operationName\":\"Deals\"', script)
        self.assertNotIn("mutation", script.lower())
        self.assertFalse(results)


class BrowserViewerCheckerTests(unittest.TestCase):
    def make_checker(self, run=None, *, page_alive=None):
        requests, scripts, results, scheduled = [], [], [], []

        def default_run(script, callback):
            scripts.append(script)
            requests.append((script, callback))

        checker = BrowserViewerChecker(
            run or default_run,
            lambda authorized, error: results.append((authorized, error)),
            schedule=scheduled.append,
            is_page_alive=page_alive,
        )
        return checker, requests, scripts, results, scheduled

    def complete_viewer_check(self, requests, scheduled, payload):
        start_script, started = requests.pop(0)
        match = re.search(r"const key=(\"[^\"]+\")", start_script)
        self.assertIsNotNone(match)
        started(json.loads(match.group(1)))
        _poll_script, poll = requests.pop(0)
        poll(json.dumps({"state": "pending"}))
        scheduled.pop(0)()
        _poll_script, poll = requests.pop(0)
        poll(json.dumps({"state": "done", "payload": payload}))

    def test_success_uses_only_current_session_viewer_get_and_does_not_return_id(self):
        checker, requests, scripts, results, scheduled = self.make_checker()
        checker.start()
        script = scripts[0]
        self.assertIn("https://bff.playerok.com/rest-api/public/viewer", script)
        self.assertIn('"method":"GET"', script)
        self.assertIn('"credentials":"include"', script)
        self.assertNotIn("/graphql", script)
        self.complete_viewer_check(requests, scheduled, {"id": "mock-viewer-id"})
        self.assertIn("JSON.stringify(envelope)", scripts[1])
        self.assertEqual(results, [(True, None)])
        self.assertNotIn("mock-viewer-id", repr(results))
        self.assertFalse(checker.active)

    def test_http_fetch_json_and_viewer_errors_are_safe(self):
        scenarios = (
            ({"__diagnostic": "http_error", "status": 401, "body": "private"}, "http_error", "401"),
            ({"__diagnostic": "fetch_error", "cause": "TypeError", "message": "private"}, "fetch_error", "TypeError"),
            ({"__diagnostic": "json_parse_error", "body": "private"}, "json_parse_error", None),
            ({"id": None}, "viewer_parse_error", None),
            ({"id": True}, "viewer_parse_error", None),
        )
        for payload, category, safe_detail in scenarios:
            with self.subTest(category=category, payload=payload):
                checker, requests, _scripts, results, scheduled = self.make_checker()
                checker.start()
                self.complete_viewer_check(requests, scheduled, payload)
                self.assertFalse(results[0][0])
                self.assertIn(category, results[0][1])
                self.assertNotIn("private", results[0][1])
                if safe_detail:
                    self.assertIn(safe_detail, results[0][1])

    def test_javascript_bridge_and_poll_errors_are_reported_without_exception_text(self):
        def broken_run(_script, _callback):
            raise RuntimeError("sensitive bridge detail")

        checker, _requests, _scripts, results, _scheduled = self.make_checker(run=broken_run)
        checker.start()
        self.assertFalse(results[0][0])
        self.assertIn("javascript_start_error", results[0][1])
        self.assertNotIn("sensitive bridge detail", results[0][1])

        checker, requests, _scripts, results, _scheduled = self.make_checker()
        checker.start()
        _script, started = requests.pop(0)
        started("wrong-id")
        self.assertIn("request_id_error", results[0][1])

        checker, requests, _scripts, results, _scheduled = self.make_checker()
        checker.start()
        start_script, started = requests.pop(0)
        key = json.loads(re.search(r"const key=(\"[^\"]+\")", start_script).group(1))
        started(key)
        _script, poll = requests.pop(0)
        poll(json.dumps({"state": "missing"}))
        self.assertIn("poll_state_error", results[0][1])

    def test_timeout_and_page_destruction_have_distinct_safe_results(self):
        for stage, category in (("timeout", "timeout_error"), ("page_destroyed", "page_destroyed_error")):
            with self.subTest(stage=stage):
                checker, _requests, _scripts, results, _scheduled = self.make_checker()
                checker.start()
                checker.cancel(stage=stage)
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0][0])
                self.assertIn(category, results[0][1])

    def test_poll_accepts_only_json_string_envelopes(self):
        invalid_values = (
            (None, "неверного типа"),
            ("not-json", "JSON-envelope"),
            (["state", "done"], "неверного типа"),
            (42, "неверного типа"),
            (json.dumps({"payload": {"id": "viewer"}}), "неизвестное состояние"),
            (json.dumps({"state": "unexpected"}), "неизвестное состояние"),
            (json.dumps({"state": "serialization_error"}), "сериализовать состояние"),
        )
        for value, detail in invalid_values:
            with self.subTest(value=value):
                checker, requests, _scripts, results, _scheduled = self.make_checker()
                checker.start()
                _start_script, started = requests.pop(0)
                request_id = json.loads(re.search(r"const key=(\"[^\"]+\")", _start_script).group(1))
                started(request_id)
                _poll_script, poll = requests.pop(0)
                poll(value)
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0][0])
                self.assertIn(detail, results[0][1])

    def test_json_pending_and_done_envelopes_complete_successfully(self):
        checker, requests, scripts, results, scheduled = self.make_checker()
        checker.start()
        _start_script, started = requests.pop(0)
        request_id = json.loads(re.search(r"const key=(\"[^\"]+\")", scripts[0]).group(1))
        started(request_id)
        _poll_script, pending_callback = requests.pop(0)
        pending_callback(json.dumps({"state": "pending"}))
        self.assertEqual(len(scheduled), 1)
        scheduled.pop(0)()
        _poll_script, done_callback = requests.pop(0)
        done_callback(json.dumps({"state": "done", "payload": {"id": "temporary-viewer"}}))
        self.assertEqual(results, [(True, None)])
        self.assertNotIn("temporary-viewer", repr(results))

    def test_page_destroyed_invalid_callback_is_classified_and_late_callbacks_are_ignored(self):
        alive = [True]
        checker, requests, _scripts, results, _scheduled = self.make_checker(
            page_alive=lambda: alive[0]
        )
        checker.start()
        _start_script, started = requests.pop(0)
        started(checker._request_id)
        _poll_script, poll = requests.pop(0)
        alive[0] = False
        poll(None)
        checker.cancel(stage="page_destroyed")
        poll(json.dumps({"state": "done", "payload": {"id": "viewer"}}))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIn("page_destroyed_error", results[0][1])

    def test_callback_after_timeout_cannot_complete_or_confirm_later(self):
        checker, requests, _scripts, results, _scheduled = self.make_checker()
        checker.start()
        _start_script, started = requests.pop(0)
        started(checker._request_id)
        _poll_script, poll = requests.pop(0)
        checker.cancel(stage="timeout")
        poll(json.dumps({"state": "done", "payload": {"id": "viewer"}}))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIn("timeout_error", results[0][1])

    def test_repeated_poll_callback_after_success_does_not_complete_twice(self):
        checker, requests, _scripts, results, _scheduled = self.make_checker()
        checker.start()
        _start_script, started = requests.pop(0)
        started(checker._request_id)
        _poll_script, poll = requests.pop(0)
        success = json.dumps({"state": "done", "payload": {"id": "viewer"}})
        poll(success)
        poll(success)
        self.assertEqual(results, [(True, None)])


if __name__ == "__main__":
    unittest.main()
