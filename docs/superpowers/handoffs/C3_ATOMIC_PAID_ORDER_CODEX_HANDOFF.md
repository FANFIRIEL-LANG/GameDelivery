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

## Implementation checkpoint — 2026-11-27

The typed/legacy-Recipe atomic path described above is partially implemented on `work/local-sync` (working tree based on HEAD `bd1c28dd2f196e840fc8ea3180aebf8f63326eb0`; no commit/push). PAID sync and explicit ConfigVersion binding no longer create Order shells. `PlayerokPaidPreparation` freezes and commit-time revalidates the account/deal/listing/config/audit binding. `prepare_paid_recipe_deal()` commits a supported Recipe Order, full manifest, ListingStock transfer, DeliveryLegs, and canonical reservations atomically; blocked preflight commits only Order+manifest+blocked status. A fault injected during DeliveryLeg insert rolls back the new Order and all earlier writes. Targeted 87-test suite and compileall/diff-check passed.

C.3 remains open. Legacy selector configurations that cannot be represented as a Recipe currently fail closed before creating an Order; their previous CS2/Steam Cards paths still need transaction-aware integration while preserving selector behavior, Steam Cards quota, and manual review. Add a ConfigVersion-change-after-plan test and a UI binding→Import regression, then run required CI. No application, user DB, Playerok, ASF, or Steam operations were used.

## Follow-up verification — 2026-11-27

The compatibility note above is narrowed by code inspection: `legacy_config_to_recipe()` supports recognized legacy CS2 wrappers, legacy Steam Cards selectors, and single unambiguous legacy asset/card forms. Those shapes enter the same atomic PAID Recipe path. Unknown or ambiguous selector shapes remain fail-closed; do not route them through old order-based reservation APIs as a workaround. `prepare_steam_card_order()` and `prepare_cs2_order()` implementations were not changed.

Additional transaction coverage was added in `tests/test_playerok_listing_stock_handoff.py` and the operator Import signal assertion in `tests/test_playerok_browser_and_listing_config.py`:

- failure after deterministic Order insert but before manifest completion;
- failure after manifest but before ListingStock transfer;
- failure after transfer but before DeliveryLeg;
- canonical reservation insert failure;
- commit-time stock depletion and external reservation conflict;
- Steam Cards quota conflict after successful planning, with no partial claim/reservation;
- successful PAID Steam Cards complete-set + quota commit;
- Deal status changes to SENT between pure planning and `BEGIN IMMEDIATE` commit;
- retry/concurrent preparation and operator bind-to-import affordance.

Verification: `python -m unittest tests.test_playerok_listing_stock_handoff tests.test_playerok_browser_and_listing_config tests.test_playerok_sync tests.test_recipe_unified_reservations tests.test_trade_delivery_ui tests.test_marketplace_integrations` — **143 passed**; targeted `compileall` — PASS; `git diff --check` — PASS. CI has not run for the resulting changes yet.

Remaining before C.3 can be marked DONE: commit this reviewed logical block without disturbing other local changes; push only after confirming no upstream-only commits; run Tests and Windows Build workflows on the exact pushed SHA; inspect final workflow results; run any required migration/schema regression not included in the Tests workflow. Marketplace Import currently supplies no live inventory/card metadata, so physical preparations without preallocated ListingStock remain blocked until an operator flow supplies fresh verified inputs. No external Playerok/ASF/Steam operations or user SQLite were used.

## C.3 accepted — 2026-11-27

The above remaining steps are complete for supported typed Recipe and recognized unambiguous legacy Recipe paths. Commit `1c4aed2f18d6a2959a67ff0f63aa1d2af5efba70` is on `origin/work/local-sync`; local and remote refs matched. Exact-SHA GitHub Actions: Tests `45983457580` success, Windows Build `45983457516` success (including packaged smoke), ASF plugin build `45983457508` success. Local focused lifecycle suite 143 PASS, migration/ledger/reservation suite 29 PASS, full discovery suite 1088 PASS, compileall and diff-check PASS.

Acceptance evidence includes orderless PAID sync/binding, pure planning, immutable context revalidation under `BEGIN IMMEDIATE`, atomic Order/manifest/ListingStock/DeliveryLeg/canonical reservation/Steam Cards quota commit, blocked manifests with no partial resources, rollback injection at OrderLine/stock transfer/DeliveryLeg/reservation boundaries, shortages/conflicts, duplicate/concurrent retry, and operator binding-to-Import signal. The historical controlled migration on the user DB is separately documented in the 2026-11-18 entry of `PLAYEROK_SAFE_SYNC_HANDOFF.md`; this block did not reopen the database.

Remaining compatibility is explicit: unrecognized/ambiguous legacy selector formats and non-Recipe manual service configs remain fail-closed; the operator Import caller currently does not obtain live ASF or buyer-card metadata, so unstocked orders remain safely blocked until those providers are connected in C.4/C.5. These are outside this atomic ledger block and are tracked under later Stage C work. No Playerok mutation, Steam offer or ASF send was performed.
