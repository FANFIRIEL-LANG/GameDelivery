from __future__ import annotations

import json
import re
import unittest

from integrations.playerok.browser_sales import BrowserSalesReader


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

        _poll_script, poll_callback = requests.pop(0)
        poll_callback({"state": "pending"})
        self.assertTrue(scheduled)
        scheduled.pop(0)()
        _poll_script, poll_callback = requests.pop(0)
        poll_callback({"state": "done", "payload": payload})

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

        result, error = results[0]
        self.assertIsNone(error)
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
        self.complete_fetch(requests, scheduled, {"__http_status": 500})
        self.assertIsNone(results[0][0])

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
        self.complete_fetch(requests, scheduled, {"__http_status": 401})
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][0])
        self.assertIn("HTTP 401", results[0][1])
        self.assertEqual(requests, [])

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
        self.assertIsNone(results[0][1])
        self.assertEqual([d["deal_id"] for d in results[0][0]["deals"]], ["same-deal"])

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


if __name__ == "__main__":
    unittest.main()
