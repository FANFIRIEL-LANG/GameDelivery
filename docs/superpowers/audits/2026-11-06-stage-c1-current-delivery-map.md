# C.1 — Карта существующей системы заказов и доставки

**Дата аудита:** 2026-11-06  
**Ветка:** `feature/asf-bulk-account-import`  
**Кодовый baseline:** `a6b0af4cb0a2cfac14c7cdbf4c56692935cc0c7f`  
**Документационный HEAD после подготовки плана:** `c543cd8e351594798c552ddca566e2c77d0e73ca`  
**Статус:** аудит выполнен; код приложения и схема БД не изменялись.

## 1. Краткий вывод

На ветке есть рабочий, но узкий путь Steam Trading Cards через UI → локальные SQLite-таблицы → ASF IPC → GameDelivery ASF plugin → Steam trade offer. Это не универсальная система выдачи.

В проекте одновременно существуют:
- базовая очередь `core/orders` с количеством и типовыми статусами;
- фактически используемая UI очередь `delivery_orders`;
- отдельная таблица конкретных активов заказа `delivery_order_assets`;
- отдельный текущий резерв Steam asset ID `steam_asset_reservations`;
- снимки Steam Community inventory (753/6);
- отдельные снимки игровых инвентарей (CS2/Dota 2/TF2/Rust);
- отдельный постоянный журнал идемпотентности ASF plugin `sent_offers.json`.

Эти части не образуют общего entitlement ledger. У них разные владельцы состояния и разные правила восстановления. Их нельзя объединять или удалять без плана миграции и тестов сохранности.

## 2. Фактический поток Playerok → UI → заказ

### 2.1 Получение сделки

`app/platforms_panel.py`:
- регистрирует `PlayerokAdapter` в `MarketplaceRegistry`;
- выполняет `fetch_orders` в QThread;
- отображает прочитанные сделки;
- по кнопке импорта передаёт выбранную нормализованную запись через сигнал `order_import_requested`.

`integrations/playerok/client.py:385-455`:
- использует приватный GraphQL endpoint сайта, а не документированный публичный API;
- запрашивает сделки направления OUT со статусами PAID и SENT;
- выбирает `first: 50`;
- запрашивает `pageInfo`, но текущая реализация не проходит по страницам;
- извлекает ID, статус, дату, название товара, пользователя и `transaction.value`;
- не извлекает отдельное подтверждённое количество единиц товара.

**Следствие:** текущая выборка ограничена первой страницей до 50 записей. Поле `amount` — денежная сумма сделки. Оно не может использоваться как `paid_quantity`. Отдельное количество из текущего ответа не подтверждено.

### 2.2 Импорт в локальную очередь

`app/platforms_panel.py:224-241` передаёт выбранную сделку в главный UI.  
`app/main.py:268-324` реализует `_import_marketplace_order`:
- принимает только `playerok` и непустой внешний ID;
- создаёт запись в `delivery_orders`;
- использует уникальный индекс по `(source_platform, source_order_id)` для защиты от дублей;
- сохраняет `source_status`, `source_amount`, URL, название и покупателя;
- при повторном импорте обновляет часть данных существующей записи.

UI фактически работает с `delivery_orders`, а не с `core/orders`.

## 3. Две модели заказов

### 3.1 `core/orders` — базовая очередь

Файлы:
- `core/database/db.py`
- `core/orders/service.py`
- `tests/test_orders.py`

Схема `orders` содержит:
- внутренний integer ID;
- source и external_id с уникальностью;
- item_type, item_name;
- quantity с CHECK > 0;
- статусы queued / processing / completed / failed / cancelled;
- payload_json и error_message;
- timestamps.

`OrderService` реализует enqueue с дедупликацией, чтение, фильтрацию и контролируемые переходы статусов. В ходе сканирования всех Python-файлов `app/` и `core/` на текущем HEAD не найдено импорта/вызова `OrderService` из UI или рабочего delivery path. Сервис проверяется отдельными unit tests, но не является фактическим диспетчером отправки Steam.

### 3.2 `delivery_orders` — фактическая UI очередь

Создаётся и мигрируется в `app/main.py:200-251`. Содержит:
- строковый order_id;
- recipient_trade_url;
- свободный текст status;
- asset_count;
- source_platform/source_order_id;
- source_title/source_buyer/source_status/source_amount/source_url;
- trade_offer_id и trade_confirmation_status.

Уникальный частичный индекс `idx_delivery_external_order` защищает внешний ID площадки от повторного импорта. Текущие статусы не ограничены CHECK/enum и меняются прямыми SQL UPDATE в UI-классе.

`asset_count` — число выбранных активов, а не оплаченная покупателем величина. В таблице нет paid_quantity, reserved_quantity, delivered_quantity и журнала событий количества.

### 3.3 Таблицы активов и резервов

- `delivery_order_assets`: сохранённый состав заказа (order_id, bot_name, asset_id), PK не задана; FK на delivery_orders объявлен в DDL.
- `steam_asset_reservations`: текущие активные резервы (bot_name, asset_id, order_id, created_at), PK (bot_name, asset_id); история освобождённых резервов не сохраняется.
- `AssetReservationStore.reserve` использует BEGIN IMMEDIATE и отклоняет конфликтующий batch целиком. Повтор того же заказа идемпотентен по строкам; имя бота сравнивается без учёта регистра.
- `release(order_id)` физически удаляет активные резервы; отдельного event log нет.

**Транзакционная граница:** UI сначала вызывает `AssetReservationStore.reserve` (отдельное соединение/транзакция), затем отдельной транзакцией вставляет заказ и строки `delivery_order_assets`. При ошибке вставки выполняется компенсирующий `release`. Это не одна атомарная транзакция для заказа + резерва + состава.

**SQLite FK:** DDL `delivery_order_assets` объявляет FK, но raw `sqlite3.connect` в `app/main.py` не включает `PRAGMA foreign_keys=ON`. `Database.connect` включает FK, но он используется отдельным `core/orders` хранилищем. В C.3 нужно проверить/нормализовать соединения и миграции; в C.1 существующие таблицы не менялись.

## 4. Реальный UI путь Steam Trading Cards

### 4.1 Снимок и отображение

`app/inventory_panel.py` использует `ASFInventoryController` → `games/steam_cards/asf_sync.py` → `ASFClient.get_bot_inventory(753, 6)` → `parse_bot_inventory` → `InventorySnapshotStore`.

`InventorySnapshotStore`:
- хранит снимки по bot_name;
- мигрирует старую схему без bot_name в legacy bot;
- сохраняет metadata и время обновления;
- считает пустой, старый (>24 часов), некорректный или несогласованный снимок устаревшим;
- сам по себе не обращается к Steam/ASF.

Парсер сохраняет не только карточки, но и другие предметы из Steam Community inventory 753/6, различает `is_trading_card`, foil и tradability. Неизвестная передаваемость не должна трактоваться как разрешение.

### 4.2 Подготовка заказа в фактически используемом UI

`app/main.py:343-389` загружает элементы из `steam_inventory_snapshot`, показывает тип «Карточка» или «Предмет» и блокирует выбор только при запрещённой/неизвестной передаваемости или активном резерве.

`app/main.py:409-507`:
- проверяет HTTPS Trade URL;
- повторно проверяет свежесть снимка и tradability каждого выбранного asset;
- резервирует выбранные `(bot_name, asset_id)`;
- записывает `delivery_orders` и `delivery_order_assets`;
- при ошибке записи освобождает резерв.

**Обнаруженное расхождение:** UI допускает резервирование любого передаваемого предмета из 753/6, включая записи с `is_trading_card = false`. Но plugin preview/send принимает только `Asset.Type == TradingCard`. Поэтому другой предмет может быть зарезервирован в UI, а затем отклонён на этапе ASF preview. Это нужно закрыть на соответствующем delivery handler этапе; в C.1 код не исправлялся.

### 4.3 Отдельный сервис preflight

`games/steam_cards/order_preflight.py` / `SteamCardOrderPreflight`:
- проверяет данные значка получателя и допустимую вместимость;
- выбирает полные наборы обычных карточек;
- исключает foil и непередаваемые/неподтверждённые экземпляры;
- резервирует точные активы;
- не отправляет обмен.

Он покрыт `tests/test_order_preflight.py`. При этом сканирование imports/calls в `app/` показало, что текущий `MainWindow` не импортирует и не вызывает `SteamCardOrderPreflight`: UI использует прямую ручную выборку и `AssetReservationStore`. Следовательно, этот preflight — существующая тестируемая доменная возможность, но не подключённый шаг текущего UI-потока.

## 5. ASF preview/send и восстановление

### 5.1 ASF client

`integrations/asf/client.py`:
- ограничивает IPC localhost:1242;
- preview отправляет только order ID, bot, SteamID64 и точный список asset IDs через `gamedelivery-preview`; trade token в preview не передаётся;
- send валидирует trade URL и asset IDs;
- trade token записывает во временный одноразовый файл с ограниченными ACL;
- в команду ASF передаёт nonce, не сам trade token;
- удаляет временный секрет после обработки.

### 5.2 ASF plugin

`integrations/asf_plugin/GameDeliveryPlugin.cs`:
- команды preview/send доступны только ASF Owner;
- preview получает живой инвентарь и проверяет каждый актив;
- preview возвращает `sendEnabled=false` и не отправляет обмен.

`integrations/asf_plugin/TradeDelivery.cs`:
- сверяет одноразовый секрет с nonce, заказом, ботом, получателем и точным списком asset IDs;
- повторно получает живой inventory перед отправкой;
- требует онлайн-бота, индивидуального получателя, отличного от самого бота;
- требует app 753/context 6, tradable=true и type TradingCard;
- сериализует send через SemaphoreSlim;
- сохраняет fingerprint и запись по order ID в `%LOCALAPPDATA%/GameDelivery/ASFPlugin/sent_offers.json` до вызова отправки;
- повтор того же payload для уже записанного offer не создаёт второй offer;
- unknown outcome блокирует повтор;
- сохраняет offer IDs и состояние awaiting_confirmation/sent.

**Граница подтверждения:** созданный offer, ASF/mobile confirmation и принятие предложения покупателем — разные события. Текущий статус UI «sent — ожидание покупателя» не является доказательством принятия покупателем и не может автоматически считаться окончательно доставленным entitlement.

### 5.3 SQLite UI статусы и recovery

`app/main.py:575-830`:
1. требует статус `reserved — не отправлен`;
2. сравнивает сохранённый состав заказа с активными резервами;
3. вызывает live ASF preview;
4. показывает точного получателя/бота/asset IDs и требует явного Yes;
5. выполняет compare-and-set статуса на `sending — запрос ASF`;
6. вызывает ASF send;
7. успех сохраняет offer IDs и pending/awaiting status;
8. неизвестный результат блокирует повтор.

`app/main.py:253-265` при запуске переводит незавершённый `sending — запрос ASF` в unknown и оставляет резерв активным. Отмена unknown заказа также сохраняет резерв до ручной сверки.

**Раздельные хранилища:** SQLite `delivery_orders` и plugin `sent_offers.json` имеют отдельные транзакции/жизненный цикл. C.4/C.5 должны определить сверку их состояний после потери файла/БД, сбоя между записью plugin record и обновлением SQLite, а также повторного импорта/запуска. Не пытаться «лечить» unknown автоматическим повтором.

## 6. Игровые инвентари и CS2

`games/catalog.py` содержит:
- CS2 730/2;
- Dota 2 570/2;
- Steam Cards 753/6;
- TF2 440/2;
- Rust 252490/2.

`games/inventory.py`:
- имеет общий `InventoryItem` без card-only assumptions;
- парсит ASF Assets/Descriptions;
- сохраняет owner bot, asset ID, class/instance, name, type, tradable/marketable, quality, rarity, exterior, tags;
- `GameInventoryStore` использует отдельные `game_inventory_snapshot` и `game_inventory_snapshot_meta`;
- `refresh_game_inventory` получает inventory по app/context из каталога.

`app/game_inventory_panel.py` явно описан как read-only: резервы и обмены отключены. Кнопка «Отправить через заказы…» только передаёт Trade URL в главное окно. Сам список активов для заказа в `app/main.py` загружается из `steam_inventory_snapshot` (753/6), не из `game_inventory_snapshot`. Следовательно, игровой инвентарь сейчас не подключён к выдаче.

`games/cs2/__init__.py` остаётся placeholder. Есть CS2 inventory parsing/display/filtering foundation, но отдельного CS2 delivery handler и проверенного способа передачи CS2 asset через ASF в текущем проекте не найдено.

## 7. Карта существующих механизмов → будущий контракт → тесты

| Существующий механизм | Фактическая роль сейчас | Будущий контракт этапа C | Существующие regression tests |
|---|---|---|---|
| Playerok read-only query/import | Получение первой страницы PAID/SENT и импорт в delivery_orders | Marketplace order/line adapter с подтверждённым paid quantity и cursor pagination | `test_playerok_read_only_sales.py`, `test_marketplace_integrations.py`, `test_database_migrations.py` |
| `core/orders.OrderService` | Отдельная базовая очередь; не подключена к UI delivery path | Общий order/line domain model после проверки совместимости | `test_orders.py` |
| `delivery_orders` + `delivery_order_assets` | Фактический UI заказ и выбранные Steam assets | Adapter/legacy compatibility в общем ledger | `test_database_migrations.py`, `test_main_delivery_send.py`, `test_trade_delivery_ui.py` |
| Steam inventory parser/snapshot | ASF 753/6, card metadata, tradability, freshness | Inventory provider contract для Steam Cards | `test_asf_inventory.py`, `test_asf_snapshot_sync.py`, `test_steam_card_snapshot.py` |
| Set selection/shared inventory | Наборы карточек, owner mapping, foil/tradability | Steam Cards handler preparation/selection | `test_steam_card_selection.py`, `test_shared_steam_card_sets.py`, `test_inventory_views.py`, `test_inventory_view_generators.py` |
| Badge preflight | Standalone domain service, не подключён к MainWindow | Card-specific validation stage before reserve | `test_order_preflight.py`, `tests/test_recipient_badge.py` |
| AssetReservationStore | Атомарный текущий резерв уникальных bot/asset ID | Resource reservation backend linked to entitlement ledger | `test_asset_reservations.py`, `test_order_preflight.py` |
| ASF client preview/send | Local IPC, no-send preview, one-use secret, structured outcome | Versioned delivery handler adapter | `test_asf_client.py`, `test_main_delivery_send.py` |
| ASF plugin | Только Steam Trading Cards 753/6, type TradingCard | Существующий card handler; отдельный handler для каждого подтверждённого игрового контракта | `test_asf_plugin_contract.py`, ASF plugin build |
| Startup recovery/cancel | sending → unknown; unknown keeps reserve | Общий attempt/reconciliation lifecycle | `test_main_delivery_send.py`, `test_trade_delivery_ui.py` |
| Game inventory/CS2 | Read-only inventory snapshots and UI | Game item inventory provider + verified game-specific delivery handler | `test_game_inventory_modules.py`, `test_inventory_foundation.py` |

## 8. Базовые тесты и CI

На кодовом baseline `a6b0af4cb0a2cfac14c7cdbf4c56692935cc0c7f` 2026-11-06 GitHub Actions завершились успешно:
- Tests run `42145721444`, job `127656590980`: **263 tests, OK**.
- ASF plugin run `42145721483`, job `127656591253`: success.
- Windows build run `42145721398`, job `127656590525`: success.

Полный test log Tests run был прочитан; в нём есть проходы соответствующих inventory, selection, reservation, order preflight, send UI, Playerok и game inventory tests.

Документационные коммиты `1e7187e`, `7559d48`, `c543cd8` не меняют код. GitHub compare `a6b0af4...c543cd8` показывает только:
- добавление `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`;
- обновление `PROJECT_MASTER_LOG.md`.

Новый test workflow для документационных коммитов не запускался; 263 теста — подтверждённый CI baseline неизменённого кода, а не отдельный новый запуск на docs-only HEAD.

## 9. Риски и открытые вопросы, перенесённые в следующие блоки

| ID | Наблюдение/риск | Где закрывать |
|---|---|---|
| C1-R1 | Два независимых order stores; UI не использует OrderService | C.2/C.3 — выбрать каноническую модель и миграцию |
| C1-R2 | Playerok query не содержит quantity и не обходит следующие страницы | C.2 — доказать источник количества; отдельная marketplace pagination задача в согласованном этапе площадок |
| C1-R3 | `source_amount` денежный, не quantity | C.2 — запретить подстановку суммы в paid quantity |
| C1-R4 | Steam UI может резервировать не-card item, ASF plugin принимает только TradingCard | C.5 — связать card handler с типовым контрактом и фильтром |
| C1-R5 | SteamCardOrderPreflight существует, но MainWindow его не вызывает | C.5 — согласовать подключение/сохранение правил badge/full sets |
| C1-R6 | Game inventory UI read-only; game snapshots не связаны с order assets | C.6 — только после подтверждения ASF transfer API |
| C1-R7 | Reservation и order/asset rows пишутся отдельными транзакциями | C.3 — атомарная граница и восстановление |
| C1-R8 | FK объявлен в delivery_order_assets, но raw app connections не включают foreign_keys | C.3 — единый connection/migration policy |
| C1-R9 | Текущая таблица reservation хранит состояние, не полную историю резервов/освобождений | C.3 — ledger/event history |
| C1-R10 | SQLite UI state и plugin JSON idempotency store независимы | C.4/C.5 — reconcile contract |
| C1-R11 | Steam offer создан / mobile confirmation handled / recipient accepted — разные факты | C.2/C.5 — точное правило delivered |
| C1-R12 | Unknown outcome intentionally blocks retry and retains reservation | C.4 — общий безопасный attempt/recovery contract |

## 10. Ограничения аудита

- Проверялись исходники и тесты ветки GitHub на указанном HEAD; локальный пользовательский SQLite файл и установленный ASF на компьютере пользователя не открывались.
- Live Steam offer end-to-end не выполнялся. Статус этапа 7 исторического плана остаётся незавершённым.
- Не проводилась внешняя проверка текущих ограничений/документации Playerok и ASF; C.1 фиксирует фактический кодовый контракт, а не гарантирует неизменность внешнего сервиса.
- Не менялись исходники приложения, тесты, пользовательская БД, PR base, main или другие ветки.

## 11. Результат C.1

**Критерии C.1:**
- [x] Карта таблиц, связей, статусов и фактических вызовов подготовлена.
- [x] Для существующего Steam Cards пути указаны конкретные regression tests.
- [x] Разделены реально используемые механизмы, standalone services и read-only inventory foundation.
- [x] Миграционные и идемпотентные риски перечислены; удаление таблиц/функций не предлагается.
- [x] Код приложения и пользовательские данные не изменялись.
- [x] Базовые CI результаты прочитаны и записаны.

**Итог:** C.1 выполнен. Следующий блок C.2 не начинать без отдельного разрешения пользователя.
