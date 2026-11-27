# C.3 — Atomic PAID Deal → Order → initial reservations (Codex handoff)

## Scope

Complete the remaining Stage C.3 acceptance requirement: creation of a Playerok PAID Order, its immutable OrderLine manifest, ListingStock transfers, DeliveryLeg rows, canonical reservations, and the final `reserved` state must be one SQLite `BEGIN IMMEDIATE` transaction.

Work only on `work/local-sync`. Do not touch `main` or merge PR #4. Read `PROJECT_MASTER_LOG.md` and `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md` before editing. No live Playerok mutations, Steam offers, or ASF sends.

## Verified starting point

- Branch HEAD at handoff creation: `16ad554780f4c7702c3f2b3d6c332ead614d2960`.
- `PlayerokDealSyncService._maybe_create_order()` currently inserts `gd_orders` during PAID sync. For legacy-compatible single Recipe it can also materialize OrderLines there. Typed Recipe OrderLines are deferred.
- `bind_paid_deal_config_version()` persists the explicit audited ConfigVersion binding and calls `_maybe_create_order()` in the same transaction. Do not remove the manual binding/audit semantics.
- `DeliveryOrderPipeline.prepare_recipe_order()` currently accepts only `order_id`, calls `_config_for_order(order_id)`, then performs pure planning and calls `reserve_recipe_plan()`.
- `reserve_recipe_plan()` already owns a `BEGIN IMMEDIATE` transaction for typed manifest materialization, ListingStock transfer, DeliveryLegs, canonical reservations, Steam Cards listing quota claims, and `reserved` status. It currently requires a pre-existing Order.
- The Stage C plan explicitly requires Order creation, manifest, and initial reservations in one transaction (acceptance paragraph around line 320).
- This checkout was inspected through GitHub source access only. No local Python/SQLite test runtime was available in this execution, so no production patch or test result is claimed.

## Required target lifecycle

1. **PAID sync phase — no Order writes**
   - Continue upserting canonical Deal/event data and preserving the exact audited ConfigVersion binding.
   - Stop inserting `gd_orders` from `_maybe_create_order()`.
   - Keep unresolved PAID deals without exact ConfigVersion binding orderless.
   - Preserve baseline, historical-deal, listing-resolution, duplicate-event and unique-deal semantics.
   - Do not silently choose latest ConfigVersion.

2. **Planning input — no ledger writes**
   - Add an explicit immutable preparation context built from the canonical PAID Deal plus the exact `playerok_paid_config_bindings` row / matching `gd_deals.config_version_id`.
   - Derive the stable future `order_id` using the existing Playerok stable-ID rule.
   - Verify listing ownership, ConfigVersion ownership, PAID/non-historical state, baseline completion and binding audit.
   - Refactor typed Recipe planning so it can consume this context without calling `_config_for_order()` or requiring a persisted `gd_orders` row.
   - Keep ASF snapshots and all buyer/seller/card metadata preflight outside the write transaction. Planning must remain pure with respect to the delivery ledger.

3. **Successful commit — one transaction**
   - Extend the reservation commit entry point (or introduce a clearly named PAID-Recipe commit entry point) to accept the immutable Deal/ConfigVersion context and the already-computed plan.
   - Open one `BEGIN IMMEDIATE`.
   - Re-read and validate the Deal is still PAID, non-historical, baseline-complete, and has the same exact listing/config binding and audit row used during planning.
   - Re-check idempotency by deal/order unique keys. A retry of an already committed order must return the persisted result without duplicating lines, transfers, legs, quota claims, or reservations.
   - Insert the stable `gd_orders` row in this transaction.
   - Materialize the complete immutable OrderLine manifest in this transaction.
   - Transfer exact ListingStock units to the Order in this transaction.
   - Insert DeliveryLegs and canonical reservations in this transaction.
   - Claim Steam Cards listing quota in this same transaction for typed Recipe SteamCardSetSpec items, preserving the current complete-set semantics.
   - Set Order status to `reserved` only after all required writes succeed.
   - Commit once. Any exception rolls back the Order row and every dependent write.

4. **Blocked commit — one transaction, no partial resources**
   - If pure preflight shows required quantity unavailable, or a commit-time stock/quota/conflict check fails, persist only the stable Order + complete immutable manifest + `preflight_blocked` state in a short atomic transaction.
   - No ListingStock transfer, order-owned stock allocation, DeliveryLeg, canonical reservation, or reserved quota claim may remain.
   - If a resource transfer was staged before a later conflict, rollback the entire successful-commit transaction first; then persist the blocked shell/manifest in a separate transaction.
   - Never return from an open transaction after partially staging resource writes.

5. **Compatibility boundaries**
   - Do not rewrite or route through `prepare_steam_card_order()` legacy path; preserve its complete-set and buyer/seller preflight semantics.
   - Do not change `prepare_cs2_order()` or `cs2_reservations.reserve_order()`.
   - For typed Recipe containing `SteamCardSetSpec`, preserve the existing selector/preflight and complete-set quota behavior exactly.
   - Preserve SteamAssetSpec exact asset selection, ListingStock amount accounting, bot ownership and no-duplicate-asset invariants.
   - Preserve ASF transport and manual-review boundary. No live offers.
   - Preserve explicit ConfigVersion operator binding and audit trail. Do not replace it with a latest-version fallback.

## Required regression tests

Add focused tests alongside existing Playerok sync and unified Recipe reservation tests:

- PAID sync creates/updates the Deal and binding but creates no `gd_orders`, OrderLines, DeliveryLegs, transfers or reservations.
- Explicit ConfigVersion binding still creates no Order; it only fixes the audited binding (if the chosen API retains a separate explicit preparation trigger, test that contract consistently).
- Successful PAID Recipe commit atomically creates exactly one Order, complete manifest, exact ListingStock transfers, expected DeliveryLegs and canonical reservations.
- Fault injection immediately after the Order INSERT proves rollback removes the Order and all dependent writes.
- Fault injection after manifest / after ListingStock transfer / during DeliveryLeg or reservation INSERT proves no partial writes survive.
- Insufficient mandatory inventory persists Order + complete manifest + `preflight_blocked`, with zero transfers, legs, canonical reservations and reserved quota claims.
- Commit-time ListingStock shortage, Steam Cards quota shortage and resource conflict all leave the same clean blocked state.
- Duplicate PAID sync event and concurrent retry for the same Deal are idempotent and produce one Order only.
- Change Deal status away from PAID, alter the exact config binding, or invalidate the binding between planning and commit; commit must fail closed and must not reserve anything.
- Re-run existing typed Recipe, ListingStock handoff/rollback, Steam Cards, CS2, Playerok ConfigVersion binding and SQLite migration tests. Do not weaken existing assertions to make the new flow pass.

## Implementation sequence for Codex

1. Inspect the exact current call graph and schema/migrations; identify every caller of `_maybe_create_order()`, `prepare_recipe_order()`, and `reserve_recipe_plan()`.
2. Design the smallest backward-compatible API split: keep existing order-based preparation for existing non-Playerok callers, add a PAID Deal context path for Playerok typed Recipe.
3. Implement no-Order PAID sync and the new pure planning input.
4. Implement atomic successful and blocked commit paths.
5. Add fault-injection and race/idempotency tests before broad regression runs.
6. Run focused tests first, then the full required suite once; do not run redundant diagnostics.
7. Inspect the final diff for accidental changes to legacy Steam Cards, CS2, ASF, UI and licensing.
8. Update `PROJECT_MASTER_LOG.md` with exact commit SHA, exact test commands/results and CI run IDs. Keep C.3 OPEN until all acceptance checks and CI pass.

## Completion criteria

Do not claim C.3 complete until the transaction tests, compatibility regressions and CI all pass on the final HEAD. Do not merge PR #4 or change `main`.
