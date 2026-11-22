# Stage C.3 — migration design: quantity stock ledger

Дата: 2026-11-22  
Ветка: `work/local-sync`  
Статус: проект миграции; реализация не начата.

## 1. Исходная схема и ограничения

Проверены `core/database/ledger_migrations.py`, `core/database/db.py`, `core/delivery/reservation_service.py`, `core/delivery/order_pipeline.py`, `games/inventory.py` и `tests/test_delivery_ledger.py`.

Текущая схема имеет следующие свойства:

- `gd_resource_reservations.units` уже существует, но не является количественным ledger: `idx_gd_resource_active_asset_key` уникален по `(bot_name, asset_id_key)` для `state='active'`.
- `gd_asset_reservations_guard_insert/update` дополнительно блокируют повторный физический ресурс для состояний `active` и `legacy_conflict`; проверка использует нормализованный Asset ID.
- `steam_asset_reservations` — compatibility projection с уникальным `(bot_name, asset_id_key)`; insert/delete triggers синхронизируют её с canonical reservation.
- Существующие order reservation services записывают в `gd_resource_reservations` по одной строке на Asset ID и в ряде сценариев создают compatibility projection.
- `InventoryItem.amount` уже валидируется, извлекается из ASF и сохраняется в `game_inventory_snapshot`, но текущая таблица снимков не является транзакционным источником лимита для reservation ledger.
- Старые резервы не содержат доказанного количества стека на момент резервирования. Нельзя автоматически считать, что они занимают только одну единицу, если реальный Amount неизвестен.

Следовательно, простое удаление уникального индекса/триггеров небезопасно: оно разрешит дублирующие резервы без агрегатного ограничения.

## 2. Предлагаемая миграционная стратегия

Использовать отдельный quantity-ledger слой, не переписывая существующую таблицу и не ломая текущие send/reconciliation call sites одним большим изменением.

Новые таблицы:

### 2.1 `gd_stock_observations`

Каноническое последнее наблюдение физического ресурса:

- `game_id`, `app_id`, `context_id`, `bot_name`, `asset_id`, `asset_id_key`;
- `observed_amount > 0`;
- `snapshot_id` / `observed_at`;
- `availability_state`: `usable`, `stale`, `conflict`, `unavailable`;
- nullable `is_tradable` и метаданные полноты источника.

Уникальный физический ключ: `(game_id, app_id, context_id, bot_name COLLATE NOCASE, asset_id_key)`. Сырой `asset_id` сохраняется для точного ASF обращения; ключ используется только для обнаружения alias/collision. Не объединять записи разных ботов.

Наблюдение должно поступать только из полного успешно разобранного ASF inventory response. Частичный/ошибочный ответ не должен обнулять отсутствующие ресурсы и не должен заменять последний подтверждённый снимок.

### 2.2 `gd_stock_allocations`

Новая таблица для quantity allocations:

- `allocation_id`;
- физический ключ ресурса и ссылка на его stock observation;
- `owner_type`: `listing`, `order`, `legacy_blocker`;
- соответствующий `listing_id/config_version_id/listing_generation` либо `order_line_id`;
- `units > 0`;
- `state`: как минимум `active`, `pending`, `unknown`, `consumed`, `released`, `legacy_conflict`;
- timestamps и source/idempotency reference.

На один физический ресурс допускаются несколько allocation rows. Блокирующие состояния должны определяться единым helper/SQL predicate и одинаково применяться в insert/update guards, свободном остатке и reconciliation. Нельзя считать `released` блокирующим; `active/pending/unknown/legacy_conflict` должны блокировать повторное использование. Для `consumed` необходима привязка к revision/snapshot: расход должен уменьшать остаток относительно старого наблюдения, но не вычитаться повторно после нового полного snapshot, который уже отражает расход.

### 2.3 Snapshot revision / consumption accounting

До реализации нужно закрепить один непротиворечивый контракт revision:

- полный inventory snapshot получает монотонную revision/идентификатор для каждого bot/game/app/context;
- allocation фиксирует revision, на которой проверялся запас;
- успешное потребление фиксирует delivery event и revision;
- до следующего полного snapshot consumed units учитываются как уже ушедшие из старого observed amount;
- после более нового полного snapshot, содержащего актуальный остаток, старое consumed accounting не вычитается повторно;
- если ресурс исчез или Amount уменьшился ниже ещё не закрытых active/pending/unknown allocations, observation становится `conflict`; существующие allocations не обрезаются и новые операции блокируются.

Без этого механизма переход `active -> consumed` может ошибочно освободить количество до обновления inventory snapshot либо дважды вычесть расход после обновления snapshot.

## 3. Сохранение совместимости старого ledger

Первая миграция не должна удалять или ослаблять:

- `idx_gd_resource_active_asset_key`;
- canonical reservation guard triggers;
- уникальность `steam_asset_reservations`;
- legacy projection triggers;
- текущие состояния и историю `gd_resource_reservations`.

Старые активные canonical/legacy reservations не мигрировать в фиктивные quantity allocations с `units=1`: фактический Amount на момент их создания неизвестен. Пока старый reservation активен, соответствующий физический ресурс должен быть полностью недоступен для нового quantity ledger (консервативный whole-asset blocker). После доказанного release/consume и свежего полного ASF snapshot ресурс может войти в новый quantity flow.

Нужны двусторонние DB guards:

1. Вставка/изменение блокирующей строки `gd_stock_allocations` отклоняется, если по этому физическому ресурсу есть активный/конфликтный legacy `gd_resource_reservations` или несовместимая legacy projection.
2. Вставка/изменение старого `gd_resource_reservations` отклоняется, если уже существуют блокирующие quantity allocations для того же физического ресурса.
3. Для quantity allocations проверяется агрегатная сумма блокирующих units против usable observed amount внутри SQLite write transaction.
4. Проверка использует полный физический ключ и canonical Asset ID key; alias collision не должен позволять обойти лимит.
5. Если observation отсутствует, stale, unavailable или conflict — новая allocation запрещена.

SQLite `BEGIN IMMEDIATE` сериализует writers; service-level transaction обязателен, но не заменяет DB guards, поскольку старые и новые call sites могут писать разными путями.

## 4. Миграционный порядок

1. Добавить новый migration ID и отдельный backup suffix; включить pre-migration backup в `Database.initialize()` до применения миграции.
2. Создать новые таблицы, индексы, FK и CHECK constraints без изменения старых таблиц.
3. Установить cross-ledger guards для старых и новых reservation writes.
4. Проверить FK и integrity; записать migration marker только в конце транзакции.
5. Сохранить rollback через SQLite transaction; backup должен быть создан до DDL и очищен от секретов тем же механизмом, что текущие миграции.
6. Не делать backfill старых reservations как единичных allocations. Зафиксировать их как legacy blockers через cross-ledger guard, сохранив исходные записи и owners.
7. На старте после миграции не включать автоматическое перераспределение; миграция схемы не должна инициировать Playerok sync, dispatcher или send.

## 5. Транзакционные операции после миграции

- Stock refresh: полный ASF inventory parse -> snapshot revision -> атомарное upsert observations; на конфликте с активными allocations сохранить факт нового Amount и пометить observation conflict, не уменьшать/удалять allocations.
- Listing allocation: `BEGIN IMMEDIATE`; проверить usable/fresh observations; рассчитать free units; записать весь требуемый quantity или rollback целиком.
- Listing -> order transfer: в одной транзакции перевести конкретные slices и idempotency key; повторное paid event не создаёт новые allocations.
- Dispatch: старый delivery pipeline не должен отправлять quantity allocation до появления quantity-aware manifest/ASF transport; migration сама по себе не меняет отправку.
- Consumption: фиксировать exact units и snapshot revision; не освобождать единицы до reconciliation по свежему inventory.

## 6. Обязательные migration и ledger tests

- Миграция на пустой БД и на БД после всех текущих migration IDs.
- Старые активные canonical и legacy reservations остаются неизменными и блокируют новый quantity ledger.
- Совпадающая legacy projection не вызывает ложного двойного блокирования; чужой owner/alias conflict fail-closed.
- Несколько allocation rows на один stack разрешены, когда агрегат не превышает observed amount.
- Попытка превысить observed amount блокируется и при INSERT, и при UPDATE.
- Два конкурентных writer-а не могут занять одни units.
- Отсутствующее/stale/conflict observation блокирует новую allocation.
- Уменьшение Amount ниже суммы blocking allocations сохраняет allocations, помечает конфликт и блокирует дальнейшие writes.
- Rollback при ошибке DDL/trigger/FK оставляет схему и данные прежними; backup integrity проверяется.
- `PRAGMA foreign_key_check` и `PRAGMA integrity_check` успешны после миграции.

## 7. Границы первого implementation slice

Первый кодовый slice ограничить миграцией, observation contract/store и тестами агрегатного лимита. Не подключать пока Playerok listing lifecycle, order transfer, quantity-aware DeliveryLeg/ASF payload, UI и автоматический dispatch. Не менять лицензию, Steam Web API usage или реальную торговлю.

Отдельное обязательное правило запуска сохраняется: после старта программа не должна принимать к исполнению сделки, которые появились, пока программа была выключена. Миграция и stock refresh не являются разрешением на обработку исторических сделок.
