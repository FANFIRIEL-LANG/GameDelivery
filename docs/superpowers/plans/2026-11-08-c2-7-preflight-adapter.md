# C.2.7 Multi-Game Preflight Adapter Plan

## Goal

Add an opt-in adapter from current Steam-card preflight inputs to the pure C.2.7 allocator. Preserve the existing `prepare()` behavior and leave the new method without production callers until integration tests pass.

## Findings and exact inputs

- `Q`: `SteamCardOrderPreflight.prepare(requested_sets=...)` already names the quantity in sets. `OrderService` stores generic `quantity`; it is not safe to reinterpret that field as card sets without a typed order contract.
- `A_g`: `find_shared_game_sets(inventories)` yields `SharedGameSets.available_sets` and exact `(bot_name, asset_id)` tuples. Because observed display names do not prove a complete card set, the adapter requires verified complete `MarketHashName` lists by AppID and counts only those exact hashes. It removes assets reserved by other orders before counting; the final reservation is still checked atomically to handle races. The caller must confirm seller snapshots are fresh using the existing snapshot status.
- AppID: `CardCopy`, `RecipientBadge`, `SharedGameSets`, and the generic order row do not carry AppID. `games.catalog.GAME_CATALOG` is the only current name-to-AppID source and covers only its listed games. Unknown game names must fail closed.
- `R_g`: `evaluate_recipient_badge_capacity()` returns `5 - badge level`; C.2.2 additionally subtracts full sets already held by the recipient. No recipient-inventory complete-set model is wired into Python preflight, so the adapter requires a verified `buyer_complete_sets_by_app_id` mapping. Missing or unverified counts must fail closed; never assume zero.

## Design

Add `SteamCardOrderPreflight.prepare_multi_game()` as a separate opt-in method and a distinct multi-game result dataclass. It will use existing `CardCopy`, `RecipientBadge`, `SharedGameSets`, `OwnedSet`, and `AssetReservationStore` structures, with the known AppIDs from `GAME_CATALOG`. The catalog is partial; unknown titles fail closed. It will calculate `R_g = max(0, badge_capacity.remaining_sets - buyer_complete_sets)`, calculate unreserved `A_g` using exact expected market hashes, call `plan_badge_allocation()`, map the exact selected shared sets back to `OwnedSet`, and only then call `reserve()` once with the complete asset list.

The existing `prepare()` method remains unchanged. An infeasible exact quantity returns a blocked result with no selected sets or reserved assets. If an order already has a different reservation set, the adapter blocks without replacing it; `AssetReservationStore.reserve()` only safely reuses the same order's existing assets and does not release stale ones. The new method has no Steam/network or trade-delivery dependency, changes no schema, and is not called from the application/UI.

## Test-first implementation steps

1. Add integration tests using real `CardCopy`, `RecipientBadge`, `SharedGameSets` selection, preflight, and temporary `AssetReservationStore`; verify tests fail because the adapter is absent.
2. Add a read-only reservation listing query (no schema change), then implement the opt-in adapter with strict bool/data checks and one final atomic reserve.
3. Test exact `Q` and ownership preservation; authoritative hash composition; infeasible `Q` with no partial reservation; prior reservations excluded; missing AppID/unverified badge, buyer, seller snapshot, or card definitions fail closed; existing single-game preflight regression tests.
4. Verify no app/UI/trade module calls the new method, run focused plus full test suite, update Stage C/Master Log/handoff, commit and push `work/local-sync`, and check Actions on the exact code SHA.

## Constraints

- Do not change `prepare()`, the approved C.2.7 rules, DB schema, migrations, UI, or Trade Offer code.
- Do not treat generic `OrderService.quantity` as Steam card sets; use the existing explicit `requested_sets` preflight parameter.
- Do not send offers, contact Steam, or use real account data.
- Keep the new adapter opt-in; if unknown AppIDs or recipient complete-set counts prevent a safe plan, return a blocked outcome rather than inferring data.
