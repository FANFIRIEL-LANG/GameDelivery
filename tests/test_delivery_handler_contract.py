from __future__ import annotations

import unittest

from core.delivery.handler_contract import (
    HANDLER_CONTRACT_VERSION,
    DeliveryHandlerKey,
    DeliveryHandlerRegistry,
    DeliveryHandlerResult,
)
from core.delivery.asf_trade_handler import ASFTradeOfferHandler, register_asf_trade_offer_handlers
from games.cs2.delivery_handler import CS2CapabilityAcceptance, CS2TradeOfferHandler, register_cs2_trade_offer_handler


class FakeHandler:
    contract_version = HANDLER_CONTRACT_VERSION
    key = DeliveryHandlerKey(" Steam_Card_Set ", " ASF_Trade ")

    def __init__(self, supported=True):
        self.supported = supported

    def supports(self, request):
        return self.supported

    def preview(self, request):
        return DeliveryHandlerResult("READY")

    def prepare(self, request):
        return DeliveryHandlerResult("PREPARED")

    def execute(self, prepared):
        return DeliveryHandlerResult("SENT", external_action_started=True)

    def reconcile(self, attempt):
        return DeliveryHandlerResult("UNKNOWN")


class DeliveryHandlerContractTests(unittest.TestCase):
    def test_cs2_handler_requires_all_capability_evidence(self):
        registry = DeliveryHandlerRegistry()
        capability = CS2CapabilityAcceptance(preview_verified=True)
        self.assertFalse(register_cs2_trade_offer_handler(registry, capability=capability))
        self.assertEqual(registry.registered_keys(), ())
        with self.assertRaises(ValueError):
            CS2TradeOfferHandler(capability=capability)

    def test_cs2_handler_is_exact_730_2_route_after_acceptance(self):
        capability = CS2CapabilityAcceptance(True, True, True, True)
        registry = DeliveryHandlerRegistry()
        self.assertTrue(register_cs2_trade_offer_handler(registry, capability=capability))
        handler = registry.resolve(
            item_type="cs2_item", delivery_method="steam_trade_offer",
            request={"item_type": "cs2_item", "delivery_method": "steam_trade_offer",
                     "order_id": "order-1", "leg_id": "leg-1", "app_id": 730, "context_id": "2"},
        )
        self.assertIsInstance(handler, CS2TradeOfferHandler)
        with self.assertRaises(LookupError):
            registry.resolve(
                item_type="cs2_item", delivery_method="steam_trade_offer",
                request={"item_type": "cs2_item", "delivery_method": "steam_trade_offer",
                         "order_id": "order-1", "leg_id": "leg-1", "app_id": 753, "context_id": "6"},
            )
    def test_asf_trade_offer_registry_includes_steam_card_sets(self):
        registry = DeliveryHandlerRegistry()
        register_asf_trade_offer_handlers(registry)
        self.assertEqual(
            [(key.item_type, key.delivery_method) for key in registry.registered_keys()],
            [("steam_asset", "steam_trade_offer"), ("steam_card_set", "steam_trade_offer")],
        )

    def test_key_normalizes_category_and_method(self):
        key = DeliveryHandlerKey(" CS2_ITEM ", " ASF_Trade ")
        self.assertEqual((key.item_type, key.delivery_method), ("cs2_item", "asf_trade"))

    def test_key_rejects_missing_parts(self):
        for item_type, method in (("", "asf"), ("cs2", " "), (None, "asf")):
            with self.subTest(item_type=item_type, method=method):
                with self.assertRaises(ValueError):
                    DeliveryHandlerKey(item_type, method)

    def test_result_normalizes_status_and_validates_flags(self):
        result = DeliveryHandlerResult(" sent ", external_reference="  123  ")
        self.assertEqual(result.status, "SENT")
        self.assertEqual(result.external_reference, "123")
        with self.assertRaises(TypeError):
            DeliveryHandlerResult("SENT", retryable=1)

    def test_registry_resolves_only_exact_item_type_and_method(self):
        registry = DeliveryHandlerRegistry()
        handler = FakeHandler()
        registry.register(handler)
        self.assertIs(
            registry.resolve(
                item_type="steam_card_set", delivery_method="asf_trade", request={}
            ),
            handler,
        )
        with self.assertRaises(LookupError):
            registry.resolve(
                item_type="cs2_item", delivery_method="asf_trade", request={}
            )
        self.assertEqual(registry.registered_keys(), (handler.key,))

    def test_registry_rejects_duplicate_key(self):
        registry = DeliveryHandlerRegistry()
        registry.register(FakeHandler())
        with self.assertRaises(ValueError):
            registry.register(FakeHandler())

    def test_registry_rejects_unsupported_request(self):
        registry = DeliveryHandlerRegistry()
        registry.register(FakeHandler(supported=False))
        with self.assertRaises(LookupError):
            registry.resolve(
                item_type="steam_card_set", delivery_method="asf_trade", request={}
            )

    def test_registry_rejects_wrong_contract_version(self):
        class WrongVersion(FakeHandler):
            contract_version = HANDLER_CONTRACT_VERSION + 1

        with self.assertRaises(ValueError):
            DeliveryHandlerRegistry().register(WrongVersion())

    def test_asf_handler_runs_lifecycle_and_preserves_existing_dispatch_result(self):
        calls = []
        handler = ASFTradeOfferHandler(
            item_type="steam_asset",
            execute_callback=lambda request: calls.append(request) or {
                "dispatched": True, "status": "SENT_TO_STEAM",
                "trade_offer_id": "offer-1",
            },
            reconcile_callback=lambda attempt: [{
                "leg_id": attempt["leg_id"], "status": "ACCEPTED", "reconciled": True,
            }],
        )
        request = {
            "item_type": "steam_asset", "delivery_method": "steam_trade_offer",
            "order_id": "order-1", "leg_id": "leg-1",
        }

        self.assertEqual(handler.preview(request).status, "READY")
        self.assertEqual(handler.prepare(request).status, "PREPARED")
        result = handler.execute(request)
        self.assertEqual(result.status, "SENT_TO_STEAM")
        self.assertTrue(result.external_action_started)
        self.assertEqual(result.external_reference, "offer-1")
        self.assertEqual(result.details["trade_offer_id"], "offer-1")
        reconciled = handler.reconcile(request)
        self.assertEqual(reconciled.status, "ACCEPTED")
        self.assertTrue(reconciled.details["reconciled"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class DigitalCodeHandlerRegistrationTests(unittest.TestCase):
    def test_digital_handler_is_explicitly_opt_in(self):
        from core.delivery.handler_contract import DeliveryHandlerRegistry
        from core.delivery.digital_code_handler import DigitalCodeHandler
        registry = DeliveryHandlerRegistry()
        registry.register(DigitalCodeHandler())
        handler = registry.resolve(item_type="digital_text_unit", delivery_method="text_delivery", request={"item_type":"digital_text_unit","delivery_method":"text_delivery","order_id":"o","leg_id":"l"})
        self.assertEqual(handler.execute({"item_type":"digital_text_unit","delivery_method":"text_delivery","order_id":"o","leg_id":"l"}).status, "ACTION_REQUIRED")
