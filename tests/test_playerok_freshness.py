from __future__ import annotations

import threading
import sqlite3
import unittest

from core.delivery.playerok_freshness import (
    PlayerokFreshnessBlocked,
    PlayerokFreshnessGate,
    PlayerokFreshnessProof,
)
from integrations.playerok.browser_sales import (
    BrowserSalesSnapshot,
    _complete_browser_snapshot,
)
from tests.playerok_freshness_fixtures import reader_completed_snapshot


class PlayerokFreshnessGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = PlayerokFreshnessGate()
        self.lease = self.gate.try_acquire_order("order-1")
        self.snapshot = reader_completed_snapshot(
            "account-1", [{"deal_id": "deal-1", "status": "PAID"}]
        )

    @staticmethod
    def snapshot_for(account, deals):
        return reader_completed_snapshot(account, deals)

    def tearDown(self):
        self.gate.release_order(self.lease)

    def issue(self, *, snapshot=None, deal="deal-1", order="order-1", leg="leg-1", account="account-1"):
        return self.gate.issue_from_complete_snapshot(
            self.snapshot if snapshot is None else snapshot,
            expected_account_user_id=account,
            deal_id=deal,
            order_id=order,
            leg_id=leg,
            lease=self.lease,
        )

    def consume(self, proof, *, deal="deal-1", order="order-1", leg="leg-1", account="account-1", claim=None):
        return self.gate.consume_for_claim(
            proof,
            account_user_id=account,
            deal_id=deal,
            order_id=order,
            leg_id=leg,
            lease=self.lease,
            claim=claim or (lambda: {"_claimed": True}),
        )

    def test_complete_paid_snapshot_issues_single_leg_proof(self):
        proof = self.issue()
        self.assertIsInstance(proof, PlayerokFreshnessProof)
        self.assertEqual(self.consume(proof), {"_claimed": True})
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.consume(proof)

    def test_old_snapshot_without_explicit_complete_marker_is_rejected(self):
        snapshot = {"account_user_id": "account-1", "deals": [{"deal_id": "deal-1", "status": "PAID"}]}
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.issue(snapshot=snapshot)

    def test_non_paid_unknown_missing_and_duplicate_deal_fail_closed(self):
        for deals in (
            [{"deal_id": "deal-1", "status": "SENT"}],
            [{"deal_id": "deal-1", "status": "CONFIRMED"}],
            [{"deal_id": "deal-1", "status": "CANCELLED"}],
            [{"deal_id": "deal-1", "status": "UNKNOWN"}],
            [],
        ):
            with self.subTest(deals=deals), self.assertRaises(PlayerokFreshnessBlocked):
                self.issue(snapshot=self.snapshot_for("account-1", deals))

    def test_account_mismatch_rejected(self):
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.issue(snapshot=self.snapshot_for("other-account", [{"deal_id": "deal-1", "status": "PAID"}]))

    def test_plain_dict_with_complete_and_paid_claim_is_not_trusted(self):
        fabricated = {
            "complete": True, "account_user_id": "account-1",
            "deals": [{"deal_id": "deal-1", "status": "PAID"}],
        }
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.issue(snapshot=fabricated)

    def test_direct_completion_helper_does_not_create_trusted_provenance(self):
        fabricated = _complete_browser_snapshot(
            "account-1", [
                {"deal_id": "deal-1", "status": "PAID"},
                {"deal_id": "deal-1", "status": "PAID"},
            ]
        )
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.issue(snapshot=fabricated)

    def test_constructed_snapshot_is_not_trusted(self):
        fabricated = BrowserSalesSnapshot(
            "account-1", [{"deal_id": "deal-1", "status": "PAID"}]
        )
        self.assertFalse(fabricated.complete)
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.issue(snapshot=fabricated)

    def test_reader_snapshot_cannot_authorize_a_second_leg(self):
        snapshot = self.snapshot_for("account-1", [{"deal_id": "deal-1", "status": "PAID"}])
        self.issue(snapshot=snapshot, leg="leg-1")
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.issue(snapshot=snapshot, leg="leg-2")

    def test_proof_cannot_be_reused_for_another_leg_order_or_account(self):
        proof = self.issue()
        for kwargs in (
            {"leg": "leg-2"}, {"order": "order-2"}, {"account": "account-2"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PlayerokFreshnessBlocked):
                self.consume(proof, **kwargs)
        # Scope mismatch does not burn it for its one correct target.
        self.consume(proof)

    def test_constructed_proof_is_not_registered_capability(self):
        proof = self.issue()
        forged = PlayerokFreshnessProof(
            proof.account_user_id, proof.deal_id, proof.order_id,
            proof.leg_id, proof.checked_at, proof._nonce,
        )
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.consume(forged)

    def test_claim_exception_burns_proof_even_if_database_claim_rolls_back(self):
        proof = self.issue()
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE claim_log(value TEXT)")

        def rolled_back_claim():
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO claim_log(value) VALUES('claim')")
            raise RuntimeError("rollback")

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            self.consume(proof, claim=rolled_back_claim)
        db.rollback()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM claim_log").fetchone()[0], 0)
        with self.assertRaises(PlayerokFreshnessBlocked):
            self.consume(proof)
        db.close()

    def test_two_parallel_consumers_have_only_one_winner(self):
        proof = self.issue()
        barrier = threading.Barrier(3)
        outcomes = []

        def run():
            barrier.wait()
            try:
                self.consume(proof)
                outcomes.append("claimed")
            except PlayerokFreshnessBlocked:
                outcomes.append("blocked")

        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()
        self.assertCountEqual(outcomes, ["claimed", "blocked"])

    def test_order_lease_prevents_parallel_checks_and_release_restores_access(self):
        self.assertIsNone(self.gate.try_acquire_order("order-1"))
        self.gate.release_order(self.lease)
        self.lease = self.gate.try_acquire_order("order-1")
        self.assertIsNotNone(self.lease)


if __name__ == "__main__":
    unittest.main()
