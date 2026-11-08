# C.2.7 Multi-Game Preflight Adapter Handoff

> **Актуальный checkpoint:** C.6.1 live GET подтверждён по отчёту пользователя; старые статусы «ожидает live» ниже — исторические. Последний раздел этого файла содержит текущий C.6.2 handoff.

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

## Historical handoff — Steam / CS2 inventory display (superseded by the final C.6.1 update)

Use CS2 (`AppID=730`, `context=2`) as the only current game-inventory target. The current game panel is a per-instance card grid with search/filters; it lacks cross-bot item aggregation and table mode. `InventoryItem` and `GameInventoryStore` preserve physical inventory records and owner/asset IDs, but the game inventory UI has no per-item lock deadline or integrated reserved-state presentation. The CS2 module remains a placeholder. Do not implement Dota 2, TF2, Rust, or the future universal adapter now; preserve the option to add per-game adapters later.

Final display requirements are recorded in the C.6 addendum in the Stage C plan and Master Log: card and table views share one in-memory projection; counts aggregate selected bots while exact `(game_id, bot_name, asset_id)` ownership remains available; table columns are image/name/unique bots/total/tradable/unavailable; tradability, lock, unknown and reserved states stay distinct; deadlines are per instance and verified by a refreshed ASF snapshot; no DB aggregation or reservation by aggregate row. No implementation was started. The supplied attachment folder had no image file, so only its textual table structure could be recorded.

**Previous next task (superseded by the final C.6.1 update):** implement and test a small CS2-only in-memory inventory view model over existing `InventoryItem` instances, with one shared source for card and table projections. Cover aggregation across selected bots, unique-bot counts, asset deduplication scoped by owner, separation of tradeability states, and preservation of per-asset lock/owner identity. Then wire the two display modes in a separate UI task. Keep the ASF Generic 6.3.11.7 read-only capability check separate; do not change DB/migrations or trade behavior in this next data-model slice.

## Historical checkpoint before live result: C.6.1 read-only CS2 inventory verification

C.6.1 is not live-verified yet. Existing read-only path is `ASFClient.get_bot_inventory(bot_name, app_id=730, context_id=2)` via ASF IPC, called for `cs2` by `refresh_game_inventory`. The C# `gamedelivery-diagnose` path is card-focused on `753/6`, not CS2. `InventoryItem` retains asset/class/instance identity, owner, name/type, tags and tradeability flags, but has no `amount` or `tradable_after` field.

**Prepare before the live check:** run a separate isolated ASF Generic 6.3.11.7 instance with one non-production bot that owns a CS2 item. Provide only the test bot's `BotName`, the item's exact `assetID`, and confirmation that the local GameDelivery IPC connection targets that isolated instance. Keep IPC secrets/config, password, cookies, tokens and maFile local; do not send them. No public SteamID64 is needed for the bot's own inventory. Do not use production accounts, offers, or the working ASF.

Once prepared, query only AppID `730`, context `2`; confirm exact asset/class IDs, name/type, quantity, tradability, available typed fields and errors. Do not guess fields that are absent. C.6.2's view model remains downstream of this source check and must not be wired to production UI before its tests and boundary checks. No live query, code change, DB/UI change, plugin change or offer occurred in this checkpoint.

## C.6.1 — isolated ASF Generic preparation

**Feasibility:** ASF supports a separate home via `--path`; the project workflow pins the complete Generic 6.3.11.7 release archive checksum and .NET 10.0.x. A test home would contain only fresh `config/ASF.json`, loopback-only `config/IPC.config`, and the single dedicated test bot config; logs are generated there. Never copy production configs, bots, plugin DLLs or logs. Set a distinct loopback endpoint such as `127.0.0.1:1243` and a new IPC password kept local. See [ASF IPC](https://github.com/JustArchiNET/ArchiSteamFarm/wiki/IPC) and [`--path`](https://github.com/JustArchiNET/ArchiSteamFarm/wiki/Command-line-arguments).

**Current code blockers:** `ASFClient.base_url` is configurable but request validation only accepts loopback port `1242`; the game inventory panel also writes refreshes to `Path.home()/.gamedelivery/gamedelivery.sqlite3`. Do not use that panel or default endpoint for the test. The C# `gamedelivery-diagnose` inventory path reads `753/6`, not CS2.

**Next implementation before live read:** add tested explicit loopback alternate-port support to `ASFClient` without changing its default, plus a direct in-memory probe limited to `GET /Api/Bot/{BotName}/Inventory/730/2?language=english`. It must filter one exact assetID, emit only sanitized item fields, use no SQLite store/UI/preflight/TradeDelivery, and contain no write/offer call path. Before launching the test instance, verify exact ASF Generic 6.3.11.7 startup, isolated `--path`, listener PID/address/port, and the probe's literal test URL.

**User preparation still required:** dedicated non-production Steam account loaded as the sole bot in that test home, one CS2 inventory item, and its exact test `BotName`/`assetID`. Keep IPC secret, bot JSON, login data, Steam Guard, cookies and maFile local. No account or inventory operation was performed; no ASF process was launched. C.6.2 remains blocked until the live source fields are verified.

## Historical checkpoint: probe готов к изолированной live-проверке; пока не подключаться

Mock-only реализация находится в integrations/asf/client.py и integrations/asf/cs2_inventory_probe.py. Обычные вызовы ASFClient остаются на 1242. Для альтернативного loopback-порта нужен allow_alternate_loopback_port=True; удалённые host/IP и не-HTTP схемы по-прежнему отклоняются. Probe строже: принимает только явно переданный loopback URL на 1243, BotName и точный числовой assetID, затем выполняет один GET для AppID 730/context 2. Он выводит только выбранные поля, отсутствующие значения показывает как "unknown", берёт amount из ответа при наличии и не выдумывает tradable_after.

Probe не использует SQLite/store/UI/preflight/торговые модули. Тесты проверяют один GET, точное совпадение assetID (включая похожие ID), сообщение при отсутствии предмета, пропущенные поля и отсутствие вызовов SQLite и методов обмена. CLI запрашивает IPCPassword скрытым локальным вводом; не помещать пароль в аргументы командной строки и не отправлять его Codex.

Локально: focused-тесты ASF-клиента, probe и inventory прошли (59); compileall прошёл. Полный набор на Windows/Python 3.14 столкнулся с существующей ошибкой очистки временных SQLite-файлов (WinError 32) и не выдал итоговую сводку; тот же lock воспроизведён отдельным существующим DB-тестом. Полный тестовый набор проверяется GitHub Actions после push.

До live-доступа дождаться пользователя. Нужно подготовить и запустить только официальный ASF Generic 6.3.11.7 с новым home через --path, одним non-production ботом и одним CS2-предметом; привязать IPC к 127.0.0.1:1243; сверить версию, путь test-home и PID listener; сообщить BotName и точный assetID; явно разрешить один read-only GET. Не запрашивать IPCPassword и иные секреты, не запускать команду до подтверждений.

Команда после разрешения: python -m integrations.asf.cs2_inventory_probe --ipc-url http://127.0.0.1:1243 --bot-name "<BotName>" --asset-id "<assetID>". CLI локально запросит IPCPassword без отображения ввода. Probe не читает и не записывает SQLite и не связан с production delivery. Не начинать C.6.2 до подтверждения результата live C.6.1.

## Актуальный handoff: C.6.1 live подтверждён; подготовка C.6.2

**Источник live результата:** отчёт пользователя от 2026-11-08, не независимый запуск этого агента. По отчёту использовался ASF Generic 6.3.11.7 из `D:\ASF-CS2-TEST\ASF`, отдельный ASF home `D:\ASF-CS2-TEST\Home`, IPC `http://127.0.0.1:1243`, non-production BotName `Ewaylariell`. Пользователь сообщает, что рабочий ASF не затрагивался. Выполнен один read-only GET `/Api/Bot/Ewaylariell/Inventory/730/2?language=english`; указаны 5 CS2 items и asset IDs в Master Log. Probe для MAC-10 | Snow Splash (`54333035531`) вернул class `7993072355`, instance `302028390`, amount `1`, name/type/market hash, `tradable=true`, `marketable=true`, `tradable_after=unknown`. Полного raw response нет; не заявлять о получении всех возможных metadata. В выводе probe нет отдельного поля asset-level `contextid`, только путь 730/2.

**Контракт ASF 6.3.11.7:** источник ASF разделяет инвентарь на `Assets` и `Descriptions`, передавая AppID/ContextID заданного GET дальше в `Actions.GetInventory`. Закреплённый SteamKit2 3.5.0 тип `CEcon_Asset` имеет `appid`, `contextid`, `assetid`, `classid`, `instanceid`, `amount`. `CEconItem_Description` содержит имя/тип/market hash, `tradable`, `marketable`, целочисленные `market_tradable_restriction` и `market_marketable_restriction`, `item_expiration` и descriptions, но не typed `tradable_after`. Текущий `InventoryItem`/store не сохраняет amount, context или restriction metadata. Пока не менять БД.

**Ограничения остаются раздельными:** Steam Trade Protection относится к CS2 items, полученным через обмен, и длится 7 дней после подтверждения; Market Purchase Cooldown показывает дату доступности у конкретного предмета, без общего фиксированного срока. Не выводить permanent restriction из `tradable=false`; не вычислять deadline из даты первого обнаружения. Неподтверждённая причина/date остаётся `unknown` и недоступной для резерва.

**730/16:** проверенные первичные Steam/ASF источники не определяют context mapping. Есть неофициальный сторонний lead о `730/16` для Trade Protected предметов; это не проверенное правило и не доказывает покрытие всех блокировок. Живой запрос проверял только 730/2. Не сливать контексты автоматически. Если следующая реализация реально требует этот случай, потребуется известный protected non-production asset, чтобы отдельно проверить 730/2 и 730/16.

**Цены:** Steamworks документирует RUB как currency `5` для своих API, но не для недокументированного Steam Community Market `/market/priceoverview/`. `country=RU` (ISO alpha-2 для выбранного рынка) + `currency=5` и точный `market_hash_name` остаются кандидатной комбинацией до разрешённой безопасной проверки endpoint. Сторонние описания указывают `lowest_price`, `median_price`, `volume` (необязательное), а также HTTP/JSON ошибки; официальный контракт отсутствует. Они также приводят разные/неофициальные оценки частоты; один справочник пишет примерно 20 запросов в минуту и 1 000 в день, это не лимит Valve и не значение для жёсткой настройки. Будущий клиент должен отличать transport/HTTP/JSON/`success=false`, не считать их ценой `0`, уважать `Retry-After` если он есть, делать ограниченный backoff на 429/временные ошибки и не затирать последний успешный ответ ошибкой. Steam Subscriber Agreement §4.C широко ограничивает автоматизированные скрипты/боты при взаимодействии со Steam, поэтому не реализовывать/не запускать collector до подтверждения разрешённого способа доступа. В этом checkpoint priceoverview не вызывался.

**Точный следующий шаг — подготовка C.6.2, без реализации:** сопоставить предварительный status contract с существующими `InventoryItem`, parser/store и планируемой общей view model; уточнить типы, nullable/unknown значения и переходы состояния. Затем зафиксировать короткий C.6.2 план с in-memory CS2 моделью и mock acceptance cases, сохранив `amount`, независимые `tradable`/`marketable`, per-asset identity и `last_inventory_refresh`; `tradable_after` разрешён только из проверенного точного источника. Причины `none`, `market_purchase_cooldown`, `trade_protection`, `permanently_non_tradable`, `unknown` пока предварительные и требуют сверки с кодом/доказуемыми источниками. `unknown` остаётся нерезервируемым. До этой сверки код не писать; пока не менять SQLite/migrations, UI, ASF plugin/runtime, production preflight или торговый путь. Не объединять 730/16 автоматически и не автоматизировать Market pricing до проверки разрешённого способа доступа.

**Действия пользователя сейчас:** не требуются. Новый live тест не запрашивать сейчас; если понадобится, сначала объяснить конкретное неизвестное и почему его нельзя закрыть ASF/Steam source audit.

Источники: [ASF 6.3.11.7 BotInventoryResponse](https://github.com/JustArchiNET/ArchiSteamFarm/blob/6.3.11.7/ArchiSteamFarm/IPC/Responses/BotInventoryResponse.cs), [SteamKit2 3.5.0 types](https://github.com/SteamRE/SteamKit/blob/3.5.0/SteamKit2/SteamKit2/Base/Generated/SteamMsgEcon.cs), [Steam Trade Protection](https://help.steampowered.com/en/faqs/view/365F-4BEE-2AE2-7BDD), [Steam Trading and Market Restrictions](https://help.steampowered.com/en/faqs/view/451E-96B3-D194-50FC), [Steamworks ECurrency](https://partner.steamgames.com/doc/store/pricing/currencies?l=english&language=english), [Steam Subscriber Agreement §4.C](https://store.steampowered.com/subscriber_agreement/), [стороннее описание context 16](https://dashboard.steamlabs.dev/docs/api/en/endpoints/inventory), [неофициальное описание priceoverview](https://cs2.sh/resources/how-to-scrape-steam-market-priceoverview).
