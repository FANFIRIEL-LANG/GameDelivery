# C.2.7 Multi-Game Preflight Adapter Handoff

## Result

Added `SteamCardOrderPreflight.prepare_multi_game()` as an opt-in method. The existing single-game `prepare()` method is unchanged, and the application has no call site for the new method. It plans the full exact quantity first, then makes one atomic local reservation call. It never sends a Steam offer or contacts Steam.

Code checkpoint: `18821187f526d5dadc69ffc99eddf578b506f696` (`feat(steam-cards): add isolated multi-game preflight`), pushed to `origin/work/local-sync`.

## Data-source map

| Value | Current source | Adapter behavior / limitation |
|---|---|---|
| `Q` | `requested_sets` at the preflight boundary | Used directly. Generic `OrderService.quantity` is not assumed to mean card sets. |
| AppID | `games.catalog.GAME_CATALOG` | Catalog is partial; unsupported game names fail closed. `CardCopy`, `RecipientBadge`, `SharedGameSets`, and the generic order row do not carry AppID. |
| `R_g` | `evaluate_recipient_badge_capacity()` plus `buyer_complete_sets_by_app_id` | Computes `max(0, 5 - badge level - buyer complete sets)`. Buyer counts are mandatory, verified, non-negative integers; missing data never defaults to zero. |
| `A_g` | Seller `CardCopy` inventories aggregated by `find_shared_game_sets()` | Counts only a caller-verified complete list of exact `MarketHashName` values; excludes nontradable/foil cards and assets reserved by other orders. Seller snapshot freshness must be confirmed by the caller. |
| Asset mapping | `SharedGameSets.sets` | Converts exact shared `(bot_name, asset_id)` pairs into existing `OwnedSet`/`OwnedAsset` structures. |

`find_shared_game_sets()` gained an optional authoritative hash-list input. Calls that omit it preserve their prior behavior. With the new input, extra market hashes are ignored and a missing expected hash prevents a false complete set.

## Safety and scope

- Infeasible exact `Q` returns a blocked result before any reservation call; tests assert no selected sets or reservations are created.
- Other orders' reservations are removed from `A_g` before allocation. A final atomic reserve handles races; a conflict creates no partial reservation.
- If the same order already has a different asset set reserved, the method blocks without replacing or expanding the old reservation.
- Buyer inventory, seller inventory, badge data, and card definitions require actual boolean verification flags. Invalid flags or missing data fail closed.
- No SQLite schema or migration changes; one read-only reservation listing method was added.
- No UI, app caller, ASF plugin, Trade Offer code, Steam account, or production data was touched.
- Graphify was attempted; it returned no workspaces selected for this agent, so local source and test references were inspected instead.

## Verification

- Focused adapter, existing single-game preflight, shared-set, and C.2.7 tests: 34 passed.
- Full Python discovery: 242 tests, 13 environment errors. Nine test modules could not import because Python 3.10 lacks PySide6; four tests hit Windows temporary SQLite cleanup locks.
- GitHub Actions `Tests` run [42474476929](https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/42474476929), job `test`: success on exact SHA `18821187f526d5dadc69ffc99eddf578b506f696`.
- No application modules call `prepare_multi_game()`; current references are the definition and integration tests.

## Remaining integration prerequisites

This adapter is not ready to be called from the application. Before wiring a caller, supply trusted complete sources for every candidate game: AppID catalog coverage, full exact card `MarketHashName` lists, verified recipient badge progress, recipient complete-set counts, and fresh bot inventory snapshots. C.2.8's live Steam response/identity/privacy checks remain a separate gate. Keep the adapter opt-in until those sources are connected and verified; do not add Trade Offer calls.

## Files

- `games/steam_cards/order_preflight.py`
- `games/steam_cards/reservations.py`
- `games/steam_cards/shared_sets.py`
- `tests/test_multi_game_order_preflight.py`
- `tests/test_shared_steam_card_sets.py`
- `docs/superpowers/plans/2026-11-08-c2-7-preflight-adapter.md`

## Follow-up: C.2.7 planner regression validation

Two test-only commits extend coverage without changing the planner or application flow:

- `8d1bb7ee91951ddd9076a72ec8d80798ff646147` adds exhaustive differential checks over 10,368 small capacity/Q combinations. `Tests` run [42475812299](https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/42475812299) passed.
- `2820c617bd5e1cc081b59705c30c415cd61a48de` covers invalid AppID and capacity inputs. `Tests` run [42476082350](https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/42476082350) passed.

Final focused results: allocator 9 passed; multi-game preflight 9; existing single-game preflight 7; shared card sets 11. No production code, DB, UI, ASF, Steam or trade behavior changed. Full discovery was not rerun; its last known result remains 242 tests with 13 environment errors noted in `PROJECT_MASTER_LOG.md`. A targeted boundary review confirmed `find_shared_game_sets()` rejects empty, invalid, and duplicate expected hash lists before counting sets.

## Exact next independent task

Obtain a confirmed complete `rgCards[].markethash` list for the first selected AppID, then add it to a source-backed, read-only catalog with provenance and tests for missing or duplicate hashes. Steam Community Market search lists current listings but does not establish a complete badge set. Keep `prepare_multi_game()` opt-in until all verified upstream inputs are available. The separate C.2.8 live gate still requires a dedicated test profile; do not use production accounts.

## AppID 570 metadata gate

Selected catalog target: Dota 2 (`AppID=570`). No complete badge metadata fixture or connected non-production ASF session is available in this workspace. Community Market search listings do not establish the full `rgCards[].markethash` set, so no values were guessed and no catalog entry was created. No profile, production account, secrets, inventory, or trade operations were used.

**Exact next task:** run the existing read-only C.2.8 badge diagnostic for AppID `570` with a dedicated non-production ASF test bot/profile; capture the complete `rgCards[].markethash` array; verify the exact, unique mapping to `MarketHashName`; then add a source-backed catalog entry and tests. Keep `prepare_multi_game()` opt-in and leave Trade Offer flow untouched.

No code tests were run for this blocked metadata acquisition because it produced no code/catalog changes. The latest successful Actions before this documentation update was run [42476489176](https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/42476489176) on SHA `b7adaed91445728ac2374b58758b9a1185505242`.
