# GameDelivery C.6.3.6 — OpenSkin implementation handoff

Дата: 2026-11-09  
Рабочая папка: `D:\GameDelivery`  
Ветка: `work/local-sync`  
HEAD: `29b36dd1d44245f933346947a1ee9cb85ef6588d`  
Commit/push: не выполнялись.  
Статус этапа: реализация и mock/focused verification готовы; live-проверка на изолированном ASF pending. Commit/push не выполнялись.

## Историческая запись первичного исследования

Следующие сведения фиксируют состояние до согласования и начала реализации. Текущий статус, изменения и тесты приведены в заключительном разделе этого файла.

## Обязательная сохранность

До C.6.3.6 дерево было dirty из-за предыдущей работы C.6.3.1–C.6.3.5/test-mode. Существующие изменения сохранены; reset/clean/stash/checkout не выполнялись. В C.6.3.6 не менялись Python-код, тесты, SQLite и настройки приложения. Изменены только `PROJECT_MASTER_LOG.md` и `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`; добавлен этот handoff.

Текущее состояние Git до исследования подтверждено:

- modified: `PROJECT_MASTER_LOG.md`, `app/asf_management.py`, `app/game_inventory_cards.py`, `app/game_inventory_panel.py`, `app/inventory_panel.py`, `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`, `games/cs2/inventory_view.py`, `games/steam_cards/inventory_views.py`, `integrations/asf/client.py`, `tests/test_cs2_inventory_view.py`, `tests/test_inventory_views.py`;
- untracked: `app/inventory_display.py`, `docs/superpowers/checkpoints/` (C.6.3.4/.5 handoffs и этот файл), `games/inventory_display.py`, `tests/test_cs2_inventory_panel.py`, `tests/test_inventory_display.py`, `tests/test_inventory_panel_display.py`.

## OpenSkin: подтверждено по официальной документации 2026-11-09

Источники:

- [OpenSkin API/v1 Pricing](https://openskin.dev/)
- [OpenSkin OpenAPI JSON](https://openskin.dev/openapi.json)
- [OpenSkin Platform/API docs](https://openskin.dev/docs/platform)

Исследование OpenAPI было read-only GET к официальному OpenAPI документу; реальные цены по API не запрашивались.

- Публичный Pricing API v1 в документации по-прежнему бесплатен, не требует API key; OpenSkin указывает, что коммерческое использование разрешено и фиксированного лимита запросов сейчас нет.
- Platform API v2 использует отдельный API key и pricing plan. Не подменять v1 на v2.
- Используемый host должен быть только `https://api.openskin.dev`; endpoint для запросов: `POST /v1/prices/batch`.
- Request `items` — точные market hash names или `{ "item": ..., "pattern": ... }`, максимум 500.
- Ответ `results` имеет одну запись в порядке запроса; у одинакового имени с разными patterns результаты различимы. `data` — map по item name и перезаписывает дубли, поэтому не годится для безопасного сопоставления pattern вариантов.
- `steam.ask` — nullable JSON number в USD; `steam.updated_at` — время обновления именно Steam quote. Top-level `updated_at` не использовать вместо него. Steam order book обновляется приблизительно раз в 6 часов; median/volume обновляются ежедневно и в оценку не входят.
- Для exact money parsing применять JSON `parse_float=Decimal` или эквивалент, сохранять в SQLite десятичную строку. Null/missing/invalid/non-positive ask либо не-USD currency означает unknown, никогда 0 и не currency conversion.
- Pattern request документирован для Doppler/Case Hardened; request/result echo позволяет проверить позицию. Не отправлять угаданный pattern и не использовать generic quote для pattern-sensitive item без точной идентичности.

OpenSkin заявляет разрешённое коммерческое использование собственного API. Это не отдельное доказательство лицензии OpenSkin на upstream Steam market data; этот источник риска не исследовался как юридическое заключение.

## Текущее состояние GameDelivery

- `games/catalog.py`: CS2 — AppID 730/context 2.
- `GameInventoryPanel.fetch_inventory()` выполняет один ASF GET 730/2; один response идёт в `parse_game_inventory()` и CS2 in-memory adapter.
- `InventoryItem`/`GameInventoryStore` сохраняют запись на физический asset `(game_id, bot_name, asset_id)`, name/type/market_hash_name, tradable/marketable и quality/rarity/exterior/tags. Snapshot не содержит ASF amount/context/pattern.
- `CS2InventoryInstance` сохраняет source AppID/context, owner/asset, amount, flags/restrictions/tags и market hash, но у неё нет typed pattern, paint seed или float/wear number. Категории тегов после адаптации не сохраняются как типизированные данные.
- В UI уже есть заглушки цены в `app/game_inventory_cards.py` и price column `app/game_inventory_panel.py`. Общие `app/inventory_display.py`, `games/inventory_display.py`, Steam Cards и другие игры используют общую заглушку; не менять их для CS2 цены.
- Нынешний grouping selector: «Общий вид» и «Раздельно по ботам». В `separate` ветке CS2 повторно строится projection на бот, но одинаковые assets этого бота продолжают группироваться. Это не режим «каждый asset отдельно» из спецификации.
- Текущая реализация не имеет OpenSkin client/price cache.

## Предложенная схема (ещё не реализована)

### Ключ и запрос

Одна ценовая позиция: `(appid=730, exact market_hash_name, exact pattern-or-null)`. Owner и asset ID не входят в ключ. Для выбранных ботов сначала отфильтровать физические inventory rows, затем собрать уникальные поддерживаемые ключи. Отправлять только item/pattern, без BotName, Steam ID, asset ID, account metadata или credentials. Chunk size ≤500; только один in-flight batch; сопоставлять `results` по request order и валидировать echoed item/pattern.

Known ordinary MarketHashName без pattern-sensitive modifiers можно запросить как bare string. Если item требует Doppler phase/Case Hardened seed или иной exact variant, но ASF/in-memory source не предоставляет typed variant value, классифицировать цену unsupported/unknown. Не передавать generic MarketHashName как замену точной pattern quote. Результат OpenSkin не используется для изменения ASF item identity/grouping.

### Cache

Предлагаем одну новую таблицу `cs2_price_cache` в существующей пользовательской DB GameInventoryPanel (`~/.gamedelivery/gamedelivery.sqlite3`). Composite key: appid, source (`openskin:steam`), exact market_hash_name, pattern (empty key for none). Поля: `steam_ask TEXT`, currency, `source_updated_at`, local `fetched_at`, last valid price fetch, last attempt, availability/status and safe HTTP error code. Keep old valid quote on network/API error, mark stale/error; do not convert missing price to zero. A successful no-price response must display unknown; a retained older quote may only appear explicitly as stale. No raw response body/error text in logs.

В коде текущего game inventory store нет общей versioned migration framework: его tables создаются собственным module via `CREATE TABLE IF NOT EXISTS`. Central `delivery_orders` migration in `app/main.py` is unrelated to `GameInventoryPanel`; `core.database.Database` is orders service. Proposal is an additive, idempotent table created by isolated `games/cs2/price_store.py`, without changing snapshot/card/order tables. Any DB table creation waits for approval.

### Freshness, error handling, valuation

- Every 10 minutes a local timer re-evaluates freshness. Do not request a new Steam batch until the documented ~6-hour source interval permits it (unless no value has ever been fetched). Distinguish source item `steam.updated_at` from `fetched_at` and preserve each exactly.
- On API error retain last good quote as stale, do not overwrite with zero. Respect HTTP 429 Retry-After and use a serialized queue/backoff. Manual refresh only queues positions whose source/cache state is due.
- Calculate with `Decimal` and number of physical asset rows (`len(instances)`), never ASF `amount`. Include non-tradable and unknown tradability. Unknown-price rows count separately and do not add zero into a claimed complete total. Summary separates fresh and stale known value and shows unknown counts.
- Per-bot total adds each selected physical asset for that bot; overall selected total is the sum across selected bots. Same price key fetched once, then applied to each asset/owner.

### UI

- Only CS2 branch in `GameInventoryPanel` and `CS2InventoryGroupCard` gets price data. Card shows Steam Ask per item and, in shared mode, group total by physical item count. Table uses existing price column; summary in both card/table modes shows per-bot and selected totals, fresh/stale status, number of priced/unpriced physical assets, and source/local timestamps.
- To meet required per-Asset mode, CS2-only `separate` mode should render one physical asset per card/table row and change its label while CS2 is selected. Other games keep current per-bot grouping behavior. Do not change Steam Trading Cards or common inventory display.
- OpenSkin I/O should be asynchronous and separate from ASF inventory refresh. Existing Qt network usage is for images only; no general API price service exists.

## Предварительный список файлов после согласования

**Новые:**

- `integrations/openskin/__init__.py`
- `integrations/openskin/client.py`
- `games/cs2/pricing.py`
- `games/cs2/price_store.py`
- `tests/test_openskin_client.py`
- `tests/test_cs2_pricing.py`
- `tests/test_cs2_price_store.py`
- `tests/test_cs2_price_panel.py`

**Изменяемые:**

- `app/game_inventory_panel.py`
- `app/game_inventory_cards.py`
- `PROJECT_MASTER_LOG.md`
- `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`
- этот handoff после implementation/test results

Существующие C.6.3.5 tests по умолчанию запускаются как регрессия; изменять их только при доказанной необходимости.

**Вне scope:** `games/inventory.py` parser/InventoryItem/GameInventoryStore и snapshot schema; `app/inventory_panel.py`; shared inventory display; Steam Cards; other game adapters; ASF plugin/DLL; delivery/preflight/trade; real Steam/ASF Trade Offers.

## Test plan после согласования

1. OpenSkin mock: exact host/v1 path, no key, HTTPS, request body, 500 ceiling, ordered results, duplicate MarketHashName with separate patterns, echo mismatch/missing result, HTTP 400/429/timeout.
2. Price parser: only `steam.ask`, Decimal preserving source number, null/invalid/negative/non-USD => unknown, no median/bid/provider fallback, source `steam.updated_at` distinct from local fetched time.
3. Price-key/projection: one query across bots per exact item variant; patterns are separate; missing special pattern unsupported; selected bot filtering; physical count vs ASF amount; non-tradable included.
4. SQLite: temp DB only, idempotent new table init, persisted/reloaded exact Decimal text, existing tables untouched, last good retained on transient failure, no secret/raw body persistence.
5. Calculation/UI: shared group unit+group total, per-Asset rows/cards, per-bot/selected totals, fresh/stale/unknown labels, selection change recalculation, timer, async UI responsiveness.
6. Re-run focused existing CS2 view/panel tests and targeted non-CS2/Steam Card display regression only where touched; do not run full suite by default. No live OpenSkin query needed for these tests.

## Дальнейшее действие

На этой сессии не запускались тесты (код не менялся), не выполнялся реальный OpenSkin price request, не менялись пользовательские данные, SQLite, ASF, Steam account or API configuration. Existing C.6.3.5 test results remain those in its handoff; not rerun. No download/manual application verification is needed now.

**Следующий шаг:** пользователь проверяет этот план и явно подтверждает реализацию. После подтверждения начать с тестов контракта API и ключа/проекции, затем реализовать клиент и кэш, после чего подключить только CS2 UI и зафиксировать проверенные результаты в журналах и handoff. Если для OpenSkin v1 появится платная подписка или API key, сверить требование только по документации v1 и остановиться; не переключаться на Platform v2. Для pattern-вариантов без точных данных оставить цену неизвестной, ничего не угадывать.

---

## C.6.3.6 — implementation checkpoint (9 ноября 2026)

**Состояние:** реализация и mock/focused verification выполнены. Live-проверка на изолированном ASF и действительных ценах/курсе обязательна; до неё этап не считать полностью завершённым. Commit/push не выполнялись.

- Репозиторий: `D:\GameDelivery`; branch `work/local-sync`; HEAD `29b36dd1d44245f933346947a1ee9cb85ef6588d`.
- Все предыдущие незакоммиченные C.6.3.1–C.6.3.5/test-mode изменения сохранены; reset/clean/stash/checkout не выполнялись.
- Новые файлы: `integrations/openskin/__init__.py`, `integrations/openskin/client.py`, `integrations/frankfurter/__init__.py`, `integrations/frankfurter/client.py`, `games/cs2/pricing.py`, `games/cs2/price_store.py`, `tests/test_openskin_client.py`, `tests/test_frankfurter_client.py`, `tests/test_cs2_pricing.py`, `tests/test_cs2_price_store.py`, `tests/test_cs2_price_panel.py`.
- Реализацией изменены `app/game_inventory_panel.py`, `app/game_inventory_cards.py`, плюс Master Log, Stage C plan и этот handoff. `games/cs2/pricing.py` дополнен conservative guard и acceptance test для seed-sensitive finishes.

### Реализовано

- OpenSkin: только `POST https://api.openskin.dev/v1/prices/batch`, batch ≤500, ordered `results`, exact item/pattern echo, только `steam.ask`. JSON quote values — Decimal. HTTP error body не сохраняется и не выводится; retry-after учитывается.
- Frankfurter: только `GET https://api.frankfurter.dev/v2/rate/usd/rub`; USD/RUB Decimal, source date отдельно от local fetched time.
- Стоимость: только физические Asset-строки, не ASF `amount`; включены непередаваемые предметы; фильтрация по выбранным ботам. `steam.ask ≤ $0.05` не конвертируется и исключается из суммы; USD цена остаётся на карточке. Только `> $0.05` конвертируется с ROUND_HALF_UP до копеек. Без курса отображается частичный итог + awaiting-rate count.
- Без typed seed/pattern generic ask не назначается Doppler, Case Hardened, Fade, Heat Treated, Crimson Web, Slaughter; state `unsupported`.
- Идемпотентные `cs2_price_cache`, `cs2_fx_rate_cache` хранят Decimal текстом, quote/rate source и fetched timestamps, состояние/error/retry и RUB conversion provenance. Временная ошибка сохраняет последнюю успешную цену/курс. QTimer 10 минут, сетевые вызовы в Qt worker.
- Shared карточки: unit price + стоимость физической группы. CS2 separate cards/table: одна строка на physical Asset. Summary в обоих режимах: per-bot и selected total; дешёвые, неизвестные, unsupported, error, awaiting-rate и stale количества.

### Результаты проверок

- `python -m unittest tests.test_openskin_client tests.test_frankfurter_client tests.test_cs2_pricing tests.test_cs2_price_store` — 35 tests, exit 0.
- `python -m unittest tests.test_cs2_price_panel` — 7 tests, exit 0.
- `python -m unittest tests.test_cs2_inventory_view tests.test_asf_inventory` — 39 tests, exit 0.
- `python -m unittest tests.test_inventory_views tests.test_inventory_display tests.test_inventory_panel_display` — 28 tests, exit 0.
- `python -m unittest tests.test_steam_card_selection` — 6 tests, exit 0.
- `python -m unittest tests.test_cs2_inventory_panel` — 28/28 assertions reported `OK`, process exit 1 without traceback; separate repeat had the same result. Treat as unresolved Qt shutdown issue, not a clean suite pass.
- `python -m unittest tests.test_steam_card_snapshot` — 7 cleanup errors; Windows `WinError 32` while deleting temporary `app.db`. Existing Steam Card store/tests were not changed.
- `python -m compileall -q integrations/openskin integrations/frankfurter games/cs2/pricing.py games/cs2/price_store.py app/game_inventory_panel.py app/game_inventory_cards.py tests/test_openskin_client.py tests/test_frankfurter_client.py tests/test_cs2_pricing.py tests/test_cs2_price_store.py tests/test_cs2_price_panel.py` — exit 0.
- `git diff --check` — exit 0 before docs were appended; rerun it after checkpoint changes.

В рамках C.6.3.6 не менялись production parser/InventoryItem/legacy GameInventoryStore/inventory snapshots, Steam Cards, другие игровые adapters, ASF plugin/DLL, delivery/preflight/trade. Проверки были mock-based; live OpenSkin/Frankfurter запросов и реального ASF UI запуска не было.

### Следующий шаг

1. Выполнить live UI verification только на изолированном ASF Generic test home/IPC, non-production bot и `730/2`. Рабочий ASF `1242` не трогать. IPCPassword вводится пользователем локально в приложение и не передаётся в чат.
2. Сверить полученные OpenSkin `steam.ask`, Frankfurter source date/rate, порог `$0.05`, цены USD дешёвых предметов, totals физической группы/бота/выбранных ботов и shared/separate отображение. Проверить выбор ботов и persistence кэша после перезапуска.
3. Не отправлять Trade Offers и не изменять Steam inventory.
4. Отдельно выяснить exit 1 процесса `tests.test_cs2_inventory_panel` и WinError 32 в `tests.test_steam_card_snapshot`; не менять Store/Steam Card tests без отдельного scope.
5. Дождаться live-результатов пользователя и представить C.6.3.6 на проверку. Commit/push не выполнять.

---

## C.6.3.6.1 — OpenSkin proxy и общий выбор ASF-ботов (2026-11-09)

**Состояние:** реализация поверх текущих незакоммиченных C.6.3.1–C.6.3.6 изменений. Ветка `work/local-sync`, HEAD `29b36dd1d44245f933346947a1ee9cb85ef6588d`. Не выполнять commit/push; они не создавались. Не очищать, не stash и не откатывать рабочую копию.

### Что сделано

- Причина OpenSkin 403 по сообщению пользователя — прямой API маршрут недоступен в его стране без VPN. Добавлена настройка OpenSkin proxy в существующую страницу прокси: enable/disable, HTTP CONNECT/SOCKS5, host, port, username/password, Save и асинхронная read-only connection check.
- Прокси задаётся только на per-request `QNetworkAccessManager` в OpenSkinClient; приложение не меняет глобальный proxy. HTTP CONNECT подходит для HTTPS API destination; отдельный `https://proxy` TLS transport не реализован/не показывается в UI. Frankfurter остаётся отдельным `FrankfurterClient()` без аргумента proxy. Другие интеграции не используют этот профиль.
- QSettings хранит только несекретные поля. Password маскируется в UI и хранится в keyring; при сохранении поле очищается, repr/safe_summary и диагностические строки не содержат пароль. Цена перечитывает настройки при каждом сетевом обновлении; перезапуск ASF не нужен.
- Поле `test_bot_name_input` было введено для изолированного C.6.3.2.1, чтобы в test-mode не показывать config-имена production ботов. Теперь оно удалено. Вкладки «Инвентарь» и «Игровые предметы» отображают общий checkbox list; обычный источник — ASF config/local snapshots, test-mode source — read-only `GET /Api/Bot/@all` с явно заданного loopback endpoint. Test-mode не читает local ASF configs и не использует default 1242. Поиск casefold, multi-select, select-all/clear, общий синхронизированный selection.
- Delivery selection, торговля, ASF plugin/DLL, парсеры и inventory schema не менялись в рамках этого подэтапа.

### Проверки

`python -m unittest tests.test_bot_selector tests.test_openskin_proxy tests.test_openskin_client tests.test_cs2_inventory_panel tests.test_inventory_panel_display tests.test_cs2_price_panel -v` — 58 passed, exit 0. `python -m unittest tests.test_cs2_pricing tests.test_cs2_price_store tests.test_inventory_views tests.test_inventory_display -v` — 49 passed, exit 0. Покрывает bot list/checks/search/selection, test ASF read-only bot listing, proxy types/store/redaction, proxy-only OpenSkin routing, отдельную конвертацию через Frankfurter, UI test/save wiring, CS2 totals/filtering path, Steam Cards display.

Не выполнялись real OpenSkin connection check и live ASF; нет подтверждения, что пользовательский proxy снимает HTTP 403 до проверки пользователем. Отдельную offline-проверку не проводили; первый focused command включил существующий mock-тест `tests.test_openskin_client` на обработку HTTP status. Не выполнялись commit/push. Следующий шаг: пользователь вводит OpenSkin proxy локально в настройках, запускает read-only connection check, затем при необходимости использует изолированный test ASF и обновляет список ботов через кнопку. Секреты в чат не отправлять.

Файлы этого продолжения: изменены `app/bot_selector.py`, `app/game_inventory_panel.py`, `app/inventory_panel.py`, `app/main.py`, `app/proxy_settings.py`, `integrations/openskin/client.py`, `tests/test_bot_selector.py`, `tests/test_cs2_inventory_panel.py`, `tests/test_inventory_panel_display.py`, `tests/test_cs2_price_panel.py`, `tests/test_openskin_proxy.py`; добавлены `app/openskin_proxy_settings.py` и `integrations/openskin/proxy.py`; обновлены Master Log, Stage C plan и этот handoff. В рабочей копии остаются и другие прежние C.6.3.1–C.6.3.6 изменения, их не очищали и не откатывали.

`python -m compileall -q` для изменённых Python-файлов — exit 0. `git diff --check` — exit 0; Git показал только предупреждения LF→CRLF для уже затронутых файлов. Текущий branch/HEAD подтверждены после проверок: `work/local-sync` / `29b36dd1d44245f933346947a1ee9cb85ef6588d`.

---

## C.6.3.6.2 — OpenSkin proxy diagnostics handoff (2026-11-09)

### Результат расследования

Подтверждённая причина неверно обобщённого сообщения «сервис недоступен»: старая кнопка «Проверить подключение» отправляла ценовой `POST /v1/prices/batch`, то есть проверяла одновременно proxy route и OpenSkin API. Если Qt не получил HTTP status, код терял `QNetworkReply.error()`/`errorString()` и выводил только общий недоступный сервис. Успешное открытие браузером `openskin.dev` не подтверждает доступ к `api.openskin.dev` или batch POST. Точное сетевое препятствие (например, auth/DNS/TLS/API denial) остаётся неизвестным без результата локального разделённого теста: действительный пользовательский proxy/пароль не читались и live network не вызывался.

### Текущее поведение

- **Проверить прокси:** один manager-local `QNetworkAccessManager`, `GET https://api.openskin.dev/`, `Range: bytes=0-0`; любой полученный HTTP status кроме 407 отображается как достигнутый HTTPS/TLS маршрут, но не гарантирует работу API.
- **Проверить OpenSkin API:** отдельный read-only ценовой `POST /v1/prices/batch`; сообщает API HTTP status или transport category.
- GET выбран вместо HEAD, чтобы probe не зависел от разрешения HTTP метода HEAD в proxy policy и был похож на обычную браузерную навигацию.
- `setProxy()` вызывается на локальном менеджере до `get/post`; `proxyAuthenticationRequired` заполняет `QAuthenticator`. HTTP CONNECT/SOCKS5 остаются поддержанными; SSL verification не обходится. SOCKS5 remote DNS capability сохраняется.
- При transport error показываются категории DNS, proxy/target TCP, proxy auth, HTTP tunnel, SOCKS handshake, TLS, timeout, invalid response; HTTP 403/407/429/5xx разделяются.
- Лог `~/.gamedelivery/openskin-proxy.log` ограничен 512 KiB, два backup. Перед записью очищаются proxy username/password, URL userinfo и URL-encoded варианты; response body и credentials не записываются. Вкладка журнала показывает OpenSkin diagnostics.
- OpenSkin profile применяется только к OpenSkin. Frankfurter создаётся отдельным `FrankfurterClient()`, ASF/Steam/прочие запросы и глобальный Qt proxy не затрагивались. При выключенном профиле панель передаёт `proxy=None`.
- QSettings по-прежнему содержит только параметры профиля; пароль хранится keyring. Тест подтверждает сохранение/загрузку и отсутствие пароля в ini/UI/log. Реальное keyring и пользовательский профиль этой сессией не читались.

### Изменения и проверки этой части

Код: `integrations/openskin/client.py`, `app/openskin_proxy_settings.py`, `app/main.py`. Tests: `tests/test_openskin_proxy.py`, `tests/test_cs2_price_panel.py`. Документация: `PROJECT_MASTER_LOG.md`, Stage C plan и этот handoff. Предшествующие незакоммиченные файлы C.6.3.1–C.6.3.6 сохранены.

- `python -m unittest tests.test_openskin_proxy -v` — 18 passed, exit 0.
- `python -m unittest tests.test_openskin_client -v` — 5 passed, exit 0; имеется только существующий mock status test (HTTP 429). Никакого внешнего API запроса не делалось.
- `python -m unittest tests.test_cs2_price_panel -v` — 9 passed, exit 0; включая proxy disabled → direct OpenSkin transport и независимый Frankfurter.
- `python -m unittest tests.test_frankfurter_client -v` — 4 passed, exit 0.
- `python -m compileall -q integrations/openskin/client.py app/openskin_proxy_settings.py app/main.py tests/test_openskin_proxy.py tests/test_cs2_price_panel.py` — exit 0.
- `git diff --check` — exit 0; только известные LF→CRLF warnings.

Полный тестовый suite, OpenSkin live request, реальная проверка пользовательского proxy и офлайн-сценарий не выполнялись. Tests — mock/source-level; успешное использование реального proxy не подтверждено. В этой задаче не изменялись price calculations/cache, threshold, inventory, SQLite, Frankfurter, ASF/plugin, delivery/preflight/trade или глобальный Qt proxy.

### Git и следующий шаг

Ветка `work/local-sync`, HEAD `29b36dd1d44245f933346947a1ee9cb85ef6588d`. Commit/push не выполнялись. Рабочее дерево содержит незакоммиченные изменения предыдущих этапов и этой работы; ничего не сбрасывалось.

Пользователь должен в существующем окне настроек локально выполнить обе отдельные проверки: сначала «Проверить прокси», затем «Проверить OpenSkin API». Передать можно только текст результата, категорию и HTTP status, если он есть; credentials или необработанный лог не присылать. При proxy success/API 403 transport пройден, но API отклоняет запрос/маршрут; если proxy probe падает, безопасная категория и detail в локальном логе позволят уточнить стадию. Следующий development step — классифицировать пользовательский результат и при необходимости сделать один узкий transport fix. Не начинать другой этап C.6.3.6 до этой проверки. Live-цены обязательны для принятия C.6.3.6.

---

## C.6.3.6.3 — false timeout guard (2026-11-09)

### Найденная причина и исправление

`_qt_proxy_request()` выдавал запрос, затем подключал `reply.finished`, но всегда входил в `QEventLoop`, не проверяя состояние reply. Qt допускает, что reply уже завершён до обработки сигнала `finished`; официальная документация предлагает `QNetworkReply.isFinished()` для такой проверки. Если сигнал завершения был пропущен до подключения, цикл мог висеть до таймера и выдать ложный timeout «до отправки запроса».

Добавлена минимальная защита: после подключения сигналов цикл/таймер запускаются только при `not reply.isFinished()`, а таймер останавливается в `finally`. Regression test создаёт уже завершённый fake reply и проверяет, что код не запускает таймер и не ждёт. Ссылка: [Qt QNetworkReply](https://doc.qt.io/qtforpython-6/PySide6/QtNetwork/QNetworkReply.html).

### Проверки и состояние

- `python -m unittest tests.test_openskin_proxy -v` — 19 passed, exit 0.
- `python -m compileall -q integrations/openskin/client.py tests/test_openskin_proxy.py` — exit 0.
- `git diff --check` — exit 0 (предупреждения LF→CRLF только для старых затронутых документов/файлов).
- Реальный proxy и OpenSkin не запускались. Эта проверка доказывает исправление сценария с уже завершённым reply, но не заменяет пользовательское live-подтверждение.

Изменения C.6.3.6.3: `integrations/openskin/client.py`, `tests/test_openskin_proxy.py`, Master Log, Stage C plan и этот handoff. Ветка `work/local-sync`, HEAD `29b36dd1d44245f933346947a1ee9cb85ef6588d`; commit/push не выполнялись. Предыдущие незакоммиченные изменения сохранены.

**Следующее действие:** пользователь повторяет «Проверить прокси», затем «Проверить OpenSkin API» в локальной GameDelivery с тем же профилем и сообщает только категорию/HTTP status. Не отправлять credentials или raw logs. Если timeout сохраняется, следующая точка сбора — значение `reply.isFinished()`, `reply.error()`/безопасный `errorString` непосредственно до abort и факт `requestSent`; не увеличивать таймаут без измерения.


---

## C.6.3.6.4 — включительная граница цены и CI checkpoint (2026-11-09)

### Уточнённое правило цены

- Значения OpenSkin Steam Ask ≤ $0.05 остаются видимыми как исходная USD-цена, не конвертируются в RUB и не входят в денежные итоги.
- В конвертацию и RUB totals допускаются только цены > $0.05.
- Условие применяется одинаково к cached_price_from_openskin(), calculate_valuation(), SQLite cache persistence/conversion guard и решению worker о необходимости USD/RUB курса.
- games/cs2/price_store.py использует общий PRICE_THRESHOLD_USD, чтобы порог не был повторно задан отдельным литералом.
- Проверка ровно $0.05 выделена в отдельный cache regression test. Fixture reopen test фиксирует now=self.now и source timestamp тем же временем, чтобы тест свежести не зависел от реального времени запуска.

### Изменения и коммиты

- 9331f343608ccce0a9658e6539de9767d75ba742 — fix: enforce inclusive CS2 price threshold and deterministic cache tests; изменены games/cs2/pricing.py, games/cs2/price_store.py, app/game_inventory_panel.py, tests/test_cs2_pricing.py, tests/test_cs2_price_store.py, tests/test_cs2_price_panel.py.
- 0a3aa2e33f48c6adc8c0a5e78a09f1b0642cdf90 — добавлен CI fallback Isolate test modules after full-suite failure с условием if: failure(). При падении общего suite каждый tests/test_*.py выполняется в отдельном процессе, чтобы найти модуль, способный вызвать самостоятельный Qt shutdown failure. На успешных прогонах шаг пропускается.

### Проверки CI

- На 9331f34 один full-suite run сообщил 481 tests OK, но процесс завершился Segmentation fault / exit 139 после отчёта OK; это непостоянный Qt shutdown симптом, а не failure тестовых assertions.
- На том же кодовом состоянии после commit 0a3aa2e полный workflow 42692454779, attempt 1, прошёл успешно: 481/481 tests, exit 0.
- Повторный attempt 2 того же workflow также прошёл: 481/481 tests, exit 0. Предварительные шаги test_trade_credentials.py (8 tests) и test_trade_delivery_ui.py (9 tests) тоже прошли.
- Диагностический запуск отдельных модулей не выполнялся, поскольку оба полных прогона завершились штатно. Root cause прежнего intermittent Qt shutdown crash не установлен; не считать его исправленным или окончательно устранённым.

### Дополнительная проверка прокси UI

- ASF proxy form и OpenSkin proxy form используют одинаковую структуру ручного ввода: тип, host, port, username, password. OpenSkin намеренно предлагает только HTTP CONNECT и SOCKS5 — именно те типы, которые поддерживает его отдельный Qt transport. Отличие списка поддерживаемых протоколов от ASF general proxy pool намеренное, а не рассинхронизация UI.
- Новый парсер или новый ввод URI в этом checkpoint не добавлялся: текущий раздельный ввод не раскрывает credentials в незамаскированном поле host и сохраняет пароль в системном keyring.

### Ограничения и следующий шаг

- Коммиты выполнялись только в work/local-sync; main, PR #4 и локальная папка D:\GameDelivery не затрагивались.
- Live OpenSkin proxy/API и реальная оценка CS2 на изолированном ASF не выполнялись. Mock/CI pass не доказывает, что пользовательский proxy пропускает запрос или что живые цены и курс получены верно.
- C.6.3.6 остаётся pending live acceptance. Следующий этап: локальные read-only проверки «Проверить прокси», затем «Проверить OpenSkin API», после чего — изолированный 730/2 price acceptance без production ASF 1242 и без отправки Trade Offers.


---

## 2026-11-10 — C.6.3.6.5 implementation continuation (supersedes separate-field UI note)

The previous “Additional proxy UI check” note in this handoff described the separate-field OpenSkin UI as intentional. That decision was wrong and is superseded by the user's explicit requirement for a single input line.

Current implementation commits on `work/local-sync`:
- `abe5fd0da54baa3452699002fec8af4e082687e1`: one masked QLineEdit; parser adapter reuses `integrations/asf/proxy_config.py::parse_proxy_list_text`; phase-aware TLS/auth/request timeout diagnostics; Windows package workflow triggers on `work/local-sync`.
- `7b00dd2812adc48506c7fe62ad899a428da355fe`: corrected obsolete test assertion to `proxy_input`, added bracketed IPv6 proxy URI coverage.
- `434f7243c7d389fd44f9af3451fd811141515453`: distinguishes proxy auth challenge from credentials already supplied; CI post-assertion Qt exit-139 handling requires full-suite “OK” and successful isolated module runs.

Only currently supported proxy types remain HTTP CONNECT and SOCKS5. The whole proxy string is masked while editing, parsed locally, saved with secret only in OS keyring, and cleared after saving. No proxy credential should be pasted in chat/logs. No global Qt proxy is installed; Frankfurter remains independent.

Latest exact-HEAD GitHub Actions runs pending at time of this update:
- Tests: `42769444424`
- Windows package: `42769444129`

Do not treat the prior Windows build artifact from `42769252005` (SHA `7b00dd2`) as final after the auth/CI code change. Wait for the package run on `434f724`, inspect its artifact and self-test. The user's only next check should be on one final package; do not ask for intermediate testing.
