from __future__ import annotations

import unittest

from core.delivery.send_policy import DeliverySendPolicy


class MemorySettings:
    def __init__(self, initial=None):
        self.values = dict(initial or {})
        self.sync_error = False
        self.read_error = False

    def value(self, key, default=None):
        if self.read_error:
            raise OSError("settings read failed")
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        if self.sync_error:
            raise OSError("settings sync failed")

    def status(self):
        return 0


class DeliverySendPolicyTests(unittest.TestCase):
    KEY = DeliverySendPolicy.SETTING_KEY

    def test_startup_always_resets_persisted_enabled_value_to_off(self):
        settings = MemorySettings({self.KEY: True})

        policy = DeliverySendPolicy(settings)

        self.assertFalse(policy.auto_send_enabled())
        self.assertIs(settings.values[self.KEY], False)

    def test_missing_invalid_and_unreadable_setting_fail_closed(self):
        for value in (None, "true", 1, [], {}):
            with self.subTest(value=value):
                policy = DeliverySendPolicy(MemorySettings({self.KEY: value}))
                self.assertFalse(policy.auto_send_enabled())

        settings = MemorySettings({self.KEY: True})
        policy = DeliverySendPolicy(settings)
        settings.read_error = True
        self.assertFalse(policy.auto_send_enabled())

    def test_setting_can_be_enabled_and_disabled_and_is_persisted(self):
        settings = MemorySettings()
        policy = DeliverySendPolicy(settings)

        self.assertTrue(policy.set_auto_send_enabled(True))
        self.assertTrue(policy.auto_send_enabled())
        self.assertTrue(policy.set_auto_send_enabled(False))
        self.assertFalse(policy.auto_send_enabled())

    def test_settings_write_failure_never_enables_auto_send(self):
        settings = MemorySettings()
        policy = DeliverySendPolicy(settings)
        settings.sync_error = True

        self.assertFalse(policy.set_auto_send_enabled(True))
        self.assertFalse(policy.auto_send_enabled())

    def test_manual_authorization_is_scoped_and_consumed_once_without_enabling_auto(self):
        policy = DeliverySendPolicy(MemorySettings())
        grant = policy.issue_manual_authorization("order-1", "leg-a")

        self.assertEqual(grant.leg_id, "leg-a")
        self.assertTrue(policy.validate_manual_authorization(grant, "order-1", "leg-a"))
        self.assertFalse(policy.validate_manual_authorization(grant, "order-1", "leg-b"))
        self.assertTrue(policy.consume_manual_authorization(grant, "order-1", "leg-a"))
        self.assertFalse(policy.consume_manual_authorization(grant, "order-1", "leg-a"))
        self.assertFalse(policy.consume_manual_authorization(grant, "order-2", "leg-a"))
        self.assertFalse(policy.auto_send_enabled())

    def test_auto_claim_is_linearized_and_new_claims_stop_after_off(self):
        policy = DeliverySendPolicy(MemorySettings())
        policy.set_auto_send_enabled(True)

        allowed, result = policy.run_auto_dispatch_claim(
            lambda: {"_claimed": True}
        )
        self.assertTrue(allowed)
        self.assertTrue(result["_claimed"])

        policy.set_auto_send_enabled(False)
        second_allowed, second_result = policy.run_auto_dispatch_claim(
            lambda: {"_claimed": True}
        )
        self.assertFalse(second_allowed)
        self.assertIsNone(second_result)


if __name__ == "__main__":
    unittest.main()
