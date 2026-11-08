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
