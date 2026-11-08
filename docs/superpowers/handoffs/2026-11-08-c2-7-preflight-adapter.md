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

## Historical note: AppID 570 metadata gate (deferred)

Dota 2 (`AppID=570`) was previously selected for badge metadata work. No complete `rgCards[].markethash` fixture or connected non-production ASF session was available, and no values were inferred from Community Market listings. This record is retained as history only; Dota 2 is not the current next task.

## Current checkpoint: Steam / CS2 priority

The active priority is CS2 inventory and eventual item delivery through ASF. Existing code provides the shared catalog, inventory parser/store, and CS2 inventory read path for `(AppID=730, context=2)`; `games/cs2/__init__.py` remains a placeholder and there is no dedicated CS2 delivery handler. Defer inventory support for Dota 2, TF2, Rust, and other games. Preserve future per-game adapter extensibility without building the universal mechanism now.

**Exact next stage:** perform a read-only source-level verification of the pinned ASF Generic 6.3.11.7 API for handling one CS2 item by exact `(AppID=730, context=2, assetID)`, including the supported eligibility/tradability checks and result contract. Then scope a small CS2-specific handler that reuses the existing inventory foundation. No real offers, production accounts, ASF runtime changes, DB, migrations, UI, or trade-path changes are authorized in this checkpoint.

This checkpoint changed documentation only. No tests were necessary; the last code test results remain 36 focused tests passed. The latest successful Actions run before this documentation commit was [42481365611](https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/42481365611), on SHA `35da3fd9d2aca96db9b11cf152b1f2b0a32763c2`.

## Current handoff — Steam / CS2 inventory display

Use CS2 (`AppID=730`, `context=2`) as the only current game-inventory target. The current game panel is a per-instance card grid with search/filters; it lacks cross-bot item aggregation and table mode. `InventoryItem` and `GameInventoryStore` preserve physical inventory records and owner/asset IDs, but the game inventory UI has no per-item lock deadline or integrated reserved-state presentation. The CS2 module remains a placeholder. Do not implement Dota 2, TF2, Rust, or the future universal adapter now; preserve the option to add per-game adapters later.

Final display requirements are recorded in the C.6 addendum in the Stage C plan and Master Log: card and table views share one in-memory projection; counts aggregate selected bots while exact `(game_id, bot_name, asset_id)` ownership remains available; table columns are image/name/unique bots/total/tradable/unavailable; tradability, lock, unknown and reserved states stay distinct; deadlines are per instance and verified by a refreshed ASF snapshot; no DB aggregation or reservation by aggregate row. No implementation was started. The supplied attachment folder had no image file, so only its textual table structure could be recorded.

**Exact next task:** implement and test a small CS2-only in-memory inventory view model over existing `InventoryItem` instances, with one shared source for card and table projections. Cover aggregation across selected bots, unique-bot counts, asset deduplication scoped by owner, separation of tradeability states, and preservation of per-asset lock/owner identity. Then wire the two display modes in a separate UI task. Keep the ASF Generic 6.3.11.7 read-only capability check separate; do not change DB/migrations or trade behavior in this next data-model slice.

## Active checkpoint: C.6.1 read-only CS2 inventory verification

C.6.1 is not live-verified yet. Existing read-only path is `ASFClient.get_bot_inventory(bot_name, app_id=730, context_id=2)` via ASF IPC, called for `cs2` by `refresh_game_inventory`. The C# `gamedelivery-diagnose` path is card-focused on `753/6`, not CS2. `InventoryItem` retains asset/class/instance identity, owner, name/type, tags and tradeability flags, but has no `amount` or `tradable_after` field.

**Prepare before the live check:** run a separate isolated ASF Generic 6.3.11.7 instance with one non-production bot that owns a CS2 item. Provide only the test bot's `BotName`, the item's exact `assetID`, and confirmation that the local GameDelivery IPC connection targets that isolated instance. Keep IPC secrets/config, password, cookies, tokens and maFile local; do not send them. No public SteamID64 is needed for the bot's own inventory. Do not use production accounts, offers, or the working ASF.

Once prepared, query only AppID `730`, context `2`; confirm exact asset/class IDs, name/type, quantity, tradability, available typed fields and errors. Do not guess fields that are absent. C.6.2's view model remains downstream of this source check and must not be wired to production UI before its tests and boundary checks. No live query, code change, DB/UI change, plugin change or offer occurred in this checkpoint.
