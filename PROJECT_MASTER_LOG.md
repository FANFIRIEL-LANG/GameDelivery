## Remote patched ASF artifact acceptance (2026-11-28)

Перед публикацией проверены `git status`, `git diff --name-only`, `git diff --stat` и `git diff --check`; посторонних файлов не было. Опубликован только `work/local-sync`: commit `361ec8caf086d0806df15291b56cd05e26dca14e`, push `origin/work/local-sync` успешен. `main`, PR #4 и force push не затрагивались.

Запущен GitHub Actions workflow `Build custom ASF Generic (no trade-off signature)` на commit `361ec8c`. Run `46147761755` завершён успешно за 3m 38s; job `build-custom-asf` успешен. Артефакт: `GameDelivery-ASF-Generic`, размер 8.21 MB / 8,605,517 bytes, digest `sha256:95e6dd2b64be5c2399907814a601ae33edcfdf565961e99dc640a0e15d8626a1`, срок хранения до 2026-12-28.

По workflow provenance подтверждено: ASF исходник checkout-ится с tag 6.3.11.7, patch применяется до publish, `ArchiSteamFarm.dll` публикуется из patched source, plugin собирается против этого host, BUILD_INFO создаётся с upstream tag/patch/GameDelivery SHA/no-live-operations, архив содержит опубликованный runtime и plugin. GitHub UI подтвердил наличие единственного артефакта `GameDelivery-ASF-Generic` и его digest. Содержимое скачанного архива через unauthenticated API получить нельзя (GitHub требует authentication), поэтому локальная побайтная инспекция `BUILD_INFO.txt` и списка файлов не выполнялась; доказательством состава остаётся успешный workflow path и artifact metadata.

Установка: скачать артефакт из run 46147761755; до запуска остановить изолированный ASF; сделать копию всей папки runtime и отдельно сохранить `config`, `bots`, `maFiles`, logs, `plugins` и `%LOCALAPPDATA%\GameDelivery`; распаковать архив во временную папку; проверить `BUILD_INFO.txt`; заменить только runtime-файлы ASF и plugin directory, не удаляя конфигурацию/данные; затем запустить ASF и проверить загрузку GameDeliveryPlugin. Официальный unpatched ASF Generic для live sending не использовать. Реальные Steam credentials, Trade Offers, confirmations и Steam operations не выполнялись. Отсутствие подписи на Steam остаётся неподтверждённым до ручной acceptance проверки пользователя.

Подготовлен следующий сценарий ручной Steam acceptance: использовать отдельный тестовый bot/recipient и минимальный разрешённый тестовый asset; перед отправкой проверить runtime version/provenance и `customMessage: string.Empty`; создать ровно один тестовый offer вручную через штатный GameDelivery confirmation flow; открыть страницу Steam Trade Offer и проверить, что `Sent by ArchiSteamFarm-modded/6.3.11.7` и любые ASF replacement messages отсутствуют; проверить offer ID, статус и confirmation handling; затем отменить/закрыть тестовый offer согласно операторскому процессу. Агент самостоятельно этот сценарий не выполнял и не будет выполнять без отдельного разрешения.

## ASF patched runtime build verification and Playerok live-acceptance preparation (2026-11-28)

Проверка применимости `integrations/asf_patch/remove-trade-offer-signature.patch` выполнена против исходников точного ASF tag `6.3.11.7`, загруженных из официального репозитория `JustArchiNET/ArchiSteamFarm`: найдено ровно одно исходное присваивание `tradeOfferMessage` на строке 690 `ArchiWebHandler.cs`; после применения ровно одна строка стала `string tradeOfferMessage = string.Empty;`; downstream payload по-прежнему передаёт `tradeoffermessage = tradeOfferMessage`. Проверка исходника и whitespace прошли.

`.github/workflows/asf-custom-generic.yml` действительно checkout-ит ASF tag `6.3.11.7` в отдельный `asf-source`, применяет single-purpose patch до `dotnet publish`, проверяет exact one-line diff, собирает ASF из patched source, компилирует GameDelivery plugin против опубликованного patched host и упаковывает `GameDelivery-ASF-Generic.zip`. Это не официальный runtime: workflow не скачивает официальный ASF binary для финальной публикации. BUILD_INFO содержит upstream tag, patch path, empty trade-offer message, GameDelivery commit и запрет реальных операций.

Локальная полная сборка ASF не выполнена: на рабочей машине установлен только .NET runtime 10.0.15, SDK отсутствует (`dotnet --info`: `No SDKs were found`). Поэтому собранную `ArchiSteamFarm.dll`, BUILD_INFO и фактический состав `GameDelivery-ASF-Generic.zip` локально подтвердить нельзя; это остаётся обязательной проверкой Windows GitHub Actions workflow. Реальные Steam credentials, ASF startup с ботом и Trade Offers не использовались.

Инструкция установки добавлена/уточнена в `integrations/asf_plugin/README.md`: для live send использовать только `GameDelivery-ASF-Generic.zip`, остановить изолированный ASF, сделать резервную копию config/data, заменить runtime-файлы patched архивом, сохранить `config`, `bots`, `maFiles`, logs и plugin data, проверить `BUILD_INFO.txt`, затем запускать ASF. Официальный unpatched host для live sending запрещён. Отсутствие подписи на странице Steam не объявляется подтверждённым до ручной проверки пользователя.

Следующий подготовленный блок — контролируемая ручная Playerok live-приёмка без запуска операций агентом: проверить авторизацию/видимость сделки, точный listing/config binding, read-only sync, preview, operator confirmation boundary, outbox/status/recovery; реальные сообщения, файлы, сделки и delivery выполнять только после отдельного разрешения.

## ASF trade-offer signature — runtime source analysis and local deployment correction (2026-11-28)

Проверен фактически доступный локальный runtime `tests/asf_plugin_smoke/bin/Release/net10.0/ArchiSteamFarm.dll`: assembly version `6.3.11.7`, informational build `6.3.11.7+6dca024487b9cfe47fc8cc86a38d9dcaa17f5cc8`. Это официальный/unpatched host artifact, поэтому он не является доказательством отсутствия подписи и не должен использоваться для live send.

Источник подписи установлен в ASF `ArchiSteamFarm/Steam/Integration/ArchiWebHandler.cs`: стандартное присваивание `string tradeOfferMessage = $"Sent by {SharedInfo.PublicIdentifier}/{SharedInfo.Version}";`. `TradeDelivery.cs` уже передаёт `customMessage: string.Empty`; GameDelivery.ASFPlugin и другие найденные компоненты строку `Sent by` не формируют. Обработка отправки и mobile confirmations не изменялась.

Исправление закреплено как единственный source/runtime build path в `.github/workflows/asf-custom-generic.yml` и `integrations/asf_patch/remove-trade-offer-signature.patch`: официальный ASF tag `6.3.11.7` патчится ровно на `string.Empty`, исходник проверяется на ровно одну замену, затем публикуется `GameDelivery-ASF-Generic.zip` вместе с GameDelivery plugin и BUILD_INFO. README теперь явно запрещает использовать официальный unpatched host для live trade sending и требует patched runtime. Добавлен regression contract test на patch/provenance/deployment boundary.

Проверка: `python -m unittest tests.test_asf_plugin_contract -v` — 45/45 passed; `git diff --check` — passed. Реальный Trade Offer не создавался; Steam credentials, confirmations и внешние операции не выполнялись. Controlled live acceptance остаётся отдельным gate: после установки patched runtime требуется разрешённый тестовый offer и проверка страницы Steam.

Изменённые файлы: `integrations/asf_plugin/README.md`, `tests/test_asf_plugin_contract.py`, этот Master Log. Commit/push не выполнялись; `main` и PR #4 не затрагивались.

**C.3 atomic PAID-deal → Order commit — implementation boundary audit (2026-11-25):** Повторно сверены текущие `order_pipeline.py`, `reservation_service.py`, `ledger.py`, `listing_stock.py`, Playerok `sync.py`, schema и acceptance plan, пункт 320. Подтверждён конкретный остаточный разрыв: `PlayerokDealSyncService._maybe_create_order()` вставляет `gd_orders` в транзакцию синхронизации PAID deal (и legacy может материализовать manifest там же); затем `DeliveryOrderPipeline.prepare_recipe_order()` требует уже существующий Order через `_config_for_order()`; только позже `reserve_recipe_plan()` открывает отдельную `BEGIN IMMEDIATE` и создаёт typed OrderLines, ListingStock→Order transfer, DeliveryLegs, canonical reservations и статус reserved. Следовательно, успешная атомарность последнего reservation commit сама по себе не выполняет требование пункта 320: создание самого Order должно быть включено в ту же финальную транзакцию. Следующий implementation slice определён: PAID sync сохраняет canonical Deal/event и стабильную привязку к ConfigVersion без преждевременного Order; pure planning получает immutable deal/config context и стабильный будущий order ID без ledger writes; успешный commit внутри одной `BEGIN IMMEDIATE` перепроверяет deal paid/eligible + exact ConfigVersion + idempotency, вставляет или подтверждает Order, материализует полный OrderLine manifest, переводит ListingStock, создаёт DeliveryLeg и canonical reservations, фиксирует reserved. Для preflight-blocked/shortage — отдельный fail-closed atomic commit Order + полный manifest + preflight_blocked без transfer/legs/reservations; не создавать частичные ресурсы. Duplicate PAID events и retry должны разрешаться по существующему UNIQUE deal_id и стабильному order ID. Существующие Steam Cards buyer/seller preflight и complete-set quota semantics, CS2 Steam Asset selection/ListingStock exact units, ASF transport и manual-review boundary не менять; live offers не запускать. Нужны regression tests на: paid sync без Order; commit создаёт Order+manifest+legs+reservations атомарно; injected failure после Order insert откатывает всё; insufficient/preflight blocked сохраняет только Order+manifest; duplicate event/retry идемпотентны; ConfigVersion/paid status change между planning и commit блокирует commit. Пока это audit/design boundary, production code не менялся и C.3 остаётся OPEN.

**C.3 typed Recipe manifest transaction boundary — CI verification (2026-11-25):** Финальный проверенный code HEAD `68ce9b7cf435b1310a4d928c96251ecdc9213334` в `work/local-sync`. После исправления границы транзакции typed Recipe OrderLines создаются внутри `reserve_recipe_plan()`, а не во время Playerok PAID sync; legacy single-selector manifest path оставлен отдельно. До reservation commit PAID sync создаёт только Order shell; ListingStock transfer и canonical reservations не должны появляться до финального commit. Handoff regression test проверяет отсутствие typed OrderLines до prepare и их наличие после успешной подготовки; rollback regression test проверяет отсутствие OrderLines после искусственной ошибки вставки reservation. GitHub Actions на точном HEAD `68ce9b7`: Tests run `45689592535` — **success**; Windows Build run `45689592517` — **success**. Более ранний Tests run на `011f0ef` завершился failure; промежуточные прогоны отменены при обновлении commit, поэтому итоговым считается только успешный прогон на `68ce9b7`. C.3 остаётся **OPEN**: успешный CI подтверждает текущие тесты, но не доказывает выполнение полного acceptance criterion единой транзакции Order + manifest + ListingStock transfer + DeliveryLeg + canonical reservations во всех сценариях. Следующий slice — аудит и закрытие оставшихся transaction-boundary gaps по acceptance plan, без изменения отдельной Steam Cards selection logic и CS2 execution logic. Нет live Playerok/Steam offers. `main`, PR #4 и licensing не затрагивались.

**C.3 typed Recipe manifest commit-boundary correction (2026-11-25):** Убрана преждевременная материализация typed Recipe OrderLines в `PlayerokDealSyncService._maybe_create_order()`. PAID sync по-прежнему создаёт стабильный canonical Order shell только после точной persisted ConfigVersion binding; legacy single-selector path сохранён без изменений. Для typed Recipe sync теперь не пишет ни одной OrderLine. Pure resource planning использует стабильные OrderLine IDs, а `reserve_recipe_plan()` создаёт полный immutable manifest уже внутри своей `BEGIN IMMEDIATE` транзакции вместе с ListingStock→Order transfer, DeliveryLegs, canonical reservations, Steam Cards listing quota claim и переводом Order в `reserved`. Поэтому ошибка на любом INSERT откатывает и manifest, и transfer, и legs/reservations; для fail-closed preflight/shortage отдельный blocked path сохраняет manifest и `preflight_blocked`, без физических резервов. Обновлён Playerok ListingStock handoff test: до prepare нет OrderLines/transfer, после успешного prepare manifest и transfer существуют; rollback test теперь явно подтверждает отсутствие OrderLines после synthetic reservation insert failure. Production selectors/Steam Cards selection/CS2 selection/ASF transport не менялись. Commits: `011f0ef` (defer typed manifest), `30253f7` (handoff assertions), `c9d8671` (rollback assertion). GitHub Actions на HEAD `c9d8671bfe6f535473d17357b905ad20f179208c` запущены; CI ещё не подтверждён. Это закрывает раннюю запись typed manifest, но C.3 остаётся OPEN до полного CI и оставшихся acceptance checks.

**C.3 reservation transaction regression test corrections (2026-11-25):** Первый CI после добавления conflict rollback показал три проблемы тестового слоя в `test_recipe_unified_reservations.py`: (1) mixed ListingStock fixture передавал исходный item id вместо ID, возвращённого persisted ConfigVersion, и оставлял обязательный второй Steam Asset без корректного allocation scope; fixture теперь берёт canonical RecipeItem IDs из сохранённой ConfigVersion и помечает конфликтующую вторую позицию optional, чтобы проверить именно rollback уже staged ListingStock transfer; (2) helper `assert_no_reservation_commit` ждал `pending` даже для ожидаемого `preflight_blocked` после quota exhaustion — теперь ожидаемый статус передаётся явно; (3) materializer теперь корректно fail-fast обнаруживает повреждённый OrderLine manifest до snapshot-level validation, поэтому тест ожидает точную ошибку manifest mismatch. Production behavior не ослаблялся. Новый HEAD `9a59a963752b2ad598ff32ec49156fe320fd3554`; GitHub Tests и Windows Build запущены, результаты ожидаются.

**C.3 mixed-plan conflict rollback correction (2026-11-25):** Дополнительная проверка transaction flow выявила ошибку в `reserve_recipe_plan()`: при конфликте с активным reservation другого Order метод возвращал `preflight_blocked` непосредственно из открытой транзакции. Если до проверки конфликтующего ресурса в той же транзакции уже был выполнен ListingStock→Order transfer для другой RecipeItem, обычный return коммитил частичный transfer без полного набора DeliveryLeg/reservations и оставлял Order в неверном состоянии. Исправлено: conflict branch явно откатывает всю reservation transaction, затем в новой `BEGIN IMMEDIATE` сохраняет только полный immutable manifest и `preflight_blocked`; если статус успел измениться конкурентно, переход отклоняется. Добавлен regression test смешанного Recipe: ListingStock-backed Steam Asset + вручную планируемый Steam Asset, занятый другим Order. Проверяется отсутствие transfer/order allocations/DeliveryLeg, сохранение исходной listing allocation и persisted `preflight_blocked`. Тесты/CI для нового HEAD ожидаются; локальный запуск в этой среде не выполнялся.

## C.3 preflight status persistence correction (2026-11-25)

**Проверенная база:** `work/local-sync`, HEAD перед исправлением `fba652b4536cb162d026e73a4253f9a316e4a355`.

### Найденная ошибка

`prepare_recipe_order()` возвращал `preflight_blocked` при недостатке обязательных ресурсов, но в БД заказ оставался `pending`. Это расходилось с фактическим результатом pipeline и делало состояние заказа неверным для очереди/повторной обработки.

### Исправление

- `_persist_preflight_manifest()` теперь в одной `BEGIN IMMEDIATE` транзакции проверяет/сохраняет полный immutable OrderLine manifest и переводит заказ в `preflight_blocked`.
- Переход разрешён только из `pending`, `insufficient_stock` или `preflight_blocked`; если другой worker уже изменил состояние (например, успешно зарезервировал заказ), транзакция откатывается и состояние не перезаписывается.
- Добавлены regression assertions для нехватки обязательных CS2 ресурсов и нехватки полного Steam Cards preflight: в обоих случаях нет DeliveryLeg/активных резервов, а persisted Order status равен `preflight_blocked`.

### Проверка и ограничения

- GitHub branch head после двух последовательных file commits: `7d35d8d02138a10c88f675f3e6eeb338e382d6c0`.
- Production diff и тестовые assertions перечитаны с GitHub.
- Локальные тесты и GitHub Actions для этого HEAD ещё не подтверждены; не объявлять исправление CI-verified.
- C.3 остаётся OPEN: payment sync по-прежнему создаёт PAID Order + manifest до pure planning, а единая транзакция должна охватывать создание/подтверждение Order, manifest и initial reservations. Этот lifecycle gap не закрыт данным исправлением.
- `main` и PR #4 не изменялись/не сливались; реальные Steam/Playerok операции не выполнялись.

---

## C.3 — перенос Recipe manifest в reservation commit (2026-11-25)

**База:** HEAD `e9f04f3430439d448341d695bc520785046c5162`, ветка `work/local-sync`.

### Изменения

- `DeliveryOrderPipeline.prepare_recipe_order()` больше не материализует OrderLine отдельной транзакцией перед планированием. Он получает Recipe из точной ConfigVersion и строит стабильные OrderLine IDs через существующий deterministic ID helper.
- `DeliveryReservationService.reserve_recipe_plan()` теперь вызывает `materialize_recipe_order_lines_in_transaction()` после повторной проверки Order/ConfigVersion и внутри своей `BEGIN IMMEDIATE` транзакции. Создание/проверка полного manifest, ListingStock transfer, DeliveryLeg и canonical reservations выполняются в одной транзакции успешного reservation commit.
- Если pure preflight не обеспечивает обязательные ресурсы, pipeline сохраняет/проверяет полный immutable manifest отдельной короткой транзакцией; DeliveryLeg и физические reservations не создаются.
- Если ListingStock transfer сообщает о недостаточном остатке, reservation transaction откатывается целиком; после rollback сохраняется только manifest, без частичного transfer/DeliveryLeg/reservation.
- Публичный `reserve_recipe_plan()` и его прежние прямые callers сохранены. Steam Cards selection/preflight, legacy CS2 path, ASF boundary и Playerok ConfigVersion fail-closed binding не переписывались.

### Regression coverage

- Добавлен тест успешного typed Recipe reservation: до подготовки OrderLine отсутствуют; после commit manifest, DeliveryLeg и canonical reservation присутствуют.
- Добавлен rollback test: ошибка проверки плана после materialization откатывает OrderLine вместе с транзакцией; Order остаётся pending, DeliveryLeg не появляется.

### Оставшаяся граница C.3

Playerok PAID sync по-прежнему создаёт canonical Order и полный manifest в своей транзакции, когда точная ConfigVersion уже доказана. Этот коммит подтверждает существующий Order в reservation transaction и переносит создание manifest для pipeline-created orders внутрь reservation commit, но не переносит само создание PAID Order из sync: это остаётся отдельным lifecycle gap для дальнейшего решения. Unresolved PAID Deal без точной ConfigVersion по-прежнему не получает Order. Поэтому C.3 остаётся **OPEN**; данный slice не объявляется полным закрытием критерия Order + manifest + initial reservations.

**Проверка:** production code и два regression tests опубликованы в `work/local-sync`. Локальные тесты в этой среде не запускались. Ожидается GitHub Actions для нового HEAD. Реальные Playerok/Steam операции не выполнялись. `main` и PR #4 не сливались.

---

## C.3 — проверка доступных Playerok timestamps для ConfigVersion binding (2026-11-25)

Проверены реальные поля GraphQL query и browser snapshot mapping:

- `integrations/playerok/client.py::fetch_sales()` запрашивает у Playerok для Deal только `id`, `status`, `createdAt`, `item { id name }`, `user { id username }`, `chat { id }`, `transaction { value }`.
- `PlayerokAdapter._map_deal()` сохраняет `createdAt` как `created_at`; отдельного `paidAt`/payment timestamp в запрошенной схеме и mapped deal нет.
- `integrations/playerok/browser_sales.py` использует тот же `PlayerokAdapter._map_deal()`; browser snapshot не добавляет независимого payment timestamp.
- Следовательно, `createdAt` — время создания Deal, а не доказанное время перехода в PAID. Использовать его для выбора ConfigVersion нельзя.
- В текущей схеме sync не хранит историю интервалов действия ConfigVersion и не может восстановить, какая версия была опубликована в момент фактической оплаты.
- Автоматический выбор latest ConfigVersion при первом наблюдении PAID остаётся некорректным: первая синхронизация может увидеть уже существующую оплату после нескольких изменений объявления.

### Безопасная граница следующего изменения

Автоматическое закрепление версии допустимо только при наличии авторитетного payment timestamp/event и истории эффективных периодов ConfigVersion, позволяющей однозначно определить одну версию. В проверенном Playerok read-only API такого timestamp сейчас не получаем. Добавление поля/запроса наугад или трактовка `createdAt` как `paidAt` не допускаются.

Альтернатива — отдельное явное операторское решение о ConfigVersion для конкретной PAID Deal с проверкой принадлежности версии точному listing, записью audit trail и неизменяемым binding после подтверждения. Это меняет бизнес-процесс и пока не подтверждено пользователем; до такого подтверждения не реализовывать выбор версии и не создавать Order из unresolved PAID Deal.

**Production code:** без изменений. **Тесты:** не запускались. **Steam/Playerok mutations:** не выполнялись.

---

## C.3 — проверка безопасного входа PAID Deal без Order (2026-11-25)

**Проверенная точка:** HEAD `2c9f777f1322bb9ef3c683efe271fa3a025b050f`, ветка `work/local-sync`. Проверены `integrations/playerok/sync.py`, `integrations/playerok/listing_config.py`, `core/delivery/playerok_eligibility.py`, `core/delivery/order_pipeline.py`, участки `app/main.py` с загрузкой/выбором/подготовкой заказов и тесты Playerok sync/listing-stock handoff.

### Подтверждённые факты

- Новый PAID Deal намеренно сохраняется без `config_version_id`: `_upsert_deal()` не назначает последнюю версию объявления, потому что имеющиеся данные не доказывают, какая версия действовала в момент оплаты.
- `_maybe_create_order()` создаёт canonical Order только при наличии уже доказанной точной ConfigVersion; стабильный `order_id` вычисляется из Playerok account + external deal ID.
- `resolve_unresolved_deal()` решает только связь с точным объявлением. Для PAID сделки без существующего Order он не закрепляет ConfigVersion.
- Текущий UI `_load_saved_orders()` читает canonical очередь через `gd_orders JOIN gd_deals`; `_prepare_ledger_order()` сначала вызывает `_config_for_order(order_id)`. Поэтому Deal без Order сейчас не виден в canonical списке и не может попасть в текущий preparation handler.
- `_show_order_details()` также требует существующий Order и OrderLines.
- Listing config save создаёт новую immutable ConfigVersion, но проверенные call sites не предоставляют доказательства payment-time version для уже оплаченной сделки.
- Тест `test_paid_playerok_recipe_transfers_listing_stock_only_with_reservation_commit` вручную задаёт `gd_deals.config_version_id` перед вызовом `_maybe_create_order()`; это тестовая фикстура, а не production механизм разрешения версии.

### Решение / блокер для безопасного refactor

Нельзя переносить Order creation в preparation или добавлять PAID Deal без Order в очередь подготовки, пока отсутствует production-механизм, который связывает сделку с **точной** ConfigVersion на основании достоверного свидетельства. Подстановка текущей/последней ConfigVersion запрещена. Простое отображение unresolved PAID Deal возможно как диагностической записи, но не как доступного к подготовке заказа.

Следующий необходимый этап перед изменением UI/pipeline: определить и реализовать источник доказательства payment-time ConfigVersion (либо явный контролируемый операторский выбор уже существующей версии с аудитом и неизменяемой фиксацией, если это соответствует утверждённому процессу). До этого не менять eligibility, не создавать Order/OrderLines для unresolved PAID Deal и не ослаблять fail-closed. Steam Cards, legacy CS2 и ручные заказы остаются вне этого refactor.

**Изменения production code:** отсутствуют. **Тесты:** не запускались. **Внешние Playerok/Steam действия:** не выполнялись.
 
---

## C.3 — перенос ListingStock в reservation commit (2026-11-25)

**База изменения:** HEAD `404290cdac6f26d0cd6d581ffc723febbe9bdfb1`, ветка `work/local-sync`.

- Удалён ранний ListingStock transfer из Playerok PAID sync. Sync сохраняет Deal, Order и полный immutable OrderLine manifest, но не переводит listing allocations на заказ.
- Передача ListingStock остаётся в `DeliveryReservationService.reserve_recipe_plan()`, внутри той же `BEGIN IMMEDIATE` транзакции, что создаёт DeliveryLeg и физические reservations. ASF/network calls не добавляются в транзакцию.
- Обновлён handoff test: до подготовки нет transfer/order allocation; после успешного reservation commit transfer, allocation и DeliveryLeg должны существовать; повторная подготовка не создаёт второй transfer.
- Поведение fail-closed для PAID Deal без точной ConfigVersion не менялось. Steam Cards и legacy CS2 paths не менялись.
- Локальные тесты не запускались в этой среде. CI будет проверен после публикации коммита.

**Статус C.3:** OPEN. Этот перенос закрывает только преждевременный ListingStock handoff. Order + manifest всё ещё создаются при PAID sync отдельно от позднего pure planning/reservation commit; полный атомарный lifecycle C.3 остаётся незавершённым.

---

## C.3 — аудит call sites и тестовой поверхности atomic lifecycle (2026-11-25)

**Проверенная точка:** HEAD `b5d228c87da90b6ecf74bbcc2f0a32b6b5d1f1e0`, ветка `work/local-sync`. Повторно сверены C.3 plan criteria и актуальные callers на этой ветке.

### Call sites

- Основной typed Recipe путь UI: `app/main.py::_prepare_typed_cs2_recipe_order` → `DeliveryOrderPipeline.prepare_recipe_order`. Он использует уже созданный Playerok Order, получает snapshots из локального inventory snapshot store и не вызывает ASF send на этапе подготовки.
- Legacy CS2 путь UI: `app/main.py::_prepare_ledger_order` → `prepare_cs2_order` → `cs2_reservations.reserve_order`. Это отдельный legacy path; не менять его неявно при refactor typed Recipe.
- Steam Cards путь UI: `app/main.py::_prepare_steam_card_order_async` получает seller snapshots и verified buyer diagnostics через ASF вне DB transaction → `prepare_steam_card_order`. Внутри метода уже есть одна транзакция, которая создаёт Steam Cards OrderLine (если отсутствует), DeliveryLegs, canonical + compatibility reservations и переводит Order в reserved. Однако сам Order создан раньше в Playerok sync. Сохранить отдельную карточную бизнес-логику; не объединять её с CS2/general stock.
- Playerok sync: `integrations/playerok/sync.py::_upsert_incremental` → `_upsert_deal` → `_maybe_create_order`. В текущем пути создаются Order и typed Recipe OrderLines, но не DeliveryLeg/reservations.
- Прямые вызовы `reserve_recipe_plan` помимо pipeline обнаружены в тестах `tests/test_recipe_unified_reservations.py`; production call path идёт через pipeline. `reserve_recipe_plan` и reservation service остаются низкоуровневыми публичными entry points, поэтому их поведение нужно сохранить для тестов/совместимости либо документированно адаптировать.

### Тестовая поверхность, которую нельзя потерять

- `tests/test_recipe_unified_reservations.py`: typed Recipe, недостаток обязательных ресурсов, optional rows, Steam Cards full-set preflight, exact resources, ListingStock transfer, idempotency, reservation conflicts, concurrent preparations, fail-closed states.
- `tests/test_multi_game_order_preflight.py`: отдельный Steam Cards flow (multi-game selection, full sets, exact legs/reservations, shortage without partial reserve) и legacy CS2 730/2 path.
- `tests/test_playerok_sync.py`: baseline не создаёт заказы; paid/incremental sync привязывает точную ConfigVersion; повторный sync идемпотентен; invalid/ineligible config не должен создавать order/lines/reservations.
- `tests/test_playerok_listing_stock_handoff.py`: оплаченный event и ListingStock handoff; проверяет reserve/transfer и повторный preparation.
- `tests/test_delivery_reservation_service.py`: атомарные reservation-service инварианты и отказные сценарии.

### Вывод для минимального refactor

1. Не переносить ASF/Steam вызовы внутрь транзакции. Snapshot и verified metadata должны быть получены заранее.
2. Не менять UI/handler маршрутизацию и не объединять Steam Cards с CS2.
3. Typed Recipe path — первый и основной scope для закрытия конкретного C.3 разрыва. Сохранить стабильный order ID от Playerok deal и exact ConfigVersion binding.
4. Paid Deal и ConfigVersion должны оставаться надёжно зафиксированными sync-процессом. Создание Order/OrderLines/manifest и initial reservations для успешной подготовки typed Recipe перенести в один короткий transaction commit после pure planning. Для shortage/failed preflight сохранить Order + полный manifest в статусе `preflight_blocked`, но без DeliveryLeg и physical reservations; эта запись также должна быть атомарной.
5. Текущие `prepare_cs2_order` и `prepare_steam_card_order` — отдельные compatibility paths. Не переписывать их в этом изменении без подтверждения, что конкретный acceptance case C.3 требует их включить; отдельно проверить и зафиксировать, что они не оставляют reservation без Order/manifest при сбое.
6. Тесты для нового commit boundary должны проверять rollback после создания Order, после OrderLine materialization, во время ListingStock transfer, после первого из нескольких reservation inserts; повторный preparation; недостаток ресурсов; parallel preparation; ConfigVersion/eligibility mismatch. Старые тесты прямого `reserve_recipe_plan` не удалять.

**Статус:** аудит callers завершён на уровне доступных production call sites и основных тестовых модулей. Production code не менялся; тесты не запускались; live Steam/Playerok actions не выполнялись. Следующий кодовый шаг — минимальный typed Recipe refactor, начиная с transaction-aware helper для создания Order + OrderLines и reservation commit, затем целевые тесты и GitHub Actions.

---
## C.3 — уточнение Order → preflight → reservation lifecycle (2026-11-25)

**Проверенная точка перед анализом:** HEAD до этой записи `86d2e2c197c953bcf52ed93746fc0225a7d3aee3`, ветка `work/local-sync`. План C.3 перечитан непосредственно из `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`.

### Подтверждённый фактический call path

1. `PlayerokSyncService._upsert_incremental()` открывает `BEGIN IMMEDIATE`, вызывает `_upsert_deal()`; при статусе PAID `_maybe_create_order()` вставляет `gd_orders(status='pending')` и вызывает `DeliveryLedger.materialize_recipe_order_lines_in_transaction()`. Deal + Order + OrderLines фиксируются в одной sync-транзакции (`integrations/playerok/sync.py:400-430,315-372`).
2. Позднее вызывается `DeliveryOrderPipeline.prepare_recipe_order()` (`core/delivery/order_pipeline.py:386-631`). Он получает уже существующий Order, отдельно вызывает `ledger.materialize_recipe_order_lines()` (собственная отдельная `BEGIN IMMEDIATE`, строки 411-413), затем читает Recipe/OrderLines, анализирует переданные snapshots и verified buyer/card metadata, строит полный план ресурсов и только после этого вызывает `reserve_recipe_plan()`.
3. `DeliveryReservationService.reserve_recipe_plan()` открывает собственную `BEGIN IMMEDIATE` (`core/delivery/reservation_service.py:520-541`), перепроверяет Order/ConfigVersion/OrderLines, конфликты ресурсов, listing allocations и Steam Cards quota; затем записывает DeliveryLeg, canonical reservations/compatibility projection и статус Order='reserved' в этой транзакции (строки 888-1025).

### Вывод относительно C.3

- Атомарность уже обеспечена **внутри** reservation commit: если запись leg/reservation/claim/transfer завершается ошибкой, транзакция откатывается; частичного набора резервов не остаётся.
- Но буквальный критерий C.3 «создание заказа, manifest/order assets и первоначальных reservations в одной транзакции» сейчас **не выполнен**: Order и OrderLines уже закоммичены при Playerok sync, а inventory planning и initial reservation выполняются позже. Также `prepare_recipe_order()` явно документирует отдельную materialization transaction.
- Важная граница: ASF inventory snapshots и verified buyer/card metadata передаются в `prepare_recipe_order()` уже снаружи DB transaction; выбор ресурсов производится до reservation transaction. Не следует переносить сетевые ASF вызовы внутрь SQLite transaction. Preflight/selection должен остаться до короткой DB transaction, а transaction должна повторно проверить все изменяемые условия и зафиксировать Order + manifest + reservations вместе.
- Оплаченная сделка не должна теряться при нехватке ресурсов: запись Playerok Deal сохраняется; в той же короткой транзакции создаётся Order + manifest со статусом `preflight_blocked` без физического резерва. Когда полный preflight готов, отдельная атомарная операция должна создавать/обеспечивать Order + manifest + все initial reservations в одном commit. Никакой частичный резерв не допускается.

### Минимальный целевой дизайн (пока только проектное решение, код не менялся)

1. Сохранить надёжную фиксацию Playerok Deal/PAID event и его точной ConfigVersion в Sync; не вызывать ASF из Sync и не держать SQLite transaction во время сети.
2. Разделить чистый planning и DB commit. Planning получает уже проверенные свежие snapshots и verified buyer/card inputs, строит полный immutable plan без записи reservations. Для планирования до появления OrderLine использовать стабильные ID, вычисляемые из `(order_id, config_version_id, recipe_item_id)`, либо временные ключи RecipeItem с однозначным преобразованием в эти ID.
3. В одной `BEGIN IMMEDIATE` транзакции: проверить paid Deal + точную ConfigVersion + eligibility + quota + актуальные ListingStock allocations/состояния; создать Order (если ещё нет), материализовать все OrderLines/manifest, проверить полный plan и глобальные reservation conflicts; атомарно перевести ListingStock allocations; создать все DeliveryLeg + canonical reservations + необходимые legacy projections/Steam Cards claims; перевести Order в `reserved`. Любая ошибка откатывает весь этот commit.
4. Если полный planning не готов/ресурсов недостаточно: в одной транзакции сохранить Order + полный manifest со статусом `preflight_blocked`, без DeliveryLeg и без физических reservations. Повторная подготовка должна быть идемпотентной и переходить в резерв только после нового полного planning.
5. Перед реализацией проверить все callers `prepare_recipe_order`, `reserve_recipe_plan`, `prepare_steam_card_order`, `prepare_cs2_order` и существующие sync/order tests. Нельзя ломать legacy callers, карточный отдельный flow, listing-stock handoff, quota или статусную модель. Вероятный минимальный технический шаг — вынести внутренний helper, принимающий уже открытую SQLite connection, из `reserve_recipe_plan()`, а создание Order/OrderLines подключить к этому же commit; конкретную форму выбрать только после аудита всех call sites.
6. Добавить rollback/idempotency tests: crash/failure до и после materialization, недостаток ресурсов, повторный paid event, повторный preflight, два параллельных preparation, изменение ConfigVersion/eligibility между planning и commit, конфликт AssetID, listing stock transfer, Steam Cards quota, legacy projection. Внешний ASF network call не входит в транзакцию.

**Статус:** C.3 всё ещё открыт. Это доказанный разрыв с буквальным acceptance criterion, а не повод переписывать ledger или reservation logic. Следующая задача — проверить все call sites и тесты, затем подготовить минимальный refactor-патч с сохранением текущих публичных API. На этом шаге код и тесты не менялись/не запускались; Steam/Playerok live actions не выполнялись.

---
## C.3 — первый аудит acceptance criteria по исходникам (2026-11-25)

**Проверенная точка:** ветка `work/local-sync`, HEAD `5ad89238e73ef1addccd22542e04b5d390f657f0` (документальный коммит с единым рабочим планом). План C: `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`, blob SHA `06b764c1706e2989d814f9395a8a73682c486261`. Изучены актуальные исходники `core/delivery/ledger.py`, `listing_stock.py`, `stock_ledger.py`, `reservation_service.py`, `recovery.py`, `reconciliation.py`, `order_pipeline.py`, `core/database/ledger_migrations.py`, `integrations/playerok/sync.py` и соответствующие тесты. В этой записи только аудит: production code и пользовательская БД не менялись; Steam/Playerok live actions не выполнялись.

### Сверка критериев C.3

| Критерий плана | Подтверждение в коде/тестах | Статус аудита |
|---|---|---|
| Инвариант остатка при параллельных резервах объявлений/заказов | `QuantityStockLedger` использует `BEGIN IMMEDIATE`, blocking allocations и DB triggers; `tests/test_quantity_stock_ledger.py` содержит параллельный overbooking test; `tests/test_listing_stock_service.py` — concurrent listing allocations и concurrent paid-event transfer | Реализовано и покрыто тестами |
| Нет отрицательного остатка и сверхвыделения | DB constraints/triggers проверяют aggregate blocking units против observed amount; тесты `test_multiple_allocations_share_one_stack_without_exceeding_amount`, `test_update_cannot_increase_aggregate_allocations_over_observed_amount`, `test_lower_inventory_observation_marks_conflict_without_dropping_allocations` | Реализовано и покрыто тестами |
| Один AssetID не может одновременно принадлежать нескольким listing generations/orders/unknown sends | Глобальные canonical reservation keys/indexes, stock allocation uniqueness/guards, legacy Steam reservation bridge; tests `test_active_resource_reservation_is_globally_unique_per_bot_and_asset`, `test_parallel_orders_cannot_reserve_same_physical_asset`, `test_concurrent_listing_allocations_cannot_overbook_one_physical_stack` | Реализовано на уровне canonical ledger; legacy conflict paths сохраняются fail-closed |
| Listing allocation передаётся в заказ атомарно и один раз | `ListingStockService.transfer_to_order` / `_transfer_to_order_in_transaction`; immutable `gd_stock_transfers`; caller transaction поддерживается; tests `test_concurrent_paid_events_cannot_transfer_one_listing_slice_twice`, `test_transfer_is_idempotent` (см. полный набор в listing-stock tests), `test_transfer_can_join_callers_transaction_and_rolls_back_with_it`, Playerok handoff test | Реализовано и покрыто тестами |
| Повтор события/операции не меняет результат повторно | Idempotency IDs для allocation batches, transfers, listing adjustments, Playerok deal/order; tests на повторное transfer/preparation и adjustment | Реализовано для проверенных listing-stock / sync путей |
| Существующие заказы, карточные резервы и отправки переживают миграцию | Additive migrations/backfill и compatibility bridge между `steam_asset_reservations` и `gd_resource_reservations`; `tests/test_delivery_ledger.py` проверяет legacy orders, delivery orders и reservations preservation/import | Реализовано и покрыто тестами миграции |
| Backup/rollback и миграция на копии БД | `ensure_pre_migration_backup` делает SQLite online backup, sanitizes secrets, проверяет `integrity_check`; migrations используют транзакции и rollback; `tests/test_quantity_stock_ledger.py` проверяет backup restore на синтетической копии и DDL rollback при конфликте trigger | Реализовано и покрыто тестами на synthetic copy; production DB пользователя не проверялась и не изменялась |
| Общий пул 100 единиц; allocations 3/5/10 → 18 занято, 82 свободно; при продаже ровно N переходит в заказ | Listing-stock tests покрывают shared stack allocation, точное количество, cross-bot selection, атомарный handoff и отсутствие двойного учёта | Реализовано и покрыто тестами |

### Незакрытые пункты C.3, подтверждённые аудитом

1. **Атомарная граница Order + OrderLine/manifest + initial reservations не соблюдена целиком.** В Playerok sync `_maybe_create_order` создаёт Order и материализует OrderLines внутри одной транзакции `_upsert_incremental` (`integrations/playerok/sync.py`, `DeliveryLedger.materialize_recipe_order_lines_in_transaction`). Но подбор/резервирование ресурсов и создание DeliveryLeg выполняются позднее отдельной транзакцией `DeliveryReservationService.reserve_recipe_plan`. Это означает, что Order/OrderLines уже могут существовать до первоначального resource reservation. Требование C.3 в разделе «Обязательное уточнение: lifecycle резервов…» требует одной транзакции для создания заказа, manifest/order assets и первоначальных reservations. Нужно отдельно спроектировать безопасную границу с учётом того, что ASF inventory/preflight выполняются до DB commit; пока считать критерием открытым, не менять без отдельного анализа.
2. **Операторский интерфейс ручного override не подключён.** `DeliveryRecoveryService.list_active_reservations` и `manual_release_reservation` существуют; тесты покрывают unknown override, InEscrow second confirmation, accepted→consumed, orphan visibility/removal, Steam Cards snapshot invalidation и append-only audit. Но метод сервиса принимает `steam_status` и related offer IDs от вызывающего слоя и сам не выполняет ASF status lookup/identity verification. Никакой пользовательский интерфейс/Telegram action к этим методам в текущем C.3 checkpoint не подтверждён. Пользователь запретил отдельную операторскую панель; операторский интерфейс должен быть подключён на Telegram-этапе. До этого backend service tests не считать завершённым операторским workflow.
3. **Полная проверка внешних Steam offer lifecycle boundaries ещё не завершена как единый acceptance run.** В коде есть `DeliveryReconciliation` для UNKNOWN/SENT_TO_STEAM, startup recovery переводит DISPATCHING в UNKNOWN; `DeliveryRecoveryService` ограничивает retry/release проверками последней terminal attempt, recipient, offer absence и свежего inventory. Тесты покрывают ключевые cancel/retry/release сценарии. Однако нужен отдельный остаточный audit matrix по нескольким legs/нескольким offer IDs, точному bot/recipient/asset manifest matching и статусам ASF, чтобы не считать набор unit tests автоматическим доказательством всех combinations.
4. **Удаление заказа и orphan visibility:** hard-delete ограничен FK/append-only schema; orphan/legacy reservations перечисляются сервисом. Но операторская видимость/разбор этих записей не подключены к утверждённому каналу управления (Telegram, не отдельная панель). Это остаётся интерфейсным acceptance item.

### Текущее решение по C.3

- Подтверждённые части C.3 не переписывать.
- Следующая работа остаётся внутри C.3 и должна идти по одному из открытых пунктов выше; сначала подготовить технический разбор атомарной границы Order → preflight → reservations, не меняя код.
- Отдельную операторскую панель не создавать. Подключение оператора к audit/recovery выполнять на Telegram-этапе с ASF-проверкой статуса и точной привязкой reservation/offer.
- C.3 пока **не закрыт**. Нельзя перескакивать на C.4/C.5/C.6 до закрытия согласованных C.3 acceptance gates.
- CI evidence, уже записанные выше для HEAD `4b1f6582...` и `b1bd50e...`, относятся к тем конкретным SHA; текущий HEAD `5ad8923...` изменил только документацию. Для этого аудита тесты повторно не запускались, так как production code не менялся.

**Следующее действие:** разобрать без изменений кода атомарную границу Playerok paid Deal → Order/OrderLine → ASF preflight → resource reservation/DeliveryLeg и предложить минимальный вариант, который выполняет C.3 invariant без потери заказа при нехватке ресурсов и без резерва до проверки полного состава.

---

## Рабочий план завершения GameDelivery — зафиксировано 2026-11-25

**Назначение записи:** прекратить параллельное выполнение разрозненных задач и вести разработку по одной последовательности до завершения программы. Эта запись фиксирует рабочий порядок и правила управления задачами; она не объявляет незавершённые блоки закрытыми и не заменяет критерии приёмки разделов C.1–C.11.

### 1. Правило актуального состояния проекта

- Единственный обязательный источник текущего состояния перед началом каждой новой задачи — этот файл `PROJECT_MASTER_LOG.md` в GitHub, ветка `work/local-sync`.
- Перед началом работы перечитать актуальную верхнюю часть журнала и относящиеся к задаче разделы, а также соответствующий план `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`.
- Сверить фактический HEAD ветки, рабочее состояние, связанные файлы, тесты и CI. Не полагаться на старые сообщения чата или память как на более авторитетный источник, чем актуальный журнал и код.
- После каждого подтверждённого существенного изменения записывать сюда дату, точный результат, затронутые области, commit SHA, тесты/CI и оставшиеся ограничения.
- В начале каждой следующей рабочей сессии сначала восстановить позицию по этому журналу. Не просить пользователя повторно объяснять уже зафиксированные требования.
- Если журнал, план и код противоречат друг другу, остановиться, выявить расхождение и зафиксировать его; не выбирать трактовку самостоятельно и не переписывать бизнес-правила.

### 2. Что уже существует — не начинать заново

По истории реализации и CI в этом журнале проект уже содержит существенные части следующих подсистем:

- ASF Generic integration и собственный `GameDelivery.ASFPlugin`, включая существующие механизмы preview/send и обработку результата.
- Игровой каталог, парсер и снимки инвентарей, `GameInventoryStore` и существующий интерфейс инвентаря.
- Существующие игровые/CS2 модели и селекторы, AppID 730 / ContextID 2, часть логики Listing Stock и распределения ресурсов.
- Отдельную Steam Cards подсистему с inventory snapshots, badge/set analysis, selection, reservations и preflight.
- Playerok adapter/browser-related workflow, Safe Sync, baseline, deduplication и разделение Deal ID / Chat ID / Listing ID.
- Delivery orders/lines, listing stock allocations, canonical reservations, delivery legs/attempts, ledger/reconciliation и SQLite migrations.
- Ручной reservation override/orphan audit service и durable Telegram outbox.
- Windows packaging/EXE smoke-test pipeline и GitHub Actions Tests / Windows Build.

Наличие этих компонентов не означает автоматического закрытия всей функции. Для каждого пункта отдельно проверять: код существует; подключён к реальному call path/UI; покрыт тестами; прошёл CI на конкретном SHA; прошёл требуемую безопасную live-проверку. Уже реализованное повторно не переписывать без выявленного дефекта или конкретного незакрытого критерия.

### 3. Последовательность завершения — один этап за раз

**Этап 1 — закрыть C.3: ledger, listing stock, reservations и recovery.**
- Провести точную сверку каждого acceptance criterion C.3 с кодом и тестами.
- Не переписывать уже работающие миграции, listing stock → order handoff, атомарные резервы и audit.
- Выявить только реальные незакрытые сценарии: cancellation/failure boundaries, order/reservation lifecycle, orphan/unknown recovery, backup/rollback, отсутствие скрытых резервов.
- Ручное снятие резерва должно оставаться отдельным локальным override с предупреждением, аудитом и последующим обновлением snapshot.
- Не создавать отдельную операторскую панель. Управление оператором планируется через Telegram в соответствующем этапе.
- Закрыть C.3 только после выполнения его acceptance criteria и фиксации доказательств.

**Этап 2 — закрыть C.4: единый исполняемый delivery path.**
- Уже добавленные `DeliveryHandlerV1` / `DeliveryHandlerRegistry` считать фундаментом, а не завершённым диспетчером.
- Проверить, чего не хватает для фактического dispatcher, попыток, parent order + delivery legs, idempotency, partial result, UNKNOWN/reconcile и restart recovery.
- Реализовывать только минимальные необходимые части, чтобы подключить существующие способы доставки.
- Завершить contract/regression tests и зафиксировать критерии.

**Этап 3 — закрыть C.5: подключить существующую Steam Cards доставку.**
- Не переписывать существующий Steam Cards flow.
- Сохранить согласованные правила: пользователь указывает для каждого Playerok объявления только общее числовое количество полных наборов; физические карточки, AssetID, разбиение по играм и ботам в настройках объявления не задаются.
- Сохранить автоматический подбор по существующим правилам, точные asset ownership, preview → live preflight → подтверждение → ASF send → offer/result, reservations и ledger sync.
- Foil cards/foil badges не участвуют. Не использовать buyer inventory как вход allocator-а; проверять обычный badge progress покупателя, состав обычного набора по подтверждённому обычному значку наших ботов и seller inventories наших ботов согласно утверждённому C.2.9.
- Закрыть точное сопоставление типов карточек, stale/unknown gates, idempotency, cancellation и unknown outcomes.
- Live-тестирование только отдельно на тестовом аккаунте и после явного разрешения.

**Этап 4 — закрыть существующую доставку игровых предметов / CS2 (C.6).**
- Это не новый проект и не повторное начало CS2: переиспользовать существующие каталог, парсер, inventory snapshots, selectors, Listing Stock, UI и ASF plugin.
- Не менять карточную подсистему и не смешивать Steam Cards 753/6 с игровыми активами.
- Проверить фактическую transfer capability ASF для AppID 730 / ContextID 2.
- Подключить существующий игровой путь к общему delivery contract; обеспечить exact asset identity, свежий snapshot, tradable gate, точный quantity, распределение между ботами/trade legs, attempts/reconcile и безопасное восстановление.
- Никаких live offers без отдельного разрешения.

**Этап 5 — завершить Playerok workflow.**
- Сохранить уже реализованные Safe Sync, baseline, deduplication и раздельные deal/chat/listing identities.
- Закрыть только недостающие части цепочки: точная привязка payment-time ConfigVersion, заказ и доставка, внутренний browser workflow, Trade URL, сообщения/статусы и безопасная обработка неизвестного результата.
- Не запускать автоматические действия с объявлениями, отправку сообщений или status mutation без соответствующего проверенного этапа и явного разрешения.

**Этап 6 — Telegram как канал оператора.**
- Переиспользовать существующий durable Telegram outbox и внутренние delivery events; не создавать второй источник состояния заказа.
- Реализовать настройки, защищённое хранение секретов, proxy support после проверки, connection test и test message, event notifications, allowlist, audit, deduplication/retry и безопасные operator commands.
- Telegram outage не должен останавливать Playerok sync или delivery.
- Не создавать отдельную операторскую панель вместо согласованного Telegram-интерфейса.

**Этап 7 — остальные категории fulfillment по плану.**
- C.7 цифровые ключи/коды.
- C.8 файловая доставка.
- C.9 услуги и ручное исполнение.
- Для каждой категории отдельно определить реальную передачу, резервирование, audit, idempotency и честные статусы automated / operator-assisted / unsupported.

**Этап 8 — C.10/C.11: итоговый интерфейс, интеграция, регрессия и приёмка.**
- Свести существующие экраны и подсистемы в целостный рабочий поток без потери текущих функций.
- Проверить UI, настройки объявлений, inventory views/filters, orders, reservations, reconciliation, Telegram и упаковку.
- Выполнить полную регрессию Tests, Windows package/smoke test и ASF plugin build.
- Миграции проверять на копии базы; live-проверки выполнять только на тестовых ресурсах и после отдельного разрешения.
- Этап C не объявлять завершённым только на основании зелёного CI: нужны все требуемые acceptance и live gates.

### 4. Обязательный рабочий протокол

1. Один этап за раз. Не переходить к следующему, пока текущий не закрыт по критериям.
2. Внутри этапа — одна конкретная задача за раз; не распыляться на параллельные функции.
3. Перед изменением — коротко назвать точную цель, разрешённые файлы/область и критерий готовности.
4. Сначала изучить существующий call path и тесты; затем TDD и минимальное изменение.
5. Не повторять уже успешные диагностики без причины.
6. После изменения — целевые тесты, нужная регрессия, затем CI на точном SHA, если он требуется.
7. Записать результат в этот журнал и остановиться на границе задачи/этапа.
8. Не заявлять «готово», если функция существует только как отдельный модуль, не подключена к рабочему пути, не проверена или имеет незакрытые обязательные gates.
9. Не придумывать новые пользовательские настройки, сущности, роли, склады, per-bot selection или правила распределения. Следовать зафиксированным требованиям.
10. Не менять `main`, не сливать PR #4 без прямого разрешения. Рабочая ветка — `work/local-sync`.
11. Не выполнять реальные Steam trades, Playerok публикации/сообщения/status mutations без отдельного разрешения.

### 5. Текущая рабочая позиция на 2026-11-25

- В работе находится C.3. Последние зафиксированные изменения затрагивают listing stock → order handoff, SQLite migration safety и manual reservation override/orphan audit.
- На HEAD `4b1f6582a5b3be4ac3e8fa88f998a95092b2d72d` Tests run `45657277445` и Windows Build run `45657277545` прошли успешно для manual reservation override/orphan audit. Более ранний listing-stock handoff CI на HEAD `b1bd50e75fd4ab0eb57e392e6ebad913765d3654`: Tests run `45654999701` и Windows Build run `45654999695` прошли успешно (1,062 tests).
- Это не доказывает закрытие C.3 целиком: UI/operator connection и оставшиеся lifecycle/failure-recovery acceptance items открыты.
- C.4: контракт и registry добавлены, но фактическое подключение полного dispatcher/attempt/recovery path не подтверждено как завершённое.
- C.5: Steam Cards listing quota реализована и проверена CI, но подключение полного существующего Steam Cards flow к общей delivery system и оставшиеся live/technical gates ещё требуют аудита.
- C.6: существующие inventory/CS2 компоненты сохраняются; готовность полного delivery handler и ASF transfer acceptance не объявляется без проверки.
- Последняя известная зафиксированная позиция: сначала завершить аудит и закрытие C.3; не начинать C.4/C.5/C.6 параллельно.
- На момент этой записи точный HEAD ветки отдельно не подтверждён этим checkpoint; перед следующими изменениями обязательно получить актуальный HEAD и состояние CI.
- `main` и PR #4 не изменять.

**Следующее действие:** провести детальную таблицу C.3 acceptance criteria → существующий код → тесты/CI → статус → конкретный остаток. Не менять код до завершения этой сверки.

---

**C.3 Manual reservation override and orphan audit (2026-11-25):** Audited `DeliveryRecoveryService` against the C.3 lifecycle requirements and found the existing `release()` only handled linked ACTION_REQUIRED orders/legs after terminal attempt, recipient verification, confirmed offer absence, and fresh inventory. Added an additive `gd_reservation_manual_actions` append-only audit table without order/leg foreign keys so manual actions remain recordable for orphaned/legacy reservations. `Database.initialize()` and standalone ledger migration now create a pre-migration backup for this migration. Added `list_active_reservations()` to enumerate canonical reservations and quantity-stock allocations, including legacy/orphaned entries, and `manual_release_reservation()` for explicit local-only override by exact reservation/allocation ID. It requires actor, reason, reason code, and confirmation; active, CreatedNeedsConfirmation, InEscrow, and unknown Steam states require a separate second override confirmation with an explicit warning that local release does not cancel the Steam offer. Accepted is recorded as `consumed`, never returned as free. Quantity-stock observations are marked stale; Steam Cards snapshot metadata is invalidated for the affected bot. Added tests for unknown override, InEscrow second confirmation, accepted→consumed, orphaned legacy reservation visibility/removal, Steam Cards snapshot invalidation, and append-only audit. Commits: migration `2af08340`, backup wiring `870c5aa3` / `9a3842fd`, service `2681473d` / `f38de8a7` / `2ecf7f82` / `f980d406` / `afe83703`, tests `5e25ace0` / `738be970` / `d6d4a290` / `1fbf06b9`. CI verification completed successfully on HEAD `4b1f6582a5b3be4ac3e8fa88f998a95092b2d72d`: Tests run `45657277445` and Windows Build run `45657277545` both passed, including the full unit suite, isolated-module fallback, packaged executable smoke test, and artifact upload. **The UI/operator screen is not yet connected to these service methods**, so the C.3 UI acceptance item remains open; no claim of full completion. No Steam offers or Playerok actions were performed. Work remains on `work/local-sync`; `main` and PR #4 untouched.

**C.3 Listing Stock → Order handoff CI verification (2026-11-25):** On branch `work/local-sync`, HEAD `b1bd50e75fd4ab0eb57e392e6ebad913765d3654`, GitHub Actions Tests run `45654999701` completed **success**: Python compilation, credential/migration checks, delivery reconciliation/operator recovery, Steam trade UI flow, full unit suite (**1,062 tests in 42.281s**), and isolated-module fallback all passed. Windows Build run `45654999695` completed **success**: compile, Windows trade-secret ACL hardening, GUI executable build, packaged smoke test, package metadata/instructions, and artifact upload. The immediately preceding correction `1dcdfcf58e83990169853b8e9dfbed39757a4215` adjusts the regression assertion to inspect listing remainder by `config_version_id` and transferred order allocations by exact `order_line_id`; no production logic changed in that correction. Existing Listing Stock tests also cover shared physical stock 100 units split as listing quantities 3/5/10 (free remainder 82), concurrent listing allocation overbooking prevention, concurrent paid-event transfer of one listing slice, transfer idempotency, caller-transaction rollback, stale observations, typed RecipeItem pinning, and exact cross-bot selection without user bot selection. This CI success does not by itself close every C.3 lifecycle acceptance item (notably broader cancellation/recovery/manual operator override and all failure-recovery boundaries); work remains in C.3. No live Steam offers or Playerok actions were performed. `main` and PR #4 remain untouched.

**C.3 Listing Stock → Order allocation assertion correction (2026-11-25):** Tests run `45653909618` on HEAD `e388b21c7f2b18340ab11922f254b812f1073316` completed the full 1,062-test suite with one failure in `test_paid_playerok_recipe_transfers_listing_stock_through_order_preflight_atomically`. The transfer and DeliveryLeg assertions passed; the remaining allocation assertion queried all owners by listing `config_version_id`, but the canonical transfer deliberately clears listing/config ownership on order-owned allocations and binds them to `order_line_id`. Thus the test query excluded the expected order allocation row. Corrected the assertion to query listing-owned remainder by `config_version_id` and order-owned transferred units by the exact `order_line_id`, preserving the expected active listing 4 + released listing 6 + active order 2 accounting. Test-only correction commit `1dcdfcf58e83990169853b8e9dfbed39757a4215`; no production delivery logic changed. CI verification pending. Windows Build on parent HEAD succeeded. No Steam offers or Playerok actions were performed; work remains on `work/local-sync`; `main` and PR #4 untouched.

**C.3 Playerok listing-stock handoff: stock-backed reservation bot-pool guard (2026-11-25):** The corrected handoff regression fixture then exposed a production-path issue in `core/delivery/reservation_service.py`: `reserve_recipe_plan` rejected an otherwise valid listing-stock-backed Steam Asset line when neither the RecipeItem nor ConfigVersion declared an explicit bot pool. That is incorrect for this path because listing allocation already owns the exact resources and their bot identities; requiring a separate user-configured bot list would reintroduce per-bot selection and block the shared listing stock contract. Narrowly changed the guard so an empty bot pool remains rejected for ordinary unbacked Steam Asset plans, while a line whose resources all carry listing stock allocation IDs may proceed under those exact allocations. The existing regression `tests/test_playerok_listing_stock_handoff.py` exercises the no-explicit-bot-pool case and verifies exact transfer, DeliveryLeg creation, and repeat-preparation idempotency. Production change commit `52e1b1bfb25b577dac540ee093710433146942a8`. Tests on parent commit `38874d15e713b301759de0c08dff12be6a6d7d48` still failed at this guard; Windows Build succeeded. CI verification of this narrow production correction is pending. No Steam offers or Playerok actions were performed. Work remains on `work/local-sync`; `main` and PR #4 were not touched.

**C.3 Playerok listing-stock handoff regression fixture correction (2026-11-25):** GitHub Actions on HEAD `57c1bd15155af861d52cbbab0753c5de5ca4ee17` reported Tests run `45651528884` failed while Windows Build run `45651528898` succeeded. The full suite ran 1,062 tests and exposed one error in `tests/test_playerok_listing_stock_handoff.py`: the fixture assumed a newly observed PAID Deal automatically creates an Order using the latest listing ConfigVersion. Production sync intentionally fails closed because PAID status alone does not prove which ConfigVersion was active at payment time; it leaves the deal unbound and creates no Order. Corrected the test fixture to assert that no Order exists before explicit binding of the exact payment-time ConfigVersion, then invoke the existing canonical order-materialization path and verify listing-stock transfer through OrderLine into DeliveryLeg, including repeat-preparation idempotency. Test-only change, commit `a1d9fb34aa7271cf8ba9861c4bd204469bb0ac2e`; no production delivery logic changed. CI verification for this correction is pending. No Steam offers or Playerok actions were performed. Work remains on `work/local-sync`; `main` and PR #4 were not touched.

**C.3 SQLite quantity-stock migration verification update (2026-11-25):** Corrected the rollback regression test placement: `test_quantity_stock_migration_rolls_back_partial_ddl_on_trigger_conflict` is now inside the discovered unittest class, before the `unittest.main()` guard (commit `0784d7aaf7e0b80ee0fa7cdefc5b0b3ec83834be`). Added `test_quantity_stock_migration_runs_on_database_copy_and_backup_is_restorable` (commit `af26869ba5a91be6976bbb31948f88e454e766d0`): it migrates a synthetic copy of a legacy SQLite database, verifies the source database remains unchanged, checks the pre-migration backup with `PRAGMA integrity_check`, restores that backup into a separate database, and verifies restored integrity and legacy data. GitHub Actions on `af26869ba5a91be6976bbb31948f88e454e766d0`: Tests run `45645247283` completed **success** (including full unit suite and isolated-module step); Windows Build run `45645247370` completed **success** (EXE build, packaged smoke test, package metadata/instructions, artifact upload). This is synthetic-copy evidence only; no user production database was accessed or modified. These checks do not close the remaining C.3 listing stock allocation lifecycle/atomic transfer acceptance criteria described below. No production delivery logic changed; no Steam offers or Playerok actions were performed. Work remains on `work/local-sync`; `main` and PR #4 were not touched.

**C.3 SQLite quantity-stock migration rollback regression (2026-11-25):** Added regression test `test_quantity_stock_migration_rolls_back_partial_ddl_on_trigger_conflict` in `tests/test_quantity_stock_ledger.py`. The test creates a pre-existing trigger-name conflict, runs the quantity-stock migration, verifies that the migration raises the expected SQLite error, and confirms that all schema objects created earlier in the failed migration (stock tables and migration marker) are rolled back while the original conflicting trigger remains intact. Commit: `952b50e491293541987eb54ba1bfcefbd4eddf5b`. GitHub Actions Tests run `45643046969` completed with **success**; compile step, focused credential/migration and delivery recovery/UI checks, full unit test suite, and isolated test-module fallback all passed. This is regression evidence for transactional DDL rollback, not proof of every backup/restore scenario against a copy of the user's production database. No Windows Build was triggered for this test-only change. No live Steam offers or Playerok actions were performed. Work remains on `work/local-sync`; `main` and PR #4 were not touched.

**CURRENT AUTHORITATIVE REQUIREMENT — Steam Cards listing quantity (2026-11-24):** For each Playerok Steam Cards listing, the user enters one number: the total number of complete sets available in that listing (for example, Listing A = 10 sets, Listing B = 5 sets). The user does not configure a warehouse, physical cards, AssetIDs, per-game distribution, per-bot allocation, or card-to-listing assignments. At order time, the existing Steam Cards flow checks the buyer's badge progress and inventory, determines the required cards/sets, and automatically selects them from available bot inventories. The listing-wide set limit is enforced atomically when the order's existing verified card selection is reserved; a pre-dispatch cancellation releases that count. The existing per-order quantity and buyer-specific selection behavior remain unchanged. This note supersedes all earlier Steam Cards notes below that describe a physical listing-owned card pool, a game-by-game 5+5 split, per-bot allocation, or a need for the user to select specific cards. Those interpretations were incorrect and must not guide implementation.

**Implementation scope:** The listing limit is a numeric quota only. It is not a stock of specific physical cards. The system may reserve actual cards internally for an order to prevent duplicate delivery, but that is not part of the user's listing configuration. No Steam offers are sent by preflight or quota reservation.

**Steam Cards audit follow-up — preserve the dedicated card subsystem (2026-11-24):** Extended the audit to `core/delivery/stock_ledger.py`, `games/steam_cards/asf_inventory.py`, `games/steam_cards/asf_sync.py`, `games/steam_cards/snapshot.py`, `games/steam_cards/reservations.py`, `games/steam_cards/shared_sets.py`, and `games/steam_cards/owned_selection.py`. Confirmed that `QuantityStockLedger.observe_snapshot` explicitly rejects games whose `item_domain == 'trading_cards'`; Steam Cards are intentionally kept out of the CS2/general quantity-stock ledger. Their authoritative inventory data is in the dedicated `steam_inventory_snapshot` store, including per-bot ownership, tradability, foil status, MarketHashName, game name, and game AppID. Therefore Steam Cards remain in their dedicated 753/6 inventory and reservation subsystem, separate from the CS2/general quantity-stock observation table. The listing-level feature is a numeric complete-set quota only; it does not create a physical card pool or assign AssetIDs to listings. Also flagged a set-composition boundary for tests: `find_shared_game_sets` documents cross-bot aggregation, while the canonical reservation tests require each complete set to be deliverable by one bot; the current canonical reservation validator enforces one-bot-per-set; preserve this existing technical behavior unless a separate engineering change is reviewed and tested. It is not a user-configurable allocation requirement. No code changed in this follow-up. No product-contract question remains: the user configures one total complete-set count per Playerok listing, with no per-game split or physical card assignment.

**Steam Cards listing quantity limit implementation (2026-11-24):** Implemented on `work/local-sync`. Commits: `803404ff2a26596b47d7b6827ae1defed20271f7` (listing quota schema, UI, configuration API, atomic order claims, cancellation release, focused tests), `33f9540713015198b2b82fbf4b4edb611c0da53f` (scope quota enforcement to Playerok listing orders), and `4205d6f85729fe69ba62e149ff0ea4d782b404c2` (align Playerok Steam Cards test fixtures with required listing quota). The Playerok editor exposes one listing-wide total set count for Steam Cards. The existing per-order quantity and buyer-specific preflight behavior remain unchanged. The cap is shared across ConfigVersions of the same listing. During `reserve_recipe_plan`, the already verified Steam Card selection claims its selected complete-set count in the same SQLite `BEGIN IMMEDIATE` transaction as the canonical card reservations, preventing concurrent orders from exceeding the listing limit. Pre-dispatch cancellation releases the claim; after dispatch begins, the claim remains counted because the physical delivery may have occurred. The selected physical cards continue to be chosen automatically from bot inventories after buyer badge/inventory checks; no card AssetIDs, per-game split, physical card pool, or per-bot allocation are configured by the user. No Steam offers were sent by these changes. **CI:** Tests run `45384495967` passed; Windows executable run `45384495836` passed, including executable build, packaged smoke test, and artifact upload.

**Ordered CS2 selector alternatives in shared listing stock (2026-11-24):** Implemented ordered alternatives as one typed CS2 Steam Asset RecipeItem instead of treating each selector as a separate required product or rejecting the listing. `SteamAssetSpec.selector_options` preserves the ordered alternatives through ConfigVersion and OrderLine snapshots; stock identity matching accepts any declared option while retaining exact field matching and allowed-bot checks. Listing allocation now reserves only complete `quantity_per_order` bundles per matching alternative, does not double-count physical observations, and fails atomically if the requested listing sale capacity cannot be covered. At order preparation, the transfer chooses the first alternative with enough active listing-owned units and transfers only resources matching that one option; it does not mix different variants in one order. The no-listing-batch snapshot path also tries alternatives in order. Added regression coverage for legacy config conversion, ConfigVersion serialization, aggregate alternative stock allocation, first-option priority, fallback to the next option, repeated orders, and distinct AssetID ownership. Steam Cards remain on AppID 753 / ContextID 6 and were not changed. No Steam offers are sent by this path. CI verification completed on commit 22728d4b39db717d0f87a4139f286d482f05e3d0: Tests run 45375915841 succeeded (1,052 tests in 26.636s, isolated module checks also passed); Windows Build run 45375916000 succeeded (Python compile, Windows ACL test, EXE build, packaged executable smoke test, and GameDelivery-Windows-x64 artifact upload).

**Playerok incremental baseline-age enforcement (2026-11-22):** Исправлен пропуск в обычной incremental-синхронизации: ранее все сделки из active sales безусловно передавались в upsert как non-historical, в отличие от browser/GraphQL snapshot, который сравнивает `created_at` с `baseline_completed_at`. Теперь incremental path внутри одной `BEGIN IMMEDIATE` транзакции перечитывает account-scoped baseline marker, требует завершённый baseline и вычисляет historical тем же fail-closed правилом: timestamp отсутствует/некорректен/без timezone или сделка создана не позже baseline — историческая. Такие сделки не могут создать Order/OrderLine. Добавлен регрессионный тест на pre-baseline, missing и malformed timestamps. Изменения в `integrations/playerok/sync.py` и `tests/test_playerok_sync.py`; commits: `c2373c8` (fix), `c5edb06` (test). CI ещё не запускался; локальные тесты не запускались. Следующий шаг — CI verification, затем продолжить аудит Deal → Order и eligibility.


**CI regression-fixture alignment (2026-11-22):** Updated startup failure assertions to match the stage-specific SQLite migration error title while retaining the checks that the original database is preserved and no replacement is created. Updated the privacy-command test fixture to reserve its canonical leg and resource, bind the validated SteamID64 through `DeliveryLedger.bind_order_recipient()`, and only then enter `PRIVACY_WAIT`; this allows the test to exercise the intended Playerok freshness gate instead of failing earlier at recipient binding. No production delivery, recipient, or freshness checks were weakened. CI verification is required.



**Legacy synchronous ASF recipient snapshot correction (2026-11-22):** CI run Tests #1859 exposed two UI failures: successful and unknown-result send scenarios both stopped before `send_trade_offer` because the atomic legacy send claim requires a persisted `recipient_steam_id64`. The asynchronous preview callback already called `_bind_legacy_order_recipient_snapshot`, but the synchronous `_send_selected_order_with_asf` path omitted that step. After the synchronous ASF preview passes its exact order/bot/asset validation, it now persists the ASF-confirmed SteamID64 through the same fail-closed helper before showing confirmation and claiming the order. Regression assertions verify the snapshot is persisted for both confirmed-send and unknown-result paths. The helper still checks preview mode, send-disabled flag, order ID, URL-derived SteamID64 equality, immutable existing snapshot, and reserved state; no weaker recipient validation or automatic retry was introduced. CI is the verification gate.


**Steam inventory selection key consistency (2026-11-22):** CI Tests #1863 exposed three failures in the Steam-card inventory panel. The UI selection set stores exact physical ownership keys `(bot_name, asset_id)`, while reservation lookup uses a normalized key. Rendering and bulk-selection paths incorrectly used the reservation-normalized key as the selection key, which caused selected tiles to disappear, stale selections to survive for non-tradable/unknown cards, and selection to be lost across grouping changes. The fix keeps exact owner/asset pairs in `_selected_asset_keys` and uses `_reservation_key` only for reservation checks. Existing regression tests cover grouping preservation, tradability filtering, and tile selection. No delivery/reservation policy was weakened. CI verification is pending.


**Canonical Playerok import and reconciliation regression fixtures (2026-11-22):** Reworked the UI import tests to exercise the canonical path: an account-bound Playerok Deal with an existing canonical Order is displayed under its canonical local order ID and repeated import does not create a legacy `delivery_orders` duplicate. Added a fail-closed assertion proving that an unbound external payload cannot create either a canonical or legacy order. Updated the multi-leg reconciliation fixture to seed one active `gd_resource_reservations` row for each physical asset; reconciliation itself must transition each reservation to `consumed` only after Steam confirms that leg. The `DELIVERED` transition and Playerok outbox therefore remain gated by acceptance of every leg and consumption of every reserved asset. No production delivery/import gates were weakened. CI verification is pending.


**Follow-up CI fixes after Tests #1871 (2026-11-22):** The full 950-test run exposed two remaining fixture/assertion mismatches. The corrupted-database startup test now asserts the actual stage-specific SQLite migration failure text and explicitly verifies that the original file is preserved and no automatic reset/recovery is claimed. The privacy-command fixture now completes the canonical Playerok eligibility prerequisites (account binding, resolved listing, non-historical deal, completed account baseline) before asserting that the missing trusted GUI/browser freshness bridge blocks dispatch. This keeps the test focused on the intended freshness gate; production eligibility and freshness checks remain unchanged. CI verification completed successfully: Tests #1874 (run 45039540113, job 137260925817) and Windows Build #1070 (run 45039540114, job 137260687391) both succeeded on commit 8ae54a13ea1dbf177f47008e7d22cb8473d9de94.


**Multi-leg reconciliation state preservation (2026-11-22):** The state-transition audit found that `DeliveryReconciliation._complete_order_if_all_accepted()` counted `UNKNOWN`, `SENT_TO_STEAM`, and `DISPATCHING` as in-flight, and `DECLINED`/`FAILED` as requiring action, but omitted `PRIVACY_WAIT` and `TRADE_RESTRICTED`. When another leg was accepted, reconciliation could therefore overwrite the order-level privacy/trade restriction state with `PROCESSING`, even though the blocked leg remained unresolved. The completion calculation now preserves `PRIVACY_WAIT` or `TRADE_RESTRICTED` while other legs reconcile; it still marks the order `DELIVERED` only when every leg is accepted and every expected asset reservation is consumed. Added a multi-bot regression test: one leg enters privacy wait, the other is accepted, the order remains `PRIVACY_WAIT`, the blocked leg's reservation remains active, and the accepted leg's reservation is consumed. Production send/eligibility gates were not weakened. Commits: `2ef2826d13d751ede1db48c77df6ceda5ae1f11b` (fix), `e0d2f70d09cb58c1769d2b6d00ce803fcc95f9de` (regression test). Tests and Windows Build CI verification pending.


**Canonical order cancellation audit (2026-11-22):** Reviewed `core/orders/service.py`, `core/delivery/recovery.py`, `core/delivery/reservation_service.py`, `core/delivery/dispatcher.py`, `core/delivery/reconciliation.py`, `core/delivery/ledger.py`, and `integrations/playerok/sync.py`, plus their focused tests. The legacy `orders` queue supports a `cancelled` transition, but canonical delivery records in `gd_orders` have no dedicated cancellation service/transition contract in the inspected code. Playerok sync persists observed deal status and blocks unexpected status mutation; it does not cancel the canonical order or release delivery reservations. `DeliveryRecoveryService.release()` is an audited, per-leg operator action limited to `ACTION_REQUIRED` orders and terminal `FAILED`/`DECLINED` legs; it verifies recipient, absence of an active/uncertain Steam offer, and exact active reservations, and blocks while any order leg is in flight. It does not cancel the order or release accepted-leg resources. No production change was made: adding canonical cancellation without a defined contract for external Playerok cancellation, partial delivery, in-flight offers, and buyer refund/confirmation semantics would risk releasing assets or misrepresenting delivery. Next required design/test boundary: define canonical cancellation states and outcomes for (a) no offer dispatched, (b) offer in flight/unknown, (c) partially accepted multi-leg order, and (d) fully delivered order.

**C.3 ledger and SQLite migration audit checkpoint (2026-11-22):** Rechecked the Stage C.3 acceptance criteria against `core/database/ledger_migrations.py`, `core/database/db.py`, `core/delivery/ledger.py`, `core/delivery/reservation_service.py`, `core/delivery/order_pipeline.py`, `integrations/playerok/sync.py`, and focused migration/ledger/reservation tests. Confirmed: canonical `gd_*` schema has FK/check constraints; active physical-resource uniqueness is enforced globally (including normalized asset-ID guards); immutable ConfigVersion triggers exist; migrations use `BEGIN IMMEDIATE`, pre-migration backups, rollback on failure, FK checks and legacy reservation backfill/conflict preservation; Playerok order + Recipe OrderLines are materialized inside the caller's sync transaction; complete recipe resource reservation/DeliveryLeg writes are grouped in a transaction, with legacy Steam reservation rows written as a compatibility projection; concurrency tests cover two orders contending for one asset. Remaining C.3 gap: the schema contains listing ownership fields (`owner_type='listing'`, `listing_generation`) but the inspected canonical service layer has no complete ListingStockAllocation lifecycle or atomic listing-allocation-to-order transfer operation. Existing order reservations are created later, after the order/lines exist and fresh inventory preflight has run; this is distinct from the stronger C.3 listing-generation transfer requirement and must not be represented as completed. The current audit has not proven all backup/rollback scenarios against a copy of the user's production database, nor full listing allocation acceptance (100 units / 3+5+10, sale transfer, duplicate events, concurrent allocation). No production code changed and no tests were run locally; source/test inspection only. Next work remains inside C.3: define and implement/test the missing listing stock allocation lifecycle and its atomic transfer boundary before moving to C.4. Do not expand into cancellation semantics, Telegram, new ASF game handlers, or live Playerok automation.


## 2026-11-22 — каноническая отмена заказа в DeliveryRecovery

Продолжен аудит переходов состояний (только ветка `work/local-sync`). Добавлена отдельная операция `DeliveryRecoveryService.cancel_order` с fail-closed поведением:
- Отмена разрешена только для `ACTION_REQUIRED` заказа, у которого есть DeliveryLeg и каждый leg находится в доказанном `FAILED` или `DECLINED`.
- Для каждого leg обязательны терминальная последняя DeliveryAttempt, точное совпадение активных резервов с manifest, проверка закреплённого Steam-получателя и подтверждение ASF об отсутствии offer.
- `UNKNOWN`, `DISPATCHING`, `SENT_TO_STEAM`, `ACCEPTED`, другие статусы, отсутствие истории попытки, неполные/конфликтующие резервы и любая ошибка внешней проверки блокируют отмену.
- После повторной проверки внутри `BEGIN IMMEDIATE` все резервы всех legs атомарно переводятся в `released`, legs и заказ — в `CANCELLED`. Каждая leg cancellation аудируется существующей таблицей operator actions как release с evidence `order_cancelled=true`; новая схема БД не требуется.
- Это не удаляет историю attempts и не меняет Playerok deal status. Не выполняется автоматически reconciliation или dispatcher.

Добавлены регрессионные тесты: успешная отмена с освобождением полного набора, блокировка при неподтверждённом отсутствии offer, блокировка UNKNOWN/ACCEPTED, требование ACTION_REQUIRED и идемпотентная защита от повторной отмены.

Проверка исполнения тестов и CI ещё не выполнена; не считать задачу закрытой до получения результатов.


**Incremental Playerok historical-deal result contract (2026-11-22):** CI Tests #1887 (run 45048598855, job 137288699187) exposed a production result-contract bug in `PlayerokDealSyncService._upsert_incremental`: historical deals were persisted with `is_historical=1`, but every newly inserted row was counted as `new_deals`; the public sync result also hardcoded `historical_deals=0`. This contradicted the existing regression test for pre-baseline, missing, and malformed timestamps (expected `new_deals=0`, `historical_deals=3`; actual `new_deals=3`). Fixed the service to count newly inserted historical and non-historical deals separately and return both counts. The regression test was retained unchanged; no test was weakened. Commit: `28bdde811cddada7fdef37a47487e3f4752f6d5c`. CI verification is pending. Windows package build #1083 on the failing commit was green; the failure is isolated to the Tests workflow's `test_playerok_sync.py` module.


**CI verification update (2026-11-22):** The historical-deal counting fix is verified on branch `work/local-sync`, whose HEAD is `e04e934c135e871e5b4376c758936fabdc38fcde`. Tests run #1889 (`45049215182`) completed successfully; all job steps passed, including Python compilation, Steam credential/migration tests, delivery reconciliation/operator recovery tests, Steam trade UI flow, and the full unit test suite. Windows build #1085 (`45049215142`) also completed successfully; Python compilation, Windows trade-secret ACL hardening, GUI executable build, packaged executable smoke test, and package upload all passed. Tests #1888 and Windows build #1084 on parent code commit `28bdde811cddada7fdef37a47487e3f4752f6d5c` also passed. The branch HEAD is confirmed at `e04e934c135e871e5b4376c758936fabdc38fcde`. This verifies CI and packaging for this fix; it does not establish that the separate delivery-state/recovery audit is complete or that real Playerok/Steam live-order execution has been proven.


**Reconciliation stale-result race fix (2026-11-22):** State-transition audit found that `DeliveryReconciliation._update_leg()` unconditionally overwrote the current DeliveryLeg state after an external Steam status read. Two concurrent reconciliation workers could therefore allow a delayed `DECLINED` response to overwrite a state already committed as `ACCEPTED`, while also rewriting the latest DeliveryAttempt and potentially emitting a false decline notification. The update is now a compare-and-set restricted to legs still in `UNKNOWN` or `SENT_TO_STEAM`; attempt/reservation mutations occur only when that state transition wins. A stale response returns `STATE_CHANGED`, and the decline notification/inventory refresh path is skipped. Added regression test `test_stale_declined_reconciliation_cannot_overwrite_concurrent_acceptance`, which simulates a competing worker committing acceptance between the Steam read and stale decline write, then asserts leg/attempt remain accepted, reservation remains consumed, and no false notification is queued. Commits: `c984ebd` (production fix), `a3321e1` (regression test). CI verification pending. This is a targeted reconciliation race fix; it does not complete the broader state-transition audit or prove live Steam/Playerok execution.


**C.3 quantitative inventory foundation (2026-11-22):** Started the quantity-based stock path without changing existing unique-asset reservation or dispatch behavior. `games.inventory.InventoryItem` now carries a validated positive integer `amount`; `parse_game_inventory()` reads the quantity from each ASF Asset (defaulting to 1 only when ASF omits the field and rejecting malformed/zero/negative values); `GameInventoryStore` persists and reloads the amount. Existing `game_inventory_snapshot` databases are upgraded additively with `amount INTEGER NOT NULL DEFAULT 1 CHECK(amount > 0)`, preserving old rows. Added regression coverage for parsing a 100-unit stack, rejecting malformed quantities, round-trip persistence and loading a pre-existing schema with default amount 1. Commits: `6a1b989425199e37a2008f897240bb7db4906be6` (implementation), `0633427d59452d7bcc441b56022fb06b6f7a15a0` (tests). CI pending. This is inventory snapshot groundwork only: no ListingStockAllocation lifecycle, partial-stack reservation, quantity-aware Steam send payload, or exact-amount live offer verification is implemented yet. The current ASF plugin send request still accepts only AssetIds and therefore must not be treated as quantity-capable.


**C.3 quantity snapshot correction (2026-11-22):** CI run #1896 exposed a defect in the initial quantity persistence change: `GameInventoryStore.load_all()` restored `amount`, but `GameInventoryStore.load()` omitted it and silently returned the dataclass default of 1. Fixed the SELECT and row mapping in commit `1813df5e8ab4c199930447ab1af55c7f000a5546`. A compatibility review also found that placing the new defaulted `amount` field before existing optional dataclass fields could shift legacy positional constructor arguments; moved `amount` to the end of `InventoryItem` in commit `44d53607c4aaf177388be3226c59d170e625c5c3`. CI validation for these fixes is pending (runs #1897/#1898 and Windows build #1093/#1094). The original failed run #1896 had three failures, all caused by the missing `load()` hydration; no test was weakened.


**C.3 ASF partial-stack transport research checkpoint (2026-11-22):** Проверен текущий контракт GameDelivery и исходники ASF. В GameDelivery `SendRequest` / `PendingTradeSecret` / `SendRecord` и `ASFClient.send_trade_offer()` передают только AssetIds; `ProcessSendCommand` выбирает исходные полные Asset из инвентаря. Поэтому текущая реализация не может гарантировать частичную отправку стека и fingerprint/reconciliation не учитывают units. В ASF исходниках найден поддерживаемый механизм: `Asset` имеет `Amount` и публичный конструктор с amount; `Bot.GetItemsForFullSets()` при необходимости клонирует Asset через `DeepClone()` и задаёт клону точный `Amount` перед вызовом `ArchiWebHandler.SendTradeOffer()`. Это доказывает техническую возможность частичного стека через ASF без отдельного Steam-клиента. Важно: текущий плагин должен создавать отдельный Asset с requested units (а не менять исходный инвентарный объект), а контракт отправки/запись идемпотентности/сверка offer должны включать точные пары (asset_id, units). Проверка выполнена по ASF Asset.cs, Bot.GetItemsForFullSets() и ArchiWebHandler.SendTradeOffer() на публичном исходнике ASF; точную бинарную совместимость с локально поставляемой ASF 6.3.11.7 ещё требуется подтвердить сборкой плагина против именно поставляемых DLL. Отдельный структурный блокер C.3: gd_resource_reservations имеет units, но уникальный partial index idx_gd_resource_active_global запрещает более одной активной строки на (bot_name, asset_id) независимо от owner; текущая схема не позволяет одному стеку одновременно иметь независимые listing allocations и последующие order allocations. До реализации нужен явный stock-allocation design (агрегатный учёт доступного amount и атомарное распределение owner allocations) с тестами конкуренции, перепродажи, duplicate event и no-partial-shortage. Изменений production-кода и тестов в этом checkpoint нет; live trades не выполнялись.


**C.3 global multi-bot stock allocation design (2026-11-22):** Зафиксировано уточнение пользовательской модели: все разрешённые ASF-боты образуют единый логический склад; пользователь указывает только quantity объявления, бот и физические Asset ID автоматически выбираются программой. Бот остаётся обязательной частью физического ключа ресурса `(game_id, app_id, context_id, bot_name, asset_id)`, но не является пользовательским фильтром/границей склада. Проверена текущая схема `core/database/ledger_migrations.py`: `gd_resource_reservations` уже содержит `units`, listing ownership и generation, однако `idx_gd_resource_active_global` и canonical guard triggers запрещают более одной блокирующей записи на физический Asset ID; следовательно, один stack нельзя сейчас разделить между несколькими listing/order allocations. Создан проектный дизайн `docs/superpowers/plans/2026-11-22-c3-global-listing-stock-allocations.md` с инвариантами общего склада, агрегатным учётом Amount, multi-bot allocation, атомарным listing→order transfer, fail-closed preflight, правилами изменения/отключения объявления, миграционными требованиями и acceptance tests. Только документация: production-код и БД не менялись, тесты/CI не запускались, реальные трейды не выполнялись. Следующий implementation slice — спроектировать безопасную миграцию quantity ledger и stock observation, затем реализовать aggregate allocation с конкурентными тестами; не переходить к UI или live Playerok/Steam.


**C.3 quantity-ledger migration design (2026-11-22):** Повторно исследованы актуальные `core/database/ledger_migrations.py`, `core/database/db.py`, `core/delivery/reservation_service.py`, `core/delivery/order_pipeline.py`, `games/inventory.py` и migration tests. Подтверждено, что `gd_resource_reservations` имеет уникальный индекс по `(bot_name, asset_id_key)` для active rows и canonical guard triggers, а legacy `steam_asset_reservations` синхронизируется двусторонними triggers и также остаётся уникальной проекцией. Текущий `InventoryItem.amount` хранится в snapshot, но snapshot не является транзакционным лимитом reservation ledger. Создан документ `docs/superpowers/plans/2026-11-22-c3-quantity-ledger-migration-design.md` (commit `861d64ca2b36151542e7f02ed7f776bac5f0d2f9`). Предлагаемый безопасный путь — отдельные `gd_stock_observations` и `gd_stock_allocations`, двусторонние guards между новым quantity ledger и старыми reservation writes, агрегатный лимит под SQLite write transaction, fail-closed при stale/missing/conflict observation и сохранение старых активных reservations как whole-asset blockers без фиктивного backfill `units=1`. Отдельно зафиксирована необходимость snapshot revision/consumption accounting, чтобы consumed units не освобождались преждевременно и не вычитались повторно после нового snapshot. Это пока только migration design: production schema/code/tests не менялись, CI не запускался, live trades не выполнялись. Следующий шаг — реализовать migration + observation store/contract и тесты агрегатного лимита как отдельный ограниченный slice.


**C.3 quantity stock ledger — first implementation slice (2026-11-22):** В `work/local-sync` добавлена миграция `2026-11-quantity-stock-ledger-v1`: таблицы `gd_stock_observations` и `gd_stock_allocations`, индексы, FK/CHECK constraints, SQLite guards для aggregate blocking units, состояния `active/pending/unknown/legacy_conflict/consumed/released`, snapshot revision для consumed accounting и двусторонняя блокировка пересечений с прежним `gd_resource_reservations`/`steam_asset_reservations`. Старые миграции/индексы/триггеры не ослаблялись; старые reservations не backfill-ятся в фиктивные unit allocations. В `Database.initialize()` добавлен отдельный pre-migration backup для новой migration ID. Создан `core/delivery/stock_ledger.py`: полный физический ключ ресурса, строгая проверка положительных integer amount/units, атомарное наблюдение с revision, fail-closed при legacy reservation/stale/conflict, транзакционная allocation и вычисление свободного количества. Добавлены `tests/test_quantity_stock_ledger.py`: миграция/backup, split одного стека на несколько allocations, insert/update over-allocation, конкурентная гонка двух writer-ов, снижение Amount ниже allocations, stale observation, legacy blocker и некорректные количества. Коммиты implementation: `cfbf73743a8667f5a32d6a11e665a664e6b659b0`, `cb439b34ffc9cbf54e62ad0eeda94cf8656f81f6`, `edd1a55c6bbbc6811d7fa15243ec3381cf4a1130`; tests/fixes: `0e8c0ca8773d65c791f9194bd4d42623d9fccbf3`, `699ab5d25759ad6b4a72b07c844245a53608df87`, `b26b0d735299ed742139e988ac92401eb6f3014b`, `f2925f62a1a98e07e0cf6246e3fb28cd56b263bf`, `22bf954d065e964ff77131e4d85506a0e834684e`. На момент записи тесты ещё не исполнены; CI не проверен. Это только миграция и низкоуровневый stock ledger: listing lifecycle, transfer listing→order, ASF quantity-aware transport, dispatch и live trades не подключены. Обязательное правило: не исполнять заказы, поступившие при выключенной программе, остаётся без изменений.


**C.3 quantity stock ledger follow-up (2026-11-22):** Дополнительно добавлены DB guards канонического `asset_id_key` для stock observations (insert/update), а также тесты на запрет подмены ключа и на корректное прекращение учёта consumed units после более новой inventory revision. Дополнен concurrency test: два параллельных writer-а пытаются зарезервировать по 4 из Amount=5; ожидается ровно один успех. Последующие commits: `fc1a6cc58b97c05a2940f426dfc031191209f20c`, `18ee8f33b7418b4cdcb5cf5a8fce9b7fe9919264`. Код и тесты пока не исполнены локально; CI status для текущей ветки ещё не получен. Следующий обязательный gate — выполнить targeted test module и полный test suite в поддерживаемом GitHub Actions/локальном окружении, исправить все ошибки до подключения к listing/order lifecycle.


**C.3 quantity stock ledger hardening (2026-11-22):** Усилена неизменяемость физической идентичности observation: запрещено менять `observation_id/game_id/app_id/context_id/bot_name/asset_id`; revision обязана строго возрастать. Добавлены регрессионные тесты на оба ограничения. Коммиты: `ae6d08b58c9c7361427e03fea0f123fc1aac9dc4`, `0a109dcf82ca3c84a819defdc65d60209e016ef1`. По-прежнему ожидается выполнение тестов/CI; до их прохождения не подключать этот слой к автоматической выдаче.


**C.3 quantity stock audit hardening (2026-11-22):** По итогам повторного review исправлены три пограничных случая в quantity stock foundation. `StockResource.normalized()` теперь использует общий строгий `validate_asset_id()` и отклоняет числа/None/пустые строки вместо неявного преобразования в строку. `QuantityStockLedger.free_units()` начинает явную read-транзакцию, чтобы чтение observation и суммы blocking allocations происходило из одного SQLite snapshot. Добавлена отдельная additive migration `2026-11-quantity-stock-audit-v1` с отдельной pre-migration backup-копией и append-only guard: физическое удаление allocation запрещено, освобождение должно фиксироваться переходом state, сохраняя историю. Добавлены регрессионные тесты для строгого Asset ID и запрета DELETE. Это не завершает C.3: listing lifecycle, атомарный listing→order transfer, multi-bot allocator и ASF quantity-aware send ещё не реализованы. Требуется targeted test module и полный CI; до их прохождения не подключать stock ledger к автоматической выдаче. Изменения только в `work/local-sync`; `main`, PR #4 и live Steam/Playerok операции не затрагиваются.


**C.3 listing stock allocation and order-transfer service (2026-11-22):** Added an additive, backed-up migration for immutable listing-allocation request batches and append-only paid-event transfer records (gd_stock_allocation_batches, gd_stock_transfers). Added ListingStockService in core/delivery/listing_stock.py: it deterministically allocates an exact requested quantity from caller-supplied eligible physical resources inside BEGIN IMMEDIATE, uses the shared observed-stock ledger and SQLite aggregate guards, rejects insufficient stock without leaving partial rows, and makes allocation requests idempotent. transfer_to_order() validates the canonical OrderLine and ConfigVersion, enforces the line quantity limit, transfers exact slices from listing ownership to order ownership in one transaction, preserves untransferred remainder as a new listing allocation, and makes repeated paid-event IDs idempotent. Added service tests for deterministic multi-bot allocation, insufficient-stock rollback, allocation retry, multi-bot transfer, duplicate paid event, event-ID parameter conflict, and OrderLine quantity cap. No Playerok sync/dispatcher integration, automatic listing adjustment/disable/release workflow, ASF quantity-aware send, or real Steam/Playerok operation was added. The service accepts already-filtered eligible resources; it does not independently establish tradability, item selector compatibility, or inventory freshness policy beyond requiring a usable stock observation. CI verification pending. The existing invariant remains: only deals/orders observed during an active application session may enter processing; no startup backfill of orders received while the app was closed.


**C.3 stock transfer test correction (2026-11-22):** GitHub Actions run #1936 exposed one incorrect assertion in test_listing_stock_service.py: Bot-B had an observed stack of 5, only 3 units were allocated to the listing, and after transferring 1 unit the remaining 2 listing units correctly stayed reserved while 2 units of the physical stack remained free. The implementation's aggregate accounting was correct; the test incorrectly expected zero free units. Corrected the assertion to expect 2 free units and added a direct assertion that the 2-unit listing remainder remains active. No production behavior or test scope was weakened. Commit: b2c38b8b070c3eb2f1fc98871e8f9998cde652df. Targeted and full CI rerun pending.


**C.3 listing-stock caller-transaction boundary (2026-11-22):** Refactored `ListingStockService.transfer_to_order()` so it can either own its existing `BEGIN IMMEDIATE` transaction or participate in a caller-owned SQLite transaction through the optional `connection` argument. The transfer logic is shared; the caller-owned path does not begin, commit, or roll back the outer transaction. Added a regression test proving that a caller rollback restores listing-owned units and removes transfer/order allocations, and that a committed caller transaction persists the transfer. This is a transaction-boundary prerequisite only: it does not yet connect Playerok PAID processing, does not select/filter resources by recipe item type, and does not alter the existing separate game-item and Steam-card delivery handlers. Historical/startup orders remain excluded by the existing baseline-age gate; no live Playerok or Steam operation was performed. Commits: `2eddcf4b451f58fba5da2e224db1b17c50f4ae21` (service), `41fbe12a95e224041796aa60d07f8182f2399b22` (test). CI verification pending.

**C.3 exact Steam asset identity contract (2026-11-22):** Добавлен изолированный контракт идентичности игрового Steam asset: строгая нормализация StockItemIdentity, проверка физического scope (game_id, app_id, context_id) против SteamAssetSpec и exact-match по каждому переданному selector field (market_hash_name, name, item_type, class_id, quality, exterior, rarity). Отсутствующие/пустые поля и placeholder name не проходят сопоставление; allowed_bot_pool проверяется отдельным методом и не входит в identity. Добавлены unit tests для exact match, missing fields, scope mismatch, placeholder rejection и bot pool. Это pure contract/test slice: stock observation persistence, ASF snapshot integration, stale-resource invalidation и listing allocator integration ещё не подключены; существующая выдача не менялась. Тесты/CI требуют отдельного запуска; live Steam/Playerok операции не выполнялись.

**C.3 identity contract CI correction (2026-11-22):** GitHub Actions run `45063451753` показал причину падения тестового job: новый тестовый модуль импортировал `pytest`, которого workflow не устанавливает; проект использует `unittest`. Перевёл тесты на `unittest`, исправил ошибочный отрицательный кейс (строка с завершающим пробелом нормализуется и должна совпадать; теперь проверяется реальное отличие значения), добавил fail-closed поведение matcher при некорректной identity и отказ от строкового `allowed_bot_pool`. Повторный CI после этого исправления обязателен; предыдущий job завершился failure, полный suite также завершился с exit 139 при shutdown, поэтому не считать его успешным.


**C.3 stock identity persistence and full ASF snapshot gate (2026-11-22):** В additive migration `2026-11-quantity-stock-identity-v1` добавлены nullable `identity_json` и tri-state `is_tradable` для `gd_stock_observations`; существующие/legacy observations остаются без подтверждённой identity и потому не допускаются к selector matching. Migration подключена к delivery ledger и отдельному pre-migration backup. `QuantityStockLedger.observe()` теперь атомарно записывает Amount, revision, полную нормализованную identity и tradability; добавлена factory-логика InventoryItem → физический ключ + identity с app/context из `games.catalog.get_game()`. Добавлены `observe_snapshot()` и `eligible_resources()`: полный snapshot проводится одной SQLite write-транзакцией, отсутствующие assets переводятся в `unavailable` с повышением revision, существующие allocations не удаляются/не освобождаются; selector match точный, допустимость по `allowed_bot_pool` отдельная, `is_tradable` должен быть строго `True`. Рабочий game inventory refresh теперь перед сохранением отображаемого snapshot сначала фиксирует успешно распарсенный полный ASF snapshot в stock ledger; ошибочный/неполный ASF ответ не заменяет наблюдения. Steam Cards (753/6) остаются на отдельном пути. Добавлены регрессионные тесты для identity/tradability, scope mismatch, allowed bot pool, unavailable при полном пустом snapshot и сохранения order/listing allocations. Playerok sync/dispatcher, ASF send/quantity transport и live operations не подключались. CI ещё не запускался; следующий gate — targeted tests и полный GitHub Actions.


**C.3 inventory refresh regression correction (2026-11-22):** Первый CI для `dd9eb3ac48e5d5d15af9e9d49dfa59fc8ec35c8e` показал две регрессии в `test_cs2_inventory_panel.py`: worker инициализировал quantity ledger для тестового `:memory:` пути, хотя SQLite connections для этого wrapper не разделяют такой in-memory DB; из-за потери schema worker завершал обработку до CS2 adapter. Также была обойдена существующая функция `refresh_game_inventory()` для не-CS2 игр. Исправлено: ledger не создаётся для connection-scoped `:memory:` test path; не-CS2 путь снова использует `refresh_game_inventory()`, а helper при наличии явно attached ledger фиксирует validated full snapshot до замены display snapshot. Добавлен тест порядка ledger-before-display для generic game refresh. CI run `45066067581` выявил только эти две регрессии; все остальные изолированные test modules, включая 19 quantity-ledger tests и 8 identity tests, прошли. Windows build run `45066067547` на момент записи ещё выполнялся. Реальные Steam/Playerok действия не выполнялись; startup/historical order gate не менялся.


**C.3 exact RecipeItem stock allocation and transfer boundary (2026-11-22):** Добавлена additive migration `2026-11-quantity-stock-recipe-item-v1` с отдельным pre-migration backup; старые allocation/transfer rows сохраняют `recipe_item_id=NULL`. Новые typed Steam Asset allocations и transfer events получают точный version-pinned `recipe_item_id`; SQLite guards проверяют, что ID принадлежит Steam Asset позиции той же ConfigVersion, а order allocation/transfer совпадает с RecipeItem snapshot внутри OrderLine. Добавлен `ListingStockService.allocate_recipe_items()`: внутри одной `BEGIN IMMEDIATE` транзакции загружает типизированный Recipe, exact-match кандидатов по полям SteamAssetSpec и bot pool, требует `is_tradable=True`, детерминированно планирует units сразу для всех запрошенных Steam Asset позиций и либо создаёт полный allocation batch, либо не оставляет строк. Другие recipe kinds не включаются в этот stock path. `transfer_to_order()` для typed Recipe требует точный `recipe_item_id`, проверяет snapshot OrderLine и повторно проверяет текущие usable/tradable observations и selector перед переводом; sibling RecipeItem allocations не могут попасть в чужую OrderLine. Playerok sync/dispatcher и ASF send path не подключались. Добавлены тесты на смешанный Steam Asset + digital text recipe, bot pool, idempotency, cross-line rejection, отдельные transfer по позициям и all-or-nothing shortage. Требуется targeted и полный CI до следующего этапа.


**C.3 recipe-item SQL binding correction (2026-11-22):** Первый CI прогон `45067948768` точно локализовал четыре ошибки в одном test module: legacy listing allocation INSERT не передавал значение `NULL` для нового `recipe_item_id`; partial-transfer remainder INSERT передавал это значение, но колонка не была добавлена в SQL column list. Исправлены оба INSERT. Удалена неиспользуемая тестовая заглушка из helper; backup для recipe-item migration также добавлен в альтернативный `migrate_delivery_ledger()` путь. Остальные test modules прошли изоляцию; повторный полный CI обязателен.


**C.3 typed recipe remainder assertion correction (2026-11-22):** CI run `45068224614` прошёл 995 из 996 тестов; единственное падение — неверное ожидание в новом тесте recipe transfer. Все 4 listing-owned Redline units были переведены в OrderLine, поэтому listing-owned остаток по Redline равен нулю; одна из пяти единиц физического стека остаётся свободной, но не принадлежит объявлению. Для Dragon Lore из двух listing units одна остаётся у объявления после частичного transfer. Исправлено: тест отдельно проверяет listing remainder `{dragon: 1}` и free physical Redline units `1`. Production logic не менялась. Повторный полный CI обязателен.


**C.3 listing-owned stock release lifecycle (2026-11-22):** Добавлена additive migration `2026-11-quantity-stock-listing-adjustment-v1` с backup и append-only audit table `gd_stock_listing_adjustments`. `ListingStockService.release_listing_units()` освобождает только указанное количество listing-owned units, при частичном освобождении создаёт остаток с тем же физическим ресурсом и RecipeItem ID; операция idempotent. Для typed Recipe release требует точный Steam Asset `recipe_item_id`, legacy path не принимает RecipeItem ID. `disable_listing_stock()` атомарно освобождает все оставшиеся active listing-owned allocations для конкретной ConfigVersion и помечает listing disabled; Order allocations остаются active и не затрагиваются. Операции fail-closed при наличии listing-owned pending/UNKNOWN/legacy-conflict allocations до reconciliation. Добавлены тесты partial release + idempotency, сохранение order-owned units при disable, unknown blocking и изолированное освобождение одной RecipeItem позиции. Никаких внешних Playerok/Steam запросов или отправок не выполнялось.


**C.3 listing release with stale observations and zero-stock disable (2026-11-22):** Review найденного SQLite guard выявил, что прежний `gd_stock_allocations_guard_update` запрещал любое изменение allocation, если последнее observation уже `stale/unavailable`; это мешало безопасно освободить listing-owned claim после полного snapshot, в котором asset исчез. В новой adjustment migration trigger заменяется fail-closed вариантом: released transition разрешён даже при stale/unavailable observation, но любой новый blocking state по-прежнему требует usable observation, отсутствия legacy reservation и соблюдения агрегатного Amount. Уточнено, что disable generation без listing-owned stock тоже должен фиксировать `status='disabled'`, `auto_relist=0` и audit event с нулевым units; partial release при этом по-прежнему требует положительное количество. Добавлены тесты на освобождение stale resource и disable пустого поколения.


**C.3 listing adjustment migration backup path correction (2026-11-22):** Добавлен вызов `ensure_pre_migration_backup()` для `QUANTITY_STOCK_LISTING_ADJUSTMENT_MIGRATION_ID` также в альтернативный `migrate_delivery_ledger()` entry point; `Database.initialize()` уже содержит тот же backup. Оба штатных migration entry point теперь создают отдельную копию до применения новой схемы.


**C.3 listing release guard hardening (2026-11-22):** В отдельной additive migration `2026-11-quantity-stock-listing-adjustment-hardening-v1` (с отдельным backup) переносится rebuild audit table к `units>=0`, чтобы disable пустого поколения сохранялся как audit event. В той же миграции обновляется allocation UPDATE trigger: переход в released разрешён после stale/unavailable observation, но partial release, который требует создания нового активного remainder, явно fail-closed без usable observation; возврат в любое blocking state по-прежнему требует usable observation и проходит aggregate Amount/legacy reservation guards. Оба аллокатора теперь отклоняют disabled ConfigVersion, partial release проверяет observation state до изменения, тест pending/unknown теперь создаёт observation для его физического ресурса. Добавлены проверки: disabled generation не принимает новые allocations и stale partial release оставляет старую allocation неизменной.


**C.3 unknown-listing guard test setup correction (2026-11-22):** Первый CI на `6337350cc0721cefdee92b659fb319d7e7199fb3` показал, что тест pending/UNKNOWN allocation пытался использовать `QuantityStockLedger.allocate()` после одиночного `observe()`; allocator намеренно требует complete snapshot marker. Тест теперь создаёт полноценный `observe_snapshot()` для второго бота перед созданием unknown allocation. Production gate полного inventory snapshot не менялся.


**C.3 SQLite partial-legacy migration dependency hardening (2026-11-22):** Новый CI (`45069533235`) выявил проблемы промежуточных legacy schema: asset-id-key tests специально подавляют marker этой migration, а quantity stock triggers не должны устанавливаться до появления `gd_resource_reservations.asset_id_key`; теперь stock migrations выполняются только после подтверждённого marker+column. Для RecipeItem triggers добавлена schema-aware установка: если legacy `gd_order_lines` ещё не имеет `item_selector_json`, typed order allocation/transfer trigger получает условие `0` и запрещает операцию fail-closed, но сам trigger больше не ссылается на отсутствующую колонку. Hardening migration сначала удаляет старые recipe-item triggers и после rebuild таблицы устанавливает их заново безопасным способом. В тесте UNKNOWN allocation исправлено исходное ожидание статуса listing: базовая тестовая строка использует schema default `unknown`, и при заблокированном disable статус обязан оставаться неизменным.


**C.3 migration syntax and return-flow correction (2026-11-22):** Исправлены некорректные отступы в hardening migration и гарантирован Boolean return из `apply_delivery_ledger_migrations()` даже на legacy DB, где Asset-ID-key migration намеренно не завершилась и quantity stock path поэтому пропускается. Stock triggers не создаются без завершённой физической Asset-ID нормализации.


**C.3 hardening migration indentation correction (2026-11-22):** Исправлен первый `DROP TRIGGER` в hardening migration: в предыдущем commit текстовая замена не совпала с фактическим девятипробельным отступом. Остальной control flow уже проверен: `apply_delivery_ledger_migrations()` возвращает `changed` и в ветке с пропуском quantity stock migration.


**C.3 listing stock increase adjustment (2026-11-23):** Добавлена additive migration `2026-11-quantity-stock-listing-increase-v1` с отдельным backup для обоих entry points миграции. Append-only audit `gd_stock_listing_adjustments` теперь поддерживает `allocate_units`; positive adjustment хранит точный список allocation IDs и units. Добавлены `increase_listing_units()` для legacy listing и `increase_recipe_units()` для typed Steam Asset RecipeItems: оба работают внутри `BEGIN IMMEDIATE`, требуют исходный allocation batch, проверяют listing/config generation, fail-closed на disabled listing, строят весь план до INSERT, а нехватка оставляет старые allocations и audit без изменений. Typed path проверяет свежесть observation, `is_tradable=True`, точный `SteamAssetSpec`, allowed bot pool, сохраняет per-RecipeItem slices и не захватывает card set/digital_text_unit. Оба метода идемпотентны по adjustment_id; SQLite migration добавляет отдельные disabled-listing guards для прямых allocation INSERT/UPDATE. Добавлены тесты legacy increase, shortage rollback, multi-item typed increase/idempotency, typed shortage и disabled generation. Не подключено к UI/Playerok; следующий gate — targeted и полный CI.


**C.3 listing increase shortage test setup correction (2026-11-23):** Первый CI на `50bd47fff660841dc35716a465aae830bccd45bb` прошёл 1008 тестов; один новый test setup завершился до вызова increase потому, что typed Recipe требует initial allocation для всех обязательных Steam Asset позиций. Исправлен сценарий: initial batch выделяет по 1 unit для Redline и Dragon Lore, затем increase запрашивает 1 дополнительный Redline и 3 Dragon Lore при двух свободных Dragon Lore units; ожидаемый shortage возникает во время второго slice planner. Проверка подтверждает, что ни один planned slice и audit event не сохраняются. Дополнительно `increase_recipe_units()` теперь отклоняет повторяющиеся `recipe_item_id` после нормализации пробелов; добавлен отдельный тест.


**C.3 typed increase shortage assertion correction (2026-11-23):** CI run `45135374114` показал, что после корректного initial typed batch тест ожидал один active unit, хотя setup намеренно закрепляет по одному unit для двух разных обязательных Steam Asset RecipeItems. Production planner вернул expected shortage, а audit row не создал; ошибка была только в test expectation. Проверка заменена на точное сравнение active units по двум `recipe_item_id`: `{first: 1, second: 1}`. Суммарное количество по-прежнему не меняется, а операция increase остаётся all-or-nothing.


**C.3 concurrent listing allocation and paid-event transfer regression tests (2026-11-23):** Добавлены два file-backed SQLite race tests с синхронизацией двух worker threads через Barrier. Первый запускает одновременные allocation requests двух ConfigVersions на один физический stack Amount=10 (каждый просит 7): ровно один batch должен закрепиться, второй должен увидеть доступно 3 и завершиться без partial row; проверяется, что aggregate allocations=7, free=3. Второй запускает два paid events для разных OrderLine, каждый пытается перевести одни и те же 4 listing units: ровно один transfer должен пройти, второй получить shortage 0; проверяются один immutable transfer event, единственная active order allocation 4 units и отсутствие активного listing remainder. Tests напрямую проверяют serialization boundary `BEGIN IMMEDIATE`, без live Steam/Playerok операций.


**C.3 atomic listing stock reallocation (2026-11-23):** Добавлена additive migration `2026-11-quantity-stock-listing-reallocation-v1` с отдельным pre-migration backup для `Database.initialize()` и `migrate_delivery_ledger()`; append-only `gd_stock_listing_adjustments` теперь принимает `reallocate_units`. Добавлен `ListingStockService.reallocate_listing_units()`: одна `BEGIN IMMEDIATE` транзакция валидирует точную active listing-owned source allocation, свежесть обоих observations, `is_tradable=True`, физический game/app/context scope и item identity. Для legacy ConfigVersion требуется полное совпадение StockItemIdentity source/destination и соблюдение ConfigVersion bot pool; для typed Recipe требуется точный Steam Asset `recipe_item_id`, `SteamAssetSpec.matches_spec()` и allowed bot pool. Источник переводится в released; незатрогнутый остаток source allocation создаётся заново; target allocations вставляются только после полного плана. Shortage/identity/stale/trigger failure откатывает всю операцию, order-owned units не меняются. Audit сохраняет source ID, target observation IDs, released/remainder IDs и created allocation IDs; повторный adjustment idempotent и отклоняет другой source/destination. Добавлены тесты legacy reallocation с уже существующей order allocation, idempotency, target shortage без partial writes, typed RecipeItem scoping и stale source gate. UI/Playerok integration и live operations не подключались.


**C.3 reallocation legacy identity fixture correction (2026-11-23):** Новый legacy reallocation test использует один и тот же item identity на source и destination. В fixture Bot-B class_id из общего inventory helper отличался от Bot-A, что при корректной fail-closed политике legacy reallocation означало бы другой item. Тест теперь задаёт целевому наблюдению тот же полный StockItemIdentity, не ослабляя production matching.


**C.3 reallocation migration legacy-schema guard correction (2026-11-23):** CI run `45136612250` показал 4 errors в промежуточных legacy schema: триггеры `gd_stock_allocations_listing_enabled_insert/update`, созданные прежней migration, ссылались на `gd_listings.status`, хотя в legacy fixture таблица содержала только `listing_id`. Добавлена отдельная backed-up migration `2026-11-quantity-stock-listing-enabled-guard-v1`: она заменяет эти триггеры без обращения к отсутствующей колонке; если `status` ещё отсутствует, любые новые blocking listing allocations fail-closed до расширения схемы. Backup добавлен в `Database.initialize()` и `migrate_delivery_ledger()`; добавлен regression test для частичной legacy schema.

**C.3 reallocation identity fixture correction (2026-11-23):** В test `test_reallocate_listing_units_is_atomic_idempotent_and_preserves_order_stock` destination использовал уже наблюдавшийся Bot-B Asset ID `1002`, у которого исходная fixture намеренно задавала другой `class_id`. Повторное наблюдение с подменённым identity не является корректной fixture для identity equality. Destination перенесён на отдельный физический Asset ID `9001` с согласованной StockItemIdentity; production matching rule остаётся строгим и неизменным.


**C.3 reallocation audit payload semantics correction (2026-11-23):** Source allocation, recreated active source remainder и newly claimed destination slices теперь записываются в три независимых audit arrays: `released_allocation_ids`, `source_remainder_allocation_ids`, `created_allocation_ids`. Это исключает двусмысленность, при которой активный остаток source ошибочно фигурировал как released. Добавлена точная проверка структуры audit payload.


**C.3 shared stack across listing generations and legacy conflict transfer gate (2026-11-23):** Добавлен сервисный тест одного физического stack Amount=100, разделённого между тремя ConfigVersions/listing generations: 3+5+10 units, занято 18, свободно 82; запрос ещё 83 units отклоняется полностью с сохранением 82 свободных units. Найден и исправлен guard gap: `transfer_to_order()` повторно проверял `availability_state='usable'` только для typed Recipe, но не для legacy ConfigVersion. Теперь любое listing→order transfer требует usable observation; stale/conflict/unavailable block применяется ко всем recipe modes. Добавлен regression test Amount 10→5 при 6 listing-owned units: legacy transfer блокируется, исходные 6 units остаются у объявления, transfer/audit/order allocation не создаются.


**C.3 three-bot typed listing allocation coverage (2026-11-23):** Добавлен прямой integration-style test одного typed Steam Asset RecipeItem на трёх ASF bot inventories. Одна операция allocation без входного выбора bot получает Amounts 3/4/5 на Bot-A/Bot-C и детерминированно распределяет 10 units как 3+4+3; free остатки равны 0/0/2, все строки остаются закреплены за одним RecipeItem. Этот тест закрывает требование C.3 «общий пул ботов, API не требует выбрать бота» без подключения UI/Playerok или реальных Steam операций.


**C.3 exact stack Amount ASF contract (2026-11-23):** ASF 6.3.11.7 transport now accepts optional per-asset `AssetAmounts` beside distinct Asset IDs for preview and send. Legacy requests omitting the map retain previous full-stack behavior; explicit maps must supply exactly one positive UInt32 amount for each requested ID. The plugin re-reads the live inventory, rejects requested amounts above the current stack, and constructs a separate ASF `Asset` copy preserving app/context/class/instance/asset IDs and Description while setting only the requested Amount. It does not mutate the original inventory object. The amount map is bound by the one-use secret, included in send fingerprint, persisted into `sent_offers.json`, and checked when reconciling sent offers. Read-only preview returns the exact amount; Python ASFClient validates both request and response. C# smoke covers a 3-of-100 copy with stable physical identity. This is the ASF transport contract only: the canonical DeliveryLeg manifest and ListingStockService→DeliveryReservationService connection have not yet been wired, so end-to-end quantity delivery remains blocked until the next integration slice. Steam Cards keep their existing behavior and default full-stack/current amount (normally one per card). No Steam offer was sent; no live ASF account was used.


**C.3 ASF quantity fingerprint backward-compatibility correction (2026-11-23):** Исправлено построение send fingerprint: при legacy запросе без `AssetAmounts` canonical string остаётся байт-в-байт прежним; дополнительная строка `amounts=...` добавляется только когда карта количеств явно передана. Это сохраняет existing idempotency keys для старых Steam Cards и CS2 отправок, но различает, например, запросы на 3 и 100 единиц одного Asset ID.


**C.3 Python ASF amount helper decorator placement fix (2026-11-23):** Windows ACL gate discovered that the new module-level `_normalize_asset_amounts()` helper had been inserted between `@dataclass(frozen=True)` and `ASFClient`, causing the decorator to apply to a function and breaking module import. Moved `@dataclass(frozen=True)` back directly above `class ASFClient`; both amount helpers remain module-level undecorated functions. No runtime ACL or trade logic change. Full Tests, Windows Build and ASF plugin workflow rerun on the corrected commit.


**C.3 DeliveryLeg amount manifest and dispatcher gate (2026-11-23):** Добавлена nullable `gd_delivery_legs.asset_amounts_json` через additive migration с pre-migration backup, а также optional `DeliveryLeg.asset_amounts` и `DeliveryLedger.add_delivery_leg(asset_amounts=...)` с точной проверкой множества Asset IDs и положительных UInt32 amounts. `DeliveryDispatcher` теперь валидирует manifest до создания DeliveryAttempt, сверяет каждый Amount с активным `gd_resource_reservations.units` и только после этого передаёт `asset_amounts` в ASFClient. Legacy DeliveryLeg без manifest сохраняет старый путь, но не допускает multi-unit reservation без явного manifest. Fake ASF и dispatch tests покрывают точный Amount pass-through и отказ при расхождении amount/reservation. Это транспортно-диспетчерский мост; генерация amount manifest из ListingStockService order allocations ещё не подключена.


**C.3 DeliveryLeg amount manifest regression tests (2026-11-23):** Добавлены проверки, что migration `2026-11-delivery-leg-asset-amounts-v1` реально создаёт колонку и фиксируется в `gd_schema_migrations`; `DeliveryLedger.add_delivery_leg()` отклоняет неполные/нулевые/boolean amounts; dispatcher блокирует повреждённый JSON manifest до создания DeliveryAttempt; и ASF получает exact map только при совпадении с reservation units. Исправлен ASF fake в pass-through тесте, чтобы он возвращал успешный результат после проверки фактически переданного amount map.


**C.3 Deferred DeliveryLeg migration ordering fix (2026-11-23):** Полный CI выявил, что generic Playerok migration path запускается на базах без таблицы `gd_delivery_legs`. Новая additive migration ошибочно считала это фатальной ошибкой. Теперь она безопасно откладывается без записи migration ID, если таблица ещё не создана; следующий вызов после установки delivery ledger schema применит колонку штатно. Это сохраняет независимый порядок старых Playerok migration tests и не маскирует ошибку при наличии таблицы.


**C.3 DeliveryLeg amount migration transaction-ownership correction (2026-11-23):** CI run `45157671580` exposed a regression in the deferred migration guard: `apply_delivery_ledger_migrations()` may be invoked inside an existing Playerok migration transaction. The guard previously issued a nested `BEGIN IMMEDIATE` and then rolled back the caller's transaction when `gd_delivery_legs` was absent. The schema-presence check now runs before opening the migration transaction and simply returns `False` when deferred; it no longer starts or rolls back a transaction it does not own. This preserves pending caller writes and allows the DeliveryLeg migration to apply later when the ledger table exists. No schema or delivery behavior changes.


**C.3 Listing quantity stock → DeliveryLeg integration (2026-11-23):** Подключён typed listing stock transfer к prepare_recipe_order и reserve_recipe_plan. Если для ConfigVersion существует allocation batch, Steam Asset RecipeItem больше не выбирает отдельные Asset IDs из ASF snapshots: его точные listing-owned units переносятся в order внутри той же SQLite-транзакции, проверяются по OrderLine/RecipeItem/Asset identity, затем атомарно конвертируются в canonical gd_resource_reservations.units. DeliveryLeg получает точный asset_amounts_json; Dispatcher сверяет его с canonical reservation и передаёт Amount в ASF. Без listing allocation batch сохраняется прежний one-asset-per-unit путь. Steam Cards не проходят через ListingStockService и сохраняют прежний selector/preflight/reservation flow. Добавлен integration regression test: stack Amount 4 + 6 из двух ботов превращается в две DeliveryLeg с Amount 4/6 и canonical reservations; повторная подготовка не создаёт дубликатов.


**C.3 Listing stock integration test corrections (2026-11-23):** Первый CI-прогон новой интеграции выявил только две регрессионные проблемы тестового слоя: тест не импортировал json для проверки asset_amounts_json, а текст ошибки при неверном количестве Steam Asset не содержал прежний диагностический маркер quantity. Добавлен импорт и сохранён ожидаемый маркер; поведение резерва/доставки не менялось.


**C.3 Quantity stock as the live per-order reservation source (2026-11-23):** Дополнительная проверка выявила, что глобальный unique index canonical gd_resource_reservations допускает только одну активную запись на физический Asset ID и тем самым не поддерживает одновременное резервирование разных частей одного стека разными заказами. Quantity-stock allocations теперь остаются активными order-owned rows в gd_stock_allocations; canonical exclusive reservation создаётся только для прежнего one-asset-per-unit и Steam Cards пути. Dispatcher сверяет DeliveryLeg.asset_amounts_json с суммой активных order allocations и глобальным объёмом blocking allocations относительно usable observation. ACCEPTED reconciliation переводит order allocations в consumed с текущим inventory revision; завершение заказа учитывает как canonical reservations, так и quantity allocations. Отмена до dispatch и operator release после доказанного отсутствия offer возвращают released allocations обратно в исходную активную listing generation, если объявление не отключено и observation остаётся usable/tradable; иначе операция откатывается fail-closed. Добавлены проверки двух одновременных заказов по 3 единицы из одного стека 10, возврата 3 единиц при отмене и ASF dispatch/reconciliation exact Amount.


**C.3 Quantity stock operator recovery hardening (2026-11-23):** Operator retry теперь требует, чтобы свежий ASF snapshot содержал на исходном боте не только tradable Asset ID, но и не меньше точного Amount из DeliveryLeg. Operator release/cancel сверяет aggregate active order allocations с asset_amounts_json и глобальным blocking quantity, затем освобождает именно эти allocations и возвращает units в listing generation в той же транзакции. Canonical reservation path для обычных ресурсов сохранён.


**C.3 Operator cancel audit ordering correction (2026-11-23):** CI выявил, что cancel_order формировал operator evidence до получения результата release/restore и обращался к restored_units раньше присваивания. Перенесено создание audit row после атомарного release/restore; restored_listing_units теперь фиксируется и возвращается в результате отмены. Это устраняет UnboundLocalError на прежнем canonical recovery path и сохраняет audit evidence для quantity stock.


**C.3 Quantity stock dispatch fixture ordering correction (2026-11-23):** CI выявил, что новый dispatcher integration test создавал stock observation до снятия test-only canonical reservation. QuantityStockLedger корректно помечал такую observation как conflict. Тест теперь сначала освобождает canonical fixture reservation и только затем создаёт usable observation и order allocation; production logic не менялась.


**C.3 Quantity stock operator release inventory proof (2026-11-23):** Operator release and ACTION_REQUIRED cancellation now also require a fresh ASF inventory snapshot for the original bot, with every Asset ID tradable and live stack Amount at least equal to DeliveryLeg.asset_amounts_json. This prevents returning a stale or depleted stack allocation to an active listing after an offer-absence check. Added operator recovery tests for exact Amount acceptance and rejection when the live stack contains fewer units than the reserved Amount.


**C.3 Recovery quantity test fixture config reference correction (2026-11-23):** В новом test fixture для operator quantity release использован несуществующий convenience method config_for_order. Заменено на сохранённый ConfigVersion ID, возвращённый при создании тестового объявления. Production code не менялся.


**C.3 reconciliation app/context scoping correction (2026-11-23):** Review обнаружил, что `DeliveryReconciliation._consume_reservations()` завершал canonical reservations и quantity stock allocations только по order_line_id, bot_name и Asset ID. Теперь оба UPDATE дополнительно ограничены `app_id/context_id` текущей DeliveryLeg, чтобы accepted offer не мог пометить consumed соседний ресурс из другого inventory context с совпавшим Asset ID. Добавлен integration regression test с одинаковым Asset ID в context 2 и context 6: принятие leg context 2 должно consume только его allocation, сохраняя соседнюю запись active.

**ASF inventory AssetID JSON type compatibility investigation (2026-11-23):** В массовом refresh игровых инвентарей путь проходит через стандартный ASF IPC и `games.inventory.parse_game_inventory()`; delivery plugin `TradeDelivery.cs` в этом пути не вызывается. Точная ошибка `asset_id должен быть непустой строкой` возникает на границе нормализации AssetID: общий валидатор требует внутреннюю строку, тогда как JSON-представление ASF может передать UInt64 AssetID числом. Планируемая коррекция ограничена входным адаптером ASF: принимать положительный UInt64 как decimal string, сохранять строгую проверку внутренних Asset ID, не преобразовывать bool/float/нулевые/отрицательные/overflow значения. Пустой `Assets: []` уже является допустимым пустым снимком. Проверка реального сырого ответа конкретного аккаунта ещё не выполнена; после изменений обязательны regression tests и CI. Steam Cards, delivery plugin, trade logic и license system не меняются.

**ASF numeric AssetID normalization implementation (2026-11-23):** В `games/asset_ids.py` добавлен отдельный `normalize_asf_asset_id()` для входной границы ASF: строковые ID проходят прежнюю строгую проверку, положительные JSON integers в диапазоне UInt64 преобразуются в десятичную строку; bool, float, ноль, отрицательные и overflow отклоняются. `games/inventory.py::parse_game_inventory()` использует нормализатор до создания `InventoryItem`; внутренний `validate_asset_id()` и остальные внутренние контракты не ослаблены. Добавлены тесты на UInt64 ID выше JS safe-integer и на недопустимые числовые типы/значения. Пустой `Assets: []` остаётся допустимым (уже покрыт worker regression test). Изменены только generic game inventory parser, общий asset-ID boundary helper и его тесты; `TradeDelivery.cs`, торговые операции, Steam Cards flow и лицензирование не менялись. Commits: `76b33c5`, `9960045`, `0f62e46`. Локальное выполнение тестов недоступно в этой сессии; требуется GitHub Actions verification. Фактический сырой ответ одного проблемного бота ещё не получен, поэтому это проверяемая совместимость гипотеза, а не подтверждённый дамп конкретного аккаунта.

**ASF AssetID CI regression correction (2026-11-23):** Первый CI после ввода ASF-boundary normalizer выявил ожидаемое расхождение со старым `test_general_game_parser_validates_raw_asset_id`: общий набор invalid inputs включал положительное число `123`, которое теперь допустимо только на внешней границе generic ASF game parser. Тест разделён по контрактам: положительный JSON integer нормализуется в строку только при разборе ASF payload; `InventoryItem` и внутренние модели по-прежнему отклоняют integer; Steam Cards parser и остальные strict consumers продолжают отклонять числовые ID. Добавлена явная проверка этого разделения. Исправление теста: commit `d870c03`. Первый CI run `45173426609`: Windows build passed; Tests failed только на старом ожидании integer=123; изолированный прогон подтвердил тот же единственный assertion failure. После обновления теста требуется повторный CI.

**ASF AssetID whitespace test fixture correction (2026-11-23):** Повторный CI на HEAD `1ed9707` выявил единственный assertion failure в `test_asset_ids.py`: тест передавал литеральные символы `\\t\\n` (обратные слеши), а не реальные tab/newline, поэтому валидатор закономерно принимал непустую строку. Исправлен fixture на фактические пробельные символы. Это ошибка самого теста, не production-кода. Windows build на `1ed9707` прошёл; Tests упал только на этом fixture. Исправление: commit `a76ffeb`. Ожидается новый CI.

**ASF AssetID fixture second correction (2026-11-23):** CI run `45178666988` повторно упал на том же тесте: предыдущая попытка замены сохранила escape-последовательность, а не фактические whitespace. Теперь fixture формирует значение выражением `" " + chr(9) + chr(10)`, исключая неоднозначность экранирования. Windows build run `45178667028` успешен. Новый тестовый commit: `a66fe5d`; ожидается CI.

**ASF AssetID whitespace fixture follow-up (2026-11-23):** CI на `a76ffeb` всё ещё падал в `test_general_game_parser_validates_raw_asset_id`, потому что runner фактически видел в тестовом значении лишний обратный слеш перед tab/newline (`value=' \\\t\n'`), то есть строка не была whitespace-only. Упростил fixture до прямого Python литерала `"\t\n"`; проверил содержимое файла в GitHub: исходник содержит `"\\t\\n"`, что при исполнении Python даёт только tab/newline и должно быть отвергнуто `validate_asset_id()` после `strip()`. Production code не менялся. Исправление: commit `9c0b6c1`. Требуется повторный CI.


**ASF inventory refresh — user acceptance check (2026-11-24):** Пользователь обновил инвентари ботов в запущенной локальной сборке GameDelivery и подтвердил: «успешно все заработало как и надо». Это подтверждает успешное выполнение массового refresh в его текущей среде после ASF numeric AssetID normalization. В этой сессии не получен сырой ASF payload конкретного аккаунта и не выполнен отдельный read-only запрос `730/2`; поэтому данный результат фиксируется как успешная пользовательская проверка refresh, но не как доказательство полного покрытия всех аккаунтов/типов инвентаря или завершения C.6.1. Торговые операции не запускались.

## 2026-11-24 — подтверждение успешного обновления инвентарей всех ботов

Пользователь выполнил проверку в запущенной GameDelivery после исправления обновления инвентарей и сообщил, что обновление всех ботов завершилось успешно и работает ожидаемым образом. Это зафиксировано как результат пользовательской проверки реального окружения; отдельные логи/скриншоты и независимая проверка каждого ответа ASF в этой записи не заявляются.

Следующий порядок работ согласован:
1. Проверить ASF Generic 6.3.11.7 и read-only получение CS2 inventory через AppID 730 / ContextID 2: подтвердить реальные AssetID и доступные поля Asset. Не выполнять trade/send и не создавать реальные offers.
2. Продолжить аудит C.6.2 CS2 View Model: отображение предметов, точных идентификаторов, количества и принадлежности конкретным ботам.
3. Вернуться к глобальному складскому учёту объявлений: общий пул разрешённых ботов, точные allocations по AssetID/units, последовательное использование свободного остатка при повторных продажах и запрет превышения заказанного количества.
4. Проверять доставку раздельно для игровых предметов и Steam Cards; не объединять их game handler/контракты.

Ограничения: только ветка work/local-sync; main и PR #4 не изменять; лицензионную систему не менять; работающую доставку не ослаблять; реальные обмены не выполнять без отдельного согласования. Эта запись подтверждает только успешное обновление инвентарей по сообщению пользователя и не закрывает CS2 GET acceptance, C.6.2 или глобальный stock allocation.


**C.6.2 CS2 card quantity and ownership visibility (2026-11-24):** Аудит CS2 View Model выявил расхождение между физическими ASF asset rows и количеством единиц в стеке: карточка показывала `group.instance_count` как «Количество», хотя реальное количество единиц определяется суммой `Amount` по физическим AssetID. Карточка теперь показывает `total_amount` в единицах и отдельно число физических ASF assets; если хотя бы у одного Asset Amount неизвестен, UI явно сообщает о неполном количестве и известной сумме, не подменяя неизвестное нулём. Tooltip карточки теперь раскрывает каждый bot/AssetID с Amount, ClassID, InstanceID, transferability, marketability и restriction state. Добавлены regression tests для stack Amount на нескольких ботах и для неизвестного Amount. Изменены только `app/game_inventory_cards.py`, `tests/test_cs2_inventory_view.py` и этот журнал. Коммиты: `8308ccd` (UI), `5de1262` (tests). GitHub Actions Tests и Windows executable workflow запущены на HEAD `5de12624a074d814667ede46398992d38968b4c8`; результаты ожидаются. Локальный Windows runtime и реальный ASF GET 730/2 этой записью не подтверждаются; торговые операции не выполнялись.


**C.6.2 CS2 card quantity label layout follow-up (2026-11-24):** Для новой двухчастной подписи «единицы стека · физические ASF assets» включён перенос строк и ограничена высота label, чтобы текст не обрезался в фиксированной ширине карточки. Commit: `c048a60`. Проверка остаётся в GitHub Actions.


**C.6.1 ASF CS2 route regression coverage (2026-11-24):** Добавлен отдельный тест `test_cs2_inventory_uses_read_only_730_2_route`: проверяет точный ASF IPC path `/Api/Bot/{bot}/Inventory/730/2?language=english`, HTTP GET, отсутствие request body и сохранение ответа Assets/Descriptions с AssetID/ClassID/InstanceID/Amount. Это автоматическая проверка корректности маршрута и read-only характера вызова; она не подменяет live-запрос к локальному ASF пользователя. Steam Cards route test 753/6 остаётся отдельным. Commit теста: `d7e9fa2`. Текущая ветка: `work/local-sync`; main и PR #4 не затронуты.


**C.6.2 CS2 card regression test alignment (2026-11-24):** Первый CI-прогон после изменения карточек выявил два устаревших ожидания в `tests/test_cs2_inventory_panel.py`: старый тест ожидал суммировать число строк ASF (2), хотя новый контракт корректно показывает сумму Amount (3), а tooltip намеренно исключал физические поля, которые теперь должны быть доступны для аудита каждого AssetID. Обновлены проверки: UI показывает `3 шт.` и `2` физических ASF assets; tooltip проверяет отдельные Bot/AssetID/Amount/ClassID/InstanceID и независимые transfer/marketability/restriction статусы. Production logic не ослаблялась; тесты уточняют новый контракт. Commit: `b1d9539`. Windows executable build на предыдущем HEAD прошёл; ASF plugin build прошёл; полный Tests workflow на предыдущем HEAD завершился failure именно на двух этих assertions (и exit 139 при завершении Qt процесса после изолированного запуска модуля). Новый прогон ожидается.

**C.6.2 CS2 card CI verification (2026-11-24):** GitHub Actions завершили оба workflow успешно на commit `b1d9539e774cc6932d5ae4c78398688407db7609`: Tests run `45367497675` (#2020) — success, включая полный unit test suite и изоляционный шаг Qt shutdown; Windows Build run `45367497190` — success, включая compile, Windows GUI executable build, packaged executable smoke test и upload artifact. Исправленные CS2 quantity/physical-asset assertions прошли. После CI был создан documentation-only commit `e6760f5386346f7c9b469d208f4c2458042e4af5`; текущий HEAD ветки `work/local-sync` — этот commit. Это подтверждает GitHub CI для указанного кода, но не заменяет локальную проверку Windows-сборки пользователя, live ASF GET 730/2 или реальные Steam offers. Реальные trade/send операции не выполнялись.

**C.3 Listing stock observation freshness hardening (2026-11-24):** Аудит общего склада выявил, что `availability_state='usable'` не ограничивал возраст stock observation: старый снимок мог оставаться usable и использоваться при создании listing allocation, top-up, reallocation, переносе units в OrderLine или возврате order allocation обратно в объявление. В `core/delivery/listing_stock.py` введён единый предел свежести 5 минут с проверкой корректного timestamp и запретом будущих timestamp. Проверка добавлена к legacy и typed allocation/top-up, typed reallocation (source и destination), transfer listing→order и restore order→listing. Операции release/disable, которые должны позволять безопасно снять старый listing claim, не требуют свежего inventory snapshot. Добавлены regression tests: usable-but-stale observation не создаёт ни allocation batch, ни частичных allocations; stale snapshot блокирует transfer без изменения listing/order ownership и audit. Изменены `core/delivery/listing_stock.py`, `tests/test_listing_stock_service.py` и этот журнал. Commits: implementation `18636057`, tests `a93d97b`. GitHub Actions verification ожидается; локальные Windows/ASF runtime и реальные offers не запускались. Работа ведётся только в `work/local-sync`; main, PR #4 и лицензирование не затрагивались.

**C.3 Listing stock freshness policy — shared ledger enforcement (2026-11-24):** Freshness validation вынесена в общий helper `is_fresh_stock_observation()` в `core/delivery/stock_ledger.py` с единым пределом 5 минут. Теперь прямой `QuantityStockLedger.allocate()` отклоняет старые usable observations; `free_units()` возвращает 0 для устаревшего снимка; `eligible_resources()` исключает его из доступных кандидатов. `ListingStockService` использует тот же helper для legacy/typed allocate и top-up, typed reallocation source/destination, listing→order transfer и order→listing restore. Это закрывает обход freshness через прямой ledger API и не позволяет UI/API показывать старый usable snapshot как свободный склад. Добавлены tests для старого usable observation в прямом allocate, free stock projection и eligible resources, а также для typed listing allocation/transfer. Изменения: `core/delivery/stock_ledger.py`, `core/delivery/listing_stock.py`, `tests/test_quantity_stock_ledger.py`, `tests/test_listing_stock_service.py`. Commits: `18636057`, `a93d97b`, `4791ecf`, `08041ed`, `61fc42d`, `007f9f3`. Первый полный Tests workflow на промежуточном commit `18636057` завершился success (run `45369167024`); после дополнительных изменений shared helper и direct allocation regression tests требуется полный CI на текущем HEAD. Реальные Steam offers не выполнялись.

**C.3 Remaining application integration gap (2026-11-24):** Кодовый аудит подтвердил, что quantity stock service и atomic listing→order transfer существуют, но обычный UI workflow ещё не подключён к ним: `PlayerokListingConfigService.save_recipe()` не вызывается из интерфейса; форма Playerok сохраняет ConfigVersion через legacy `save()` и не создаёт listing allocation batch; `app/main.py::_prepare_ledger_order()` пока выбирает только Steam Cards или legacy CS2 `prepare_cs2_order()`, не вызывая `prepare_recipe_order()`. Поэтому текущие unit/integration tests подтверждают внутренний stock engine, но не end-to-end сценарий «сохранить объявление → выделить общий остаток → оплатить → перенести exact units → подготовить delivery». Следующий implementation slice — подключить typed Recipe и явное количество складского остатка к интерфейсу/сервису, затем направить typed CS2 order preparation через `prepare_recipe_order()`; до этого не считать глобальный склад полностью завершённым. Не менять рабочую Steam Cards ветку и не запускать реальные offers.

**C.3 Listing stock freshness CI verification and regression correction (2026-11-24):** CI run `45369826045` на промежуточном HEAD `86168f44` выявил один failure в новом тесте: typed `allocate_recipe_items()` не пропускал observations через общий freshness helper, хотя соседний typed top-up path и transfer path уже проверяли возраст. В `core/delivery/listing_stock.py` добавлен недостающий фильтр свежести в первоначальный typed allocation. Correction commit: `723565dbc754cf8f8fe8b96b21ce98be21ac9c28`. Повторный полный CI на этом commit завершился success: Tests run `45370316301` — 1047 tests, все assertions OK; workflow также изолированно выполнил каждый test module после известного Qt shutdown exit 139, все модули завершились успешно. Следовательно, остаётся отдельное предупреждение о segfault Python/Qt при завершении общего процесса, но не о падении assertions. Windows Build run `45370316291` — success: compile, trade-secret ACL test, Windows GUI executable build, packaged executable smoke test и artifact upload. Artifact: `GameDelivery-Windows-x64`, ID `14909016692` (238,037,611 bytes). GitHub branch `work/local-sync` на момент проверки указывает на `723565dbc754cf8f8fe8b96b21ce98be21ac9c28`. Это подтверждает CI и packaging, но не выполнение новой сборки на локальном ПК пользователя и не live ASF/Steam exchange. Реальные offers не создавались.

**C.3 Playerok CS2 listing quantity → shared stock → typed order preparation (2026-11-24):** Подключён первый end-to-end UI slice для однозначных CS2 listings. `PlayerokListingConfigService.allocate_listing_stock()` проверяет source platform, active/current ConfigVersion и typed CS2 recipe; количество резервируемых единиц вычисляется как `listing_quantity × quantity_per_order` для каждой обязательной RecipeItem. Idempotent request ID детерминирован от ConfigVersion и mapping единиц. Резервирование использует общий `ListingStockService.allocate_recipe_items()` и свежие наблюдения, распределяет единицы по разрешённому пулу ASF-ботов. В `PlayerokBrowserPanel` добавлено отдельное поле «Количество в объявлении»; пустой bot pool по умолчанию заполняется найденными ASF-ботами. После сохранения CS2 ConfigVersion система пытается выделить общий остаток; при ошибке ConfigVersion остаётся сохранённой, но интерфейс явно сообщает об отсутствии резерва. Несколько CS2 selectors остаются альтернативами и не выдаются за независимые обязательства — для них global allocation не создаётся.

В `app/main.py` typed CS2 Recipe направляется в `prepare_recipe_order()`; Playerok typed ConfigVersion без listing stock batch блокируется. Существующий batch переводит точные AssetID/Amount units в OrderLine через атомарный listing→order transfer. В новом UI пути автоматическая отправка Steam offer отключена: выполняется только подготовка/резервирование, реальная отправка требует отдельного ручного действия. Steam Cards и mixed Recipe не перенаправлялись на CS2 обработчик.

Regression coverage: `test_playerok_listing_quantity_allocates_recipe_units_from_shared_stock` (3 продажи × 2 единицы = 6 резервируемых единиц, повторный вызов идемпотентен); ambiguous/superseded ConfigVersion rejection; UI save + stock allocation; typed CS2 UI order preparation с уже существующим batch и запретом auto-send. Полный CI на commit `1bf01e7c241524f96158f0b526f993d99e3bf694`: Tests run `45373158811` — 1051 tests, success; каждый изолированный test module также прошёл. Windows Build run `45373158849` — success, executable smoke test success, artifact upload success. На предыдущем run `45372527342` единственная ошибка была в fixture test (stock migrations не инициализировались до observe); исправлено и подтверждено успешным повторным CI. Qt shutdown exit 139 в этом прогоне не воспроизвёлся. Реальный Playerok listing, local Windows runtime, live ASF snapshot и Steam offers не использовались.

Оставшаяся граница: UI global stock сейчас поддерживает однозначный typed CS2 Steam Asset recipe. Множественные альтернативные selectors, Steam Cards и mixed recipes требуют отдельного безопасного allocation/execution path; существующий legacy CS2 и Steam Cards flow не переписан. Работа — только `work/local-sync`; main, PR #4 и лицензирование не затрагивались.

**Steam Cards Playerok listing set-limit verification (2026-11-24):** Implemented the separate listing-wide complete-set limit on `work/local-sync`, following the authoritative requirement at the top of this log. The Playerok Steam Cards editor accepts one explicit total set count for the listing; it does not infer that value from per-order quantity. `delivery_quantity_per_deal` remains unchanged and is stored independently. Saving Steam Cards ConfigVersion without an explicit listing set count now fails before creating the listing/config row. The existing buyer badge/inventory and seller inventory preflight remains unchanged and continues choosing the exact cards automatically from available bot inventories.

At order reservation, the number of complete sets selected by the existing verified preflight is claimed against the listing's total quota in the same `BEGIN IMMEDIATE` SQLite transaction that commits the canonical card reservations and DeliveryLegs. The quota is shared across ConfigVersions of the same listing. If no listing quota exists, Playerok Steam Cards reservation fails closed. If the remaining quota is insufficient, pipeline result is `preflight_blocked` (the pipeline deliberately translates `InsufficientListingStock` to this result); no claim or delivery reservations are committed. Cancellation before any ASF attempt releases the listing-set claim in the same cancellation transaction. After dispatch has begun, cancellation cannot release it.

Regression coverage added/updated: explicit listing count separate from per-order quantity; omission of listing total fails without creating a listing; existing claimed sets cannot be undercut by lowering the limit; selected one complete set consumes one quota unit even when it contains two physical cards; a second order is blocked at a one-set limit even when another complete set exists; pre-dispatch cancellation releases the first claim and a later order can claim it; missing limit fails closed. No per-game split, card/AssetID assignment, per-bot setting, or physical listing-owned card pool was introduced.

Changes: `integrations/playerok/listing_config.py`, `tests/test_playerok_browser_and_listing_config.py`, `tests/test_recipe_unified_reservations.py`, plus this journal. Commits: `ea92d25aff0f67c7c6054b79abff9f7a3ebb1a69` (require explicit separate listing count), `220783db7754da188858cdb71ada1d5e56f79154` (config/validation tests), `8f4a7efbd082fa81aba47075a9186f76be956872` (listing quota order/cancel tests), `48e982e35dd489c67dacef47dd0efbcbf88580ea` (align test with the pipeline's fail-closed `preflight_blocked` result; production logic unchanged).

**CI on code HEAD `48e982e35dd489c67dacef47dd0efbcbf88580ea`:** Tests run `45426620129` — SUCCESS; full suite reported 1,057 tests in 45.6s with all assertions passing, and the workflow's isolated per-module verification also succeeded. The workflow emits a technical warning that the shared Python/Qt process exits with code 139 during interpreter shutdown after the successful assertions; it then runs every test module separately, and each module passed. Windows Build run `45426620097` — SUCCESS: Python compile, Windows trade-secret ACL hardening, GUI executable packaging, packaged executable smoke test, and artifact upload all passed. Artifact `GameDelivery-Windows-x64`, ID `14936713290`, size 238,050,339 bytes. This confirms CI and packaging, not a launch on the user's local Windows machine, not a live ASF inventory refresh, and not a real Steam exchange. No Steam offers were sent. Work remained on `work/local-sync`; `main`, PR #4, and licensing were not changed.


**Steam Cards automatic all-bot reservation — CI verified (2026-11-25):** Follow-up to the Playerok listing-wide complete-set quota and removal of the manual Steam Cards bot picker. New Steam Cards RecipeItems may keep `allowed_bot_pool=[]`; the buyer-specific preflight selects exact cards from all available verified AppID 753 / ContextID 6 snapshots. Canonical reservation validation now accepts that empty configured pool only for `SteamCardSetSpec`, while still binding each reserved physical card to the immutable successful preflight selection (bot, AssetID, MarketHashName, exact complete-set composition). Explicit bot pools remain enforced when present; Steam Asset/CS2 requirements remain unchanged. No user-configured card pool, AssetID assignment, per-game split, or per-bot allocation was introduced. Commits: `3624d323b11b5382ebc35acc7cd04e695bd2b96c` (Recipe validation), `5b917ca8a8519162b9c48cd6f61979f0de41dcb1` (reservation validation), `7b474bc05a0f194102688d46cefb0814d706acc9` (allow empty bot pool for verified Steam Cards preflight). CI on final HEAD `7b474bc05a0f194102688d46cefb0814d706acc9`: Tests run `45613974338` succeeded; Windows Build run `45613974369` succeeded and uploaded `GameDelivery-Windows-x64` artifact ID `15021223060` (not expired at verification). Earlier failed run `45561190774` correctly exposed the missing empty-pool exception in canonical reservation validation; the targeted fix was applied and both final workflows passed. No live Steam offers were sent.


**C.4 versioned delivery-handler contract foundation (2026-11-25):** Добавлен изолированный `core/delivery/handler_contract.py`: immutable `DeliveryHandlerKey` `(item_type, delivery_method)`, versioned `DeliveryHandlerV1` Protocol с обязательными `supports/preview/prepare/execute/reconcile`, нормализованный `DeliveryHandlerResult` и точный `DeliveryHandlerRegistry`. Registry fail-closed: не принимает неизвестную версию/неполный handler/повторный ключ, не выбирает по display name и отклоняет неподдерживаемый запрос. Добавлен `tests/test_delivery_handler_contract.py` с проверками нормализации ключа, валидации результата, exact-key routing, duplicate registration, unsupported request и contract version. Коммиты: contract `401873fe0b771ce8472795c9acdb050aa6e5f418`; tests `514cf0ac5aaa25a25d0728e4684b33571621b2c5`. GitHub Actions для HEAD `514cf0ac5aaa25a25d0728e4684b33571621b2c5` запущены: Tests run `45662577336` (pending/queued на момент записи), Windows Build run `45662577380` (in progress на момент записи). CI пока не считать пройденным. Это только контракт и registry foundation: существующий `DeliveryDispatcher`, Steam Cards preflight/reservation, CS2 и ASF transport не подключались и не менялись; Telegram и proxy не начинались. Нет live Steam/Playerok действий. Работа только в `work/local-sync`; `main` и PR #4 не затрагивались.


**C.4 handler contract foundation — CI verification (2026-11-25):** На актуальном HEAD `4502f355ab2891c91a6bd05669f9851f5d6e25f1` в ветке `work/local-sync` GitHub Actions завершились успешно: Tests run `45662731355` — **success** (compile, credential/migration checks, delivery reconciliation/recovery, Steam trade UI flow, полный unit test suite); Windows Build run `45662731289` — **success** (compile, Windows trade-secret ACL test, GUI executable build, packaged smoke test и artifact upload). Проверен именно актуальный HEAD после добавления контракта, тестов и этой записи. Это подтверждает CI для изолированного C.4 contract foundation, но не закрывает весь C.4 и не означает, что Steam Cards handler уже подключён: dispatcher и действующие Steam Cards/CS2 delivery paths не менялись. Следующий этап — изучить фактический существующий Steam Cards preflight/reservation/ASF execution path и подключить его как первый handler без переписывания алгоритма, с тестами на сохранение поведения. Никаких live Steam offers или Playerok действий не выполнялось; Telegram не начинался; `main` и PR #4 не затрагивались.
---

## C.3 — payment-time ListingStock handoff (2026-11-25)

**Проверенная точка:** HEAD `e9f04f3430439d448341d695bc520785046c5162`, ветка `work/local-sync`.

### Изменение

- В `integrations/playerok/sync.py::_maybe_create_order` typed Recipe путь теперь передаёт уже выделенные ListingStock units соответствующим OrderLine в той же транзакции, где фиксируются PAID Deal, Order и полный OrderLine manifest.
- Переводы сгруппированы savepoint-ом. Если обязательного ListingStock объёма не хватает, все переводы этого handoff откатываются; PAID Deal и полный manifest остаются, Order получает `preflight_blocked`. Необязательный RecipeItem без полного выделенного количества пропускается.
- Для transfer используется тот же стабильный idempotency key, что и в `DeliveryReservationService`, чтобы последующая подготовка не создавала повторный transfer.
- ASF/Steam сетевые вызовы в sync не добавлялись.

### Проверка

- Добавлен assert в `tests/test_playerok_listing_stock_handoff.py`: ListingStock transfer и active order allocation уже существуют сразу после paid sync, до вызова `prepare_recipe_order`.
- GitHub Actions на HEAD `e9f04f3`: Tests — **success** (run `45671188888`); Windows executable build — **success** (run `45671188945`).
- Изменены только `integrations/playerok/sync.py` и `tests/test_playerok_listing_stock_handoff.py`.

### Ограничение / следующий шаг

Это закрывает только атомарный payment-time ListingStock handoff. Это **не** закрывает полный C.3 criterion по единой транзакции для Order + manifest + initial DeliveryLeg/canonical reservations после pure planning. В частности, создание DeliveryLeg и физических reservations по-прежнему происходит позднее в reservation pipeline. C.3 остаётся **OPEN**. Следующий шаг — transaction-aware commit path для typed Recipe после planning: в одной короткой транзакции перепроверить Order/ConfigVersion/eligibility/conflicts, создать или подтвердить Order + manifest, перенести ListingStock allocations (идемпотентно), создать весь набор DeliveryLeg/canonical reservations и обновить статус. ASF snapshots и verified metadata должны быть получены до открытия этой транзакции. Сохранить публичный `reserve_recipe_plan` API и отдельные Steam Cards/legacy CS2 flows.


**C.3 concurrent same-order preparation regression — CI verified (2026-11-25):** Added `test_parallel_preparations_of_same_order_are_idempotent` to force two concurrent `prepare_recipe_order()` calls for the same order past initial config lookup together. The test asserts both calls converge on the same reserved legs/assets and that only one order-line/leg/resource-reservation manifest is committed. The first test revision incorrectly expected two rows in the legacy `steam_asset_reservations` compatibility table for a CS2-only scenario; CI exposed this test expectation error. Corrected the assertion to expect zero rows there; canonical CS2 reservations remain asserted in `gd_resource_reservations`. No production code changed in these test commits. Commits: `b2a518b148ad64fdef8028791d4959e7cc37c3b3` (concurrency regression test), `3ec57ab28e1a22adcbc37be6612b62c825ac99e1` (correct CS2/card-table assertion). CI on final HEAD `3ec57ab28e1a22adcbc37be6612b62c825ac99e1`: Tests run `45693719918` — **success**; Windows Build run `45693719792` — **success**. This verifies the regression test and Windows packaging workflow; it does not close the remaining C.3 atomic Order/manifest/DeliveryLeg/canonical-reservation transaction criterion. No live ASF/Steam offers were created. Work remained on `work/local-sync`; `main`, PR #4 and licensing were not changed.


**C.3 listing-stock reservation rollback — CI verification (2026-11-25):** Проверен точный branch HEAD `3a5d0df43864fda014d654f83393941750ef8ef9` (`work/local-sync`). GitHub Actions на этом SHA: Tests run `45695003868` — **success**; Windows Build run `45695003853` — **success**. Новый fault-injection regression test в `tests/test_playerok_listing_stock_handoff.py` создаёт SQLite trigger, принудительно отклоняющий INSERT в `gd_delivery_legs`, и проверяет rollback OrderLine manifest, ListingStock transfer, order-owned allocations, DeliveryLeg и canonical reservations; исходная listing allocation остаётся active. Затем trigger удаляется, и штатная подготовка заказа успешно завершается. Production code не менялся. Текущий остаточный C.3 gap остаётся прежним: PAID sync создаёт Order до reservation transaction, тогда как acceptance требует атомарно охватить Order + manifest + ListingStock transfer + DeliveryLeg + canonical reservations. Автоматически выбрать payment-time ConfigVersion по доступному Playerok `createdAt` нельзя: это timestamp создания Deal, не подтверждённое время оплаты. Не реализовывать latest-version fallback и не создавать Order для unresolved PAID Deal. Операторский выбор точной ConfigVersion требует отдельного подтверждённого бизнес-процесса; до его согласования не менять этот boundary. Следующий шаг — согласовать источник доказательства точной ConfigVersion для PAID Deal, затем продолжить C.3 implementation. Steam Cards selection/quota semantics, CS2 selection, ASF transport и manual-review boundary не менялись; live Playerok/Steam actions не выполнялись; `main` и PR #4 не затрагивались.


**C.3 explicit ConfigVersion binding for PAID Playerok deals — implementation awaiting CI (2026-11-25):** В `integrations/playerok/sync.py` добавлена fail-closed операция `bind_paid_deal_config_version`: оператор выбирает точную ConfigVersion только из того же listing, указывает причину; binding audit, фиксация ConfigVersion в Deal и создание Order выполняются в одном `BEGIN IMMEDIATE`. Добавлен `playerok_paid_config_bindings` с уникальностью account+deal и неизменяемой записью выбора. Повторный polling сохраняет только audited binding; latest-version fallback отсутствует. Добавлен список допустимых версий для сделки. В `app/platforms_panel.py` добавлено действие выбора ConfigVersion для выбранной PAID-сделки с вводом причины. Добавлены тесты на успешную audited привязку, сохранение после повторного sync, запрет повторного выбора и отказ от версии другого объявления. Коммиты реализации/тестов/UI: `eed523de59ea669929187c77fde3ad6013415b0c`, `2ab062b2855d2d4d963c7305ba2955564d93e869`, `5b4ff74cd675d1cc177891d93b52d3a85795606c`, `e36683c3327202dc9a3a9325f9eb900388c2b128`, `f63388f19d7d0ca586412eed385d850e8f79aae5`, `a40fe110b63dd465bdeff7d6c03a1967e59b4b53`. На HEAD `a40fe11`: Tests run `45696686100` — pending; Windows Build `45696686052` — in progress (compile и ACL step прошли, GUI packaging выполняется). CI пока не считать успешным; после завершения проверить оба workflow и исправить найденные ошибки. Operator ID сейчас фиксируется как `desktop-operator`; это локальный идентификатор действия приложения, не подтверждённая учётная запись Windows. Никаких live Playerok/Steam mutations не выполнялось. Изменения только в `work/local-sync`; `main` и PR #4 не затрагивались.


## C.3 — Codex handoff: atomic PAID Deal → Order → reservations (2026-11-27)

- Проверена актуальная ветка `work/local-sync`, HEAD на момент проверки: `16ad554780f4c7702c3f2b3d6c332ead614d2960`.
- Повторно прочитаны Stage C plan и актуальные `sync.py`, `order_pipeline.py`, `reservation_service.py`. Остаточный gap подтверждён: PAID sync всё ещё вставляет Order shell до подготовки; pipeline требует persisted Order; reservation commit атомарен только для последующих manifest/transfer/legs/reservations.
- Подготовлен подробный Codex implementation handoff: `docs/superpowers/handoffs/C3_ATOMIC_PAID_ORDER_CODEX_HANDOFF.md`. В нём зафиксированы текущий call graph, требуемый lifecycle, атомарная успешная/blocked ветки, совместимые границы Steam Cards/CS2, fault-injection/idempotency/race tests и последовательность реализации.
- **Production code не изменялся. C.3 остаётся OPEN.** В этой сессии доступно чтение/запись GitHub-файлов, но нет подключённого локального checkout GameDelivery и исполняемой SQLite/Python test-среды; поэтому безопасно выполнить и проверить транзакционный рефакторинг здесь нельзя. Непроверенный production patch не публиковался.
- `main`, PR #4, licensing, реальные Playerok mutations и Steam offers не затрагивались.

## C.3 — atomic PAID preparation implementation slice (2026-11-27)

- На исходной контрольной точке `work/local-sync`, HEAD `bd1c28dd2f196e840fc8ea3180aebf8f63326eb0`, worktree был чистым и `origin/work/local-sync` совпадала. Код менялся только после проверки call sites sync, MarketplacePanel/MainWindow, `DeliveryOrderPipeline`, `DeliveryReservationService` и целевых тестов.
- PAID sync теперь сохраняет Deal и audited ConfigVersion binding, но не вставляет `gd_orders`/OrderLines. Явная привязка возвращает детерминированный будущий order ID и `awaiting_preparation`; окно площадок предлагает импортировать/подготовить только после привязки. Повторный sync сохраняет binding без материализации Order.
- Добавлен immutable `PlayerokPaidPreparation`: account, external/canonical deal, точный listing/config, audit binding/operator/reason и стабильный order ID. Перед commit контекст заново читается внутри `BEGIN IMMEDIATE`; изменение статуса, baseline, listing/config или audit binding блокирует запись.
- Для поддерживаемого typed/legacy Recipe пути `prepare_paid_recipe_deal()` планирует без ledger writes. Успешный `reserve_recipe_plan()` в одной транзакции создаёт Order, полный immutable OrderLine manifest, ListingStock transfers, DeliveryLegs, canonical reservations и reserved status. Ошибка после вставки Order/transfer откатывает всё. Недостаточный обязательный ресурс создаёт Order + полный manifest + `preflight_blocked` без legs/reservations; повтор подготовки идемпотентен. Existing order-based preparation остаётся доступным.
- Интеграция панели вызывает preparation только по операторскому действию Import. Вызов без свежих snapshot безопасно резервирует только заранее выделенный ListingStock; прочим Recipe возвращается blocked manifest, который можно продолжить существующим экраном подготовки. Сообщения, Playerok statuses, ASF и Steam offers не отправляются.
- Проверки: `python -m unittest tests.test_playerok_sync tests.test_playerok_listing_stock_handoff tests.test_recipe_unified_reservations tests.test_trade_delivery_ui tests.test_marketplace_integrations` — **87 passed**; `python -m compileall -q` по шести затронутым модулям — PASS; `git diff --check` — PASS (Git сообщил только стандартные предупреждения о LF→CRLF для изменённых текстовых файлов).
- Изменено 8 файлов: `app/main.py`, `app/platforms_panel.py`, `core/delivery/order_pipeline.py`, `core/delivery/playerok_eligibility.py`, `core/delivery/reservation_service.py`, `integrations/playerok/sync.py`, `tests/test_playerok_listing_stock_handoff.py`, `tests/test_playerok_sync.py`. Commit/Push не выполнялись; CI и GUI не запускались; использовались тестовые временные SQLite. Пользовательская БД и live-интеграции не открывались.
- **C.3 остаётся OPEN.** Новый atomic entrypoint проверен для Recipe, включая ListingStock, blocked preflight, stale status, rollback и retry. Старые legacy selector-конфигурации, которые `recipe_for_config_version(..., allow_legacy=True)` не может однозначно представить как Recipe, теперь fail closed и не получают Order; их прежний CS2/Steam Cards selector path требует отдельного transaction-aware commit без изменения selection/quota/manual-review semantics. Также нужны гонка смены ConfigVersion и UI-level regression для binding→Import перед закрытием C.3. Следующий конкретный шаг — реализовать/проверить atomic bridge старых selector flows, затем добавить binding/config race test и прогнать CI на финальном commit.

## 2026-11-27 — C.3 atomic PAID preparation: transaction acceptance coverage

- Сверено состояние `work/local-sync` на исходном SHA `bd1c28dd2f196e840fc8ea3180aebf8f63326eb0`; незакоммиченный код из предыдущего checkpoint сохранён. `PLAYEROK_LOCAL_REVIEW.patch` не читался и не изменялся.
- Уточнена совместимость legacy selector-конфигураций: `DeliveryLedger.recipe_for_config_version(..., allow_legacy=True)` использует `legacy_config_to_recipe()` для поддерживаемых однозначных CS2/Steam Cards форматов; неоднозначные/неподдерживаемые форматы по-прежнему fail closed до создания заказа. Самостоятельные `prepare_steam_card_order()` и `prepare_cs2_order()` не менялись.
- Усилены транзакционные regression tests: отказ после создания Order до manifest (`gd_order_lines` trigger), после manifest до transfer (`gd_stock_transfers` trigger), после ListingStock transfer до DeliveryLeg (`gd_delivery_legs` trigger), отказ canonical reservation INSERT, отсутствие частичных writes после каждого сбоя; commit-time ListingStock depletion и внешний resource conflict оставляют только blocked Order + manifest; Steam Cards quota race оставляет без legs/reservations/claim; успешный PAID Steam Cards path атомарно фиксирует полный набор, reservations и listing quota.
- Проверены stale PAID status между planning и commit; существующая проверка изменения закреплённой ConfigVersion, duplicate prepare, конкурентный retry и operator binding → import-кнопка сохранены в целевых тестах. UI import не создаёт Order до явного действия и передаёт связанный Deal/ConfigVersion в подготовку.
- Проверки: `python -m unittest tests.test_playerok_listing_stock_handoff tests.test_playerok_browser_and_listing_config tests.test_playerok_sync tests.test_recipe_unified_reservations tests.test_trade_delivery_ui tests.test_marketplace_integrations` — **143 passed**; `python -m compileall -q` по затронутым production/test модулям — PASS; `git diff --check` — PASS.
- Эта запись описывает локально проверенный блок; **C.3 не закрыт**: проверки выполнялись до коммита, CI для точного конечного SHA ещё не запускался. Остаются полная обязательная регрессия/CI и аудит точек интеграции C.4; текущий UI импорт передаёт пустые live snapshots и поэтому безопасно оставляет физические заказы blocked, пока операторский поток не предоставит свежие проверенные snapshots/Steam Cards metadata. Нет live Playerok/ASF/Steam действий, GUI-запусков или пользовательской БД.

### Addendum — full-suite cleanup errors corrected

- Первый полный `python -m unittest discover -s tests` дал 13 ошибок Windows `WinError 32`: временные тестовые SQLite/log файлы ещё были открыты в момент очистки. Повторная проверка конкретных тестов подтвердила, что `sqlite3.Connection` context manager не закрывает connection; в logging tests file handler закрывался только в `tearDown`, уже после временного каталога.
- Исправлены только тестовые cleanup paths: `tests/test_app_startup.py`, `tests/test_asf_proxy_store.py`, `tests/test_game_inventory_modules.py` используют проектный `connect_sqlite()` с `row_factory=None`, чтобы сохранить старые tuple assertions и гарантированно закрыть handle; `tests/test_diagnostics.py` закрывает handlers до выхода из TemporaryDirectory.
- Целевые cleanup-проверки: **28 passed**. Повторный полный прогон `python -m unittest discover -s tests` — **1088 passed**. C.3 focused suite — **143 passed**; migration/ledger suite — **29 passed**; targeted compileall и `git diff --check` — PASS.
- Исправление не меняет production storage behavior и не открывает пользовательскую БД. CI ещё не запускался на коммите; C.3 остаётся PARTIAL до проверки точного SHA в GitHub Actions.

### C.3 acceptance — exact-SHA CI passed (2026-11-27)

- Commit: `1c4aed2f18d6a2959a67ff0f63aa1d2af5efba70` (`Make paid Playerok preparation atomic`), pushed to `origin/work/local-sync`; local and remote refs verified equal. No force push; remote had no upstream-only commit before push.
- Tests workflow run `45983457580` — success; Windows executable build run `45983457516` — success, including packaged executable smoke; ASF plugin build run `45983457508` — success. All runs use exact HEAD `1c4aed2f18d6a2959a67ff0f63aa1d2af5efba70`.
- Local evidence: focused lifecycle suite **143 passed**; migration/ledger/reservation suite **29 passed**; cleanup regressions **28 passed**; full `python -m unittest discover -s tests` **1088 passed**; targeted compileall and `git diff --check` PASS. Historical controlled migration on user DB is recorded in the 2026-11-18 handoff entry, with backup, integrity/foreign-key checks and before/after row comparison; this task did not reopen it.
- C.3 acceptance is DONE for the supported typed Recipe and recognized unambiguous legacy Recipe paths: PAID sync/binding stay orderless; commit revalidates immutable account/deal/listing/config/audit context; one transaction covers stable Order, full manifest, ListingStock transfer, DeliveryLegs, canonical resources and Steam Cards quota; blocked paths contain only Order+manifest+blocked state; injected failures and conflicts prove rollback/idempotency/concurrency.
- Non-Recipe manual service/configs and unknown legacy selector formats remain fail-closed and are classified under C.7–C.9/future compatibility, not silently routed through legacy preparation. The Marketplace Import currently does not fetch live ASF/card metadata; without supplied verified snapshots it correctly records blocked preparation. Integration of verified providers into the C.4/C.5 paths remains open.
- Changed 16 files: `PROJECT_MASTER_LOG.md`, `app/main.py`, `app/platforms_panel.py`, `core/delivery/order_pipeline.py`, `core/delivery/playerok_eligibility.py`, `core/delivery/reservation_service.py`, `docs/superpowers/handoffs/C3_ATOMIC_PAID_ORDER_CODEX_HANDOFF.md`, `docs/superpowers/handoffs/PLAYEROK_SAFE_SYNC_HANDOFF.md`, `integrations/playerok/sync.py`, `tests/test_app_startup.py`, `tests/test_asf_proxy_store.py`, `tests/test_diagnostics.py`, `tests/test_game_inventory_modules.py`, `tests/test_playerok_browser_and_listing_config.py`, `tests/test_playerok_listing_stock_handoff.py`, `tests/test_playerok_sync.py`.
- No live Playerok mutations/messages, Steam offers, ASF sends, GUI startup or user DB access occurred in this block. `main`, PR #4, licensing and the protected local review patch were not touched.
## 2026-11-27 — C.4 handler routing integration checkpoint

- Подключён существующий versioned `DeliveryHandlerRegistry` к production `DeliveryDispatcher` и `DeliveryReconciliation`. Маршрут вычисляется только из неизменяемого `RecipeItem` snapshot после сверки с закреплённой ConfigVersion; ключ — `(item.kind, item.executor_key)`. Display name не участвует. Повреждённый/неподтверждённый snapshot и незарегистрированный маршрут завершаются fail-closed до ASF send.
- Текущий зарегистрированный путь — `steam_asset / steam_trade_offer`; handler делегирует исполнение неизменённому guarded ASF core и reconcile существующему reconciler. Selection, preflight, reservations, attempt idempotency, manual authorization, Playerok freshness и UNKNOWN handling не переписаны.
- Multi-leg manual dispatch после одного подтверждения продолжает остальные legs после локального исключения, фиксируя исключительную leg как `UNKNOWN`; Playerok freshness failure по-прежнему останавливает последующие sends.
- Целевые проверки: `tests.test_delivery_dispatcher`, `tests.test_delivery_handler_contract`, `tests.test_delivery_recovery`, `tests.test_delivery_chat_commands_telegram`, `tests.test_main_delivery_send` — **87 passed**. Compileall затронутых модулей и `git diff --check` — PASS.
- C.4 остаётся **PARTIAL**: у ledger пока нет отдельной durable родительской `DeliveryOrderAttempt` записи с batch correlation/result map; order-level outcome хранится через `gd_orders` и events, а отдельные `DeliveryAttempt` привязаны к legs. Следующий шаг — спроектировать минимальный parent-attempt lifecycle/idempotency, который группирует multi-leg вызов и безопасно восстанавливается после restart, затем добавить rollback/restart/concurrency тесты. До этого C.5 не объявлять принятым.
- Не запускались приложение/GUI, Playerok, ASF или Steam; пользовательская БД не открывалась, никаких внешних действий не было.

## 2026-11-27 — C.4 parent attempts, recovery and acceptance

- Добавлена безопасная отдельная migration `2026-11-delivery-order-attempt-v1` с pre-migration backup suffix `.pre-gamedelivery-delivery-order-attempt-v1.bak`. Новая `gd_delivery_order_attempts` связывает один запуск всего заказа, ожидаемый неизменённый список DeliveryLeg, поколение/idempotency key `order_id:generation`, map результатов legs и общий результат `completed/partial/unknown/failed`.
- Последовательные leg dispatch одного заказа присоединяются к активной parent попытке. Первая сохранённая leg outcome неизменяема; результат заказа закрывается только после отчёта по ожидаемым legs. Повторный вызов не повторяет ASF send, так как per-leg idempotency/lock из прежнего core остаётся последней защитой.
- Startup recovery переводит незавершённые parent attempts в UNKNOWN, сохраняя исходный result и признак `interrupted_after_restart`; UNKNOWN не разблокирует отправку. Read-only reconciliation маршрутизируется через handler contract. Совместимость старого уже отправленного `cs2_item` разрешена только в reconciliation и только при однозначной pinned ConfigVersion Steam Asset, совпадающих game/app/context и bot pool; dispatch для неполного legacy snapshot остаётся заблокирован.
- Общая ручная авторизация остаётся один раз на заказ. После подтверждения ошибки отдельных legs больше не останавливают остальные подготовленные legs; результат фиксируется как UNKNOWN/partial. Ошибка одного заказа не блокирует отдельный следующий заказ.
- C.4 acceptance: **DONE** для зарегистрированного `steam_asset / steam_trade_offer` маршрута и versioned contract foundation. Целевые dispatcher/handler/recovery/chat-command/MainWindow и migration suites — **106 passed** до последнего добавленного изоляционного теста; полный `python -m unittest discover -s tests -q` — **1096 passed**; затем четыре точечных parent-attempt/concurrency/isolation tests — **4 passed**. Compileall и `git diff --check` — PASS.
- Область остаётся намеренно ограниченной: Steam Cards handler и другие executor methods подключаются в C.5 и следующих игровых этапах. Ни selection/reservation logic, ни существующий guarded ASF core не переписывались. Live sends, GUI, ASF/Steam services и пользовательская БД не запускались/не открывались.
- Один полный прогон до reconciliation-совместимости выявил legacy CS2 read-only regression; узкая reconciliation-only проверка pinned selector/config + bot + app/context добавлена. Полный suite после неё — **1096 passed**. После финальной записи результата freshness failure адресные `tests.test_main_delivery_send`, `tests.test_delivery_dispatcher`, `tests.test_delivery_ledger` — **64 passed**, targeted compileall и diff-check — PASS; полный suite после этих последних малых изменений не повторялся.

## 2026-11-27 — C.5 Steam Cards handler connection

- Registered `steam_card_set / steam_trade_offer` in the existing versioned ASF handler registry, which is shared by dispatch and read-only reconciliation. Steam Cards selection, complete-set validation, listing quota and canonical reservation implementation remain in their existing preflight/reservation code.
- The MainWindow now carries each successful ASF preview result through the one whole-order confirmation to dispatch. In the same `BEGIN IMMEDIATE` claim transaction, typed Steam Card dispatch compares the current leg ID, bot, AppID, context, exact ordered asset list, and bound recipient SteamID64 with the preview. Missing or changed preview evidence returns `CONFIRMED_PREVIEW_MISMATCH` before the ASF send call. Existing reservation manifest checks, per-leg idempotency, parent attempt aggregation, UNKNOWN lockout and Playerok freshness gates remain active.
- Regression tests cover exact registered route, dispatch of the unchanged previewed Steam Card leg, blocking after the stored assets change, and blocking changed bot/AppID/context/recipient. MainWindow tests confirm the preview is forwarded for every leg after the existing single whole-order approval.
- Validation: focused Steam Cards/dispatcher/handler/UI/preparation suites — **141 passed**; `python -m compileall` on changed Python files — **PASS**; `git diff --check` — **PASS**; full `python -m unittest discover -s tests -q` — **1101 passed**. The first focused command included an incorrect nonexistent test module name; that command was not used as evidence, and the corrected command passed all 141 tests.
- No live Steam offer, Playerok mutation/message, ASF operation, GUI run or user database access occurred. The plan's separate low-risk live test-account end-to-end acceptance is still blocked; live readiness is not claimed. Continue with C.6 while keeping that explicit release gate.
- Changed implementation/tests: `app/main.py`, `core/delivery/asf_trade_handler.py`, `core/delivery/dispatcher.py`, `tests/test_delivery_dispatcher.py`, `tests/test_delivery_handler_contract.py`, `tests/test_main_delivery_send.py`. Exact-SHA GitHub CI is the next verification after this block is committed/pushed.

## 2026-11-27 � C.6 CS2 fail-closed routing checkpoint

- ��������� ����������� C.6 ���� � `work/local-sync`; ������� ����� � `origin/work/local-sync` ��������� �� ������ ��������� ���������. Existing C.5/C.4/C.3 acceptance results ���������.
- ��� typed `SteamAssetSpec(game_id="cs2")` routing ������ ������������� ��������� ����������� AppID 730, context 2 � ���������� bot pool �� Recipe/ConfigVersion. Normal dispatch ���������������� � ��������� `cs2_item`, ������� �� ��������������� � production registry; ������� CS2 �������� ������� fail-closed �� ���������� handler capability acceptance. Reconciliation-only compatibility ����������� ������ ��� ������ ������������ legacy pinned route.
- Dispatcher ����������� item type �������� � ������� ������������� exact ASF preview ��� CS2 capability path ��� ��, ��� ��� Steam Cards. �������� CS2 dispatch fixtures ���������� ����� fake handler � preview, ����� �� �������� production gate.
- ��������: dispatcher/handler/recovery/chat-command/MainWindow suite � **97 passed**; ������� ����� �������� reconciliation test ��������� � ��������. Compileall � `git diff --check` ��������� ��� ���������� diff.
- Live Playerok/Steam/ASF, GUI � ���������������� �� �� ��������������; ��������� � trade offers �� ������������. C.6 ������� **PARTIAL/BLOCKED**: ����������� ��������� production CS2 handler, verified inventory/tradable/restriction capability proof � ����������� live test-account gate. ��������� ��� � �������������� � ������� ��������� CS2 handler contract/capability, �������� ������� fail-closed �������.

## 2026-11-27 — C.6 routing hardening follow-up

- Усилена fail-closed обработка CS2 route: некорректный AppID в persisted DeliveryLeg теперь преобразуется в контролируемый `DeliveryHandlerRouteError`, без необработанного исключения и без попытки отправки.
- Проверки после изменения: `tests.test_delivery_dispatcher` и `tests.test_delivery_handler_contract` — **48 passed**; targeted compileall — PASS; `git diff --check` — PASS.
- Production CS2 handler по-прежнему не зарегистрирован; live inventory/tradability/restriction proof, ASF/Steam и Playerok не использовались. C.6 остаётся PARTIAL/BLOCKED.

## 2026-11-27 — C.6 dedicated CS2 handler capability gate

- Добавлен отдельный `games/cs2/delivery_handler.py` с `CS2TradeOfferHandler` и immutable `CS2CapabilityAcceptance`. Handler может быть зарегистрирован только при явном подтверждении всех четырёх независимых evidence: свежий inventory, tradable/restriction metadata, no-send preview и отдельная test-account send acceptance.
- Неполное capability не регистрирует маршрут; non-bool evidence отклоняется; accepted handler поддерживает только exact AppID 730/context 2. Production dispatcher намеренно не регистрирует этот handler, поэтому текущая отправка CS2 остаётся fail-closed.
- Проверки: handler contract + dispatcher — **50 passed**; targeted compileall — PASS; `git diff --check` — PASS.
- Live ASF/Steam/Playerok, test-account send, GUI и пользовательская БД не использовались. C.6 остаётся **PARTIAL/BLOCKED** до получения и аудита реальных capability evidence и отдельного разрешённого live gate.

## 2026-11-27 — C.6 CS2 capability wiring checkpoint

- Устранён контрактный разрыв между route loader и отдельным CS2 handler: `load_leg_handler_request()` теперь передаёт exact `app_id` и `context_id` в нормальный, legacy-reconcile и opt-in handler request. Поэтому принятый handler может проверить 730/2 по фактической DeliveryLeg, а не по значениям по умолчанию.
- `DeliveryDispatcher` получил необязательный `cs2_capability` параметр. При отсутствии capability production registry не меняется; при неполном evidence registration возвращает false и CS2 остаётся `HANDLER_UNAVAILABLE`; только полностью принятой capability разрешено зарегистрировать dedicated handler.
- Проверки: handler contract, dispatcher и MainWindow send — **73 passed**; targeted compileall — PASS; `git diff --check` — PASS.
- Live ASF/Steam/Playerok, test-account send, GUI и пользовательская БД не использовались. C.6 остаётся PARTIAL/BLOCKED до реального evidence и отдельного live gate.

## 2026-11-27 — C.6 opt-in dispatcher registration regression

- Добавлен необязательный `cs2_capability` в `DeliveryDispatcher`: неполная capability не меняет registry, а полностью принятая capability регистрирует только exact `cs2_item/steam_trade_offer`. Default construction без capability остаётся прежним и fail-closed.
- Route loader передаёт фактические AppID/context в request, поэтому opt-in handler проверяет persisted 730/2 leg без defaults.
- Regression coverage подтверждает incomplete/full registration boundary. Handler/dispatcher tests — **51 passed**; compileall и diff-check — PASS.
- Capability evidence остаётся неподтверждённым синтетическими тестами: live ASF inventory, tradable/restriction check, preview и отдельный test-account send не выполнялись. C.6 остаётся PARTIAL/BLOCKED.

## 2026-11-27 — C.6 accepted-capability synthetic integration

- Добавлена synthetic integration regression для opt-in пути: typed CS2 Recipe проходит через route loader с фактическими 730/2, dedicated `CS2TradeOfferHandler`, exact preview binding и существующий guarded ASF dispatch core. Fake ASF подтвердил вызов с AppID 730/context 2; внешний ASF/Steam send не выполнялся.
- Одновременно восстановлена контролируемая обработка malformed persisted AppID через `DeliveryHandlerRouteError`; необработанный `ValueError` наружу не выходит.
- Точечная проверка handler/dispatcher: **13 passed**; compileall и diff-check — PASS.
- Это подтверждает только синтетическую wiring-интеграцию. Реальные fresh inventory, tradable/restriction evidence и test-account acceptance отсутствуют; production capability остаётся disabled по умолчанию, C.6 PARTIAL/BLOCKED.

## 2026-11-27 — C.6 reconciliation compatibility correction

- Проверена попытка подключить dedicated CS2 handler к reconciliation. Она ломала существующий legacy reconciliation path: старые `cs2_item` legs должны оставаться `steam_asset` route для принятого read-only reconciler. Optional CS2 handler registration из `DeliveryReconciliation` удалена; dedicated CS2 capability остаётся только normal dispatch opt-in, а reconciliation сохранён через generic route.
- Исправлен оставшийся `NameError` после удаления незавершённого блока. Malformed AppID продолжает преобразовываться в `DeliveryHandlerRouteError`.
- Полный связанный delivery suite (`dispatcher`, `handler_contract`, `recovery`, `chat_commands_telegram`, `main_delivery_send`) — **101 passed**; compileall и diff-check — PASS.
- Live Playerok/Steam/ASF, GUI, test-account send и пользовательская БД не использовались. C.6 остаётся PARTIAL/BLOCKED до реального capability evidence и отдельного live gate.

## 2026-11-27 — C.6 isolated ASF CS2 inventory read-only acceptance

- Пользователь штатно обновил пароль IPC изолированного ASF; значение не выводилось, не записывалось в проект и передавалось только в памяти локального read-only клиента.
- Запущена только изолированная ASF-сборка из `C:\Users\а\Downloads\GameDelivery-ASF-Generic-READY` с профилем `D:\ASF-CS2-TEST\Home` на loopback `127.0.0.1:1243`. Выполнен один GET инвентаря тестового бота `Ewaylariell` для AppID `730`, context `2`.
- Получены 3 физических asset-записи; все имеют подтверждённые `tradable=true` и `marketable=true`; `tradable_after` отсутствует. Asset IDs и полные названия намеренно не записываются в журнал.
- ASF остановлен после запроса; порт `1243` свободен. Preview, Steam Trade Offer, Playerok, пользовательская БД и GameDelivery GUI не запускались.
- Это закрывает только live evidence свежего CS2 inventory/tradability для изолированного тестового бота. C.6 остаётся PARTIAL/BLOCKED до отдельной проверки exact no-send preview, restriction semantics и одной разрешённой test-account send acceptance.

## 2026-11-27 — C.6 no-send preview live attempt

- На изолированном ASF с IPC-паролем выполнен один read-only `gamedelivery-preview` для CS2 730/2 и ранее разрешённого test recipient; токен Trade URL не передавался в ASF command payload и не записывался в журнал.
- Preview завершился fail-closed с безопасной ошибкой ASF о невозможности проверить актуальный inventory выбранного бота. По plugin contract это происходит до формирования preview и до любого создания/отправки offer; `sendEnabled=false` не был получен.
- Steam Trade Offer не создавался, recipient inventory не изменялся, Playerok и пользовательская БД не использовались. ASF остановлен после проверки.
- Свежая read-only inventory-проверка 730/2 остаётся подтверждённой отдельно; exact preview и test-account send acceptance не подтверждены. C.6 остаётся PARTIAL/BLOCKED.

## 2026-11-27 — C.6 inventory-vs-preview discrepancy audit

- Уточнено, что многоботовый ASF-пул является штатным и не ограничивался. В полном профиле загружается множество аккаунтов; `Ewaylariell` присутствует, включён и в журнале имеет успешный Steam login.
- `games/cs2/delivery_handler.py` не вызывает ASF inventory и не участвует в preview preflight; изменения в нём для устранения этого расхождения не подтверждены.
- Успешный Python probe использует HTTP GET `/Api/Bot/Ewaylariell/Inventory/730/2?language=english`. GameDelivery ASF plugin использует тот же выбранный `Bot` по case-insensitive имени и вызывает `targetBot.Actions.GetInventory(request.AppId, request.ContextId, language: "english")`; для CS2 request values остаются 730/2. Поэтому расхождения в имени бота, AppID/context или передаче результата по исходному коду не найдено.
- Plugin contract дополнительно требует `targetBot.IsConnectedAndLoggedOn` и live Actions inventory; прямой IPC GET не доказывает успешность этого второго runtime вызова. Один контролируемый preview завершился fail-closed на `GetInventory`, до создания offer.
- Отдельная read-only команда `gamedelivery-diagnose Ewaylariell` через `/Api/Command` вернула HTTP 400, поэтому runtime method signatures получить нельзя; это отдельный недостаток/несовместимость command invocation, но он не доказывает ошибку выбора CS2-бота.
- Код и многоботовая архитектура не менялись. Preview и Steam send не запускались повторно; C.6 остаётся PARTIAL/BLOCKED до подтверждения live `Actions.GetInventory` и no-send preview.


## 2026-11-27 — C.6 preview readiness race diagnosis and bounded gate

- Изолированный ASF log установил точную причину предыдущего fail-closed preview: `gamedelivery-preview` был принят до завершения `OnLoggedOn/Init` выбранного `Ewaylariell`; plugin проверял `IsConnectedAndLoggedOn` и завершал работу до `Bot.Actions.GetInventory`. Расхождения имени бота и параметров 730/2 не обнаружено; полный многоботовый пул не ограничивался.
- В `integrations/asf_plugin/GameDeliveryPlugin.cs` добавлен bounded readiness gate только для read-only preview: до 15 секунд, polling 250 мс, затем fail-closed с явной причиной. Send path и Steam Cards не изменялись.
- `tests.test_asf_plugin_contract` — **43 passed**; Python compileall и `git diff --check` — PASS.
- Live повтор preview не выполнен: локально отсутствует .NET SDK, поэтому новый C# DLL не собран и старый DLL не подменялся. Steam Trade Offer не создавался и не отправлялся. C.6 остаётся **PARTIAL/BLOCKED** до сборки нового plugin, повторного no-send preview на полном ASF-пуле и отдельной проверки разрешённой test-account доставки.


## 2026-11-27 — C.6 rebuilt plugin and live preview evidence

- Installed .NET SDK 10.0.404 in a separate user-local directory and built `integrations/asf_plugin/GameDelivery.ASFPlugin.csproj` against the assemblies in the ASF Generic 6.3.11.7 test package. Build completed with 0 warnings and 0 errors.
- Replaced only the isolated test ASF plugin DLL at `C:\Users\а\Downloads\GameDelivery-ASF-Generic-READY\plugins\GameDelivery.ASFPlugin\GameDelivery.ASFPlugin.dll`; the rebuilt file was 172544 bytes and its source/destination SHA-256 matched. ASF loaded the plugin successfully. The full configured multi-bot pool remained enabled; `Ewaylariell` was confirmed `IsConnectedAndLoggedOn=true`.
- A new no-send preview completed successfully after the readiness gate: selected bot `Ewaylariell`, AppID `730`, Context `2`, current inventory count `3`, exact asset composition matched, `mode=preview-only`, `sendEnabled=false`. No Trade Offer was created by preview.
- A separate live-send attempt was made only after checking the previously authorized recipient partner `899288977` and exact preview composition. The ASF IPC call timed out after send processing began and returned `resultUnknown=true`; the fail-closed order status query returned `status=unknown` and no confirmed TradeOfferID. A read-only inventory query still returned 3 assets. No retry was attempted. Therefore live delivery acceptance and post-delivery inventory change are **NOT CONFIRMED**; C.6 remains **PARTIAL/BLOCKED**.
- Regression evidence: `tests.test_asf_plugin_contract` — **43 passed**; compileall and `git diff --check` — PASS. No Playerok mutation, user SQLite access, Steam Web API, or Steam Cards changes were made.


## 2026-11-27 — C.6 read-only investigation of unknown live send

- В ASF log найдена точная запись операции `c6-live-test-20261127`: `Ewaylariell`, получатель соответствовал ранее разрешённому partner ID, `assetCount=3`; `inventory_fetch_started` зафиксирован в 19:31:56, `inventory_fetch_completed` в 19:31:59 с 3 активами. Последующей записи `steam_offer_dispatch_completed` нет: IPC timeout произошёл после начала send pipeline.
- Локальный `sent_offers.json` не содержит записи этого order ID. Read-only `gamedelivery-status Ewaylariell order:c6-live-test-20261127` через вновь запущенный ASF вернул `found=false`, `status=unknown` с причиной отсутствия записи в локальном журнале. Это не считается доказательством отсутствия предложения в Steam.
- ASF/Steam read-only история исходящих предложений не дала подтверждённого TradeOfferID для exact recipient и состава. Состав не может считаться сопоставленным с тремя preview assets; текущий inventory count=3 сам по себе доставку не доказывает. Статус принятия/завершения и timestamps предложения не установлены.
- Повторная send-команда, создание/отмена предложения, изменение получателя или состава не выполнялись. Операция оставлена в `UNKNOWN`; C.6 остаётся **PARTIAL/BLOCKED**. Новый диагностический endpoint в код не добавлялся; существующие пользовательские изменения сохранены.


## 2026-11-27 — C.6 Steam outgoing-history availability audit

- Повторно изучен существующий read-only контур проекта. `ASFClient.get_trade_offer_status()` and `get_order_trade_status()` only call plugin `gamedelivery-status`; the exact offer path requires a known `TradeOfferID`, while the order path first requires the order ID to exist in `%LOCALAPPDATA%\GameDelivery\ASFPlugin\sent_offers.json`.
- The plugin contains `ArchiWebHandler.GetTradeOffers(activeOffers:null, receivedOffers:false, sentOffers:true, withDescriptions:true)`, but it is not exposed as a public enumeration/history command. No existing project endpoint can enumerate all outgoing offers for recipient partner `899288977` around 2026-11-27 19:31:56 without a known offer ID or a new IPC command/API, both prohibited by this task.
- The local `sent_offers.json` remains without `c6-live-test-20261127`; only an older, unrelated `Ewaylariell` send record exists for another recipient. ASF logs for the interval contain `ProcessSendCommand`, recipient `76561198859554705`, `inventory_fetch_started/completed` with 3 assets, but no post-dispatch completion record and no TradeOfferID.
- An actual Steam outgoing-trade-offer history was **not** queried through an authorized project mechanism in this pass. Therefore TradeOfferID, live offer state, acceptance/completion and exact Steam asset composition remain unconfirmed. Status remains `UNKNOWN`; C.6 stays **PARTIAL/BLOCKED**. No new offer, resend, accept, cancel, confirmation, or Steam Web API call was performed.

## 2026-11-27 — C.6 ASF read-only outgoing offers enumeration

- На изолированном ASF Generic 6.3.11.7 с полным многоботовым пулом и обновлённой `GameDelivery.ASFPlugin.dll` выполнена только read-only команда `gamedelivery-diagnose Ewaylariell`. Подтверждены ASF `6.3.11.7`, бот `Ewaylariell`, `sendEnabled=false`, `tradeOfferCreated=false`, инвентарь CS2 получен.
- Диагностика вызвала поддерживаемый `ArchiWebHandler.GetTradeOffers(activeOffers:null, receivedOffers:false, sentOffers:true, withDescriptions:true)` и получила 11 исходящих предложений. Для partner ID `899288977` (SteamID64 `76561198859554705`) найдены четыре предложения: `9500675937` (Accepted, 1 CS2 asset), `9500562380` (Accepted, 1 CS2 asset), `9500617575` (Accepted, 2 CS2 assets), `9503716632` (Accepted, 2 CS2 assets). Полный состав сохранён только в диагностическом результате, секреты не выводились.
- ASF `TradeOffer` не содержит времени создания или изменения. Поэтому нельзя доказать, какое из найденных предложений относится к `c6-live-test-20261127` (операция была около 19:31:56), и нельзя подтвердить совпадение с тремя assets исходного preview. TradeOfferID операции неизвестен; результат операции остаётся `UNKNOWN`, C.6 — `PARTIAL/BLOCKED`.
- Не выполнялись send/resend, accept, cancel, confirmation или Steam Web API. Изолированный ASF после диагностики остановлен.


## 2026-11-27 — C.6 send pipeline durability after lost IPC result

- Установлена причина инцидента `c6-live-test-20261127`: клиент `ASFClient.send_trade_offer()` создавал одноразовый secret и после IPC timeout возвращал `resultUnknown=true`, но до этого не имел собственного durable operation claim. Долговечная запись создавалась только внутри ASF plugin перед `SendTradeOffer`; при потере IPC-ответа GameDelivery не имел локальной записи, из которой можно было бы безопасно восстановить операцию.
- В `integrations/asf/client.py` добавлен защищённый от повторной отправки operation journal `GameDelivery/ASFPlugin/send_operations.json`: до вызова ASF фиксируются order/bot/recipient/AppID/context/assets и fingerprint со статусом `dispatching`; успешный проверенный ответ переводит запись в `sent` с offer IDs; потеря ответа/ошибка после dispatch переводит её в `unknown`. Trade URL token в журнал не записывается.
- Повторная отправка по тому же order ID и fingerprint блокируется до нового IPC вызова; другой fingerprint для того же order ID также блокируется. Это не выполняет reconciliation и не утверждает доставку: UNKNOWN требует отдельной read-only проверки Steam/ASF.
- Адресный `python -m unittest tests.test_asf_client -q`: **61 passed**. Добавлены проверки durable UNKNOWN после timeout и запрета повторного IPC dispatch. Реальные Steam/ASF send операции не выполнялись; C.6 остаётся PARTIAL/BLOCKED до отдельного разрешённого успешного live gate.

## 2026-11-27 — C.6 final duplicate-send protection audit

- Проверены все production call sites Steam send: `app/main.py` (две UI точки) и `core/delivery/dispatcher.py`; все они вызывают общий `ASFClient.send_trade_offer()`. Единственный IPC route — `gamedelivery-send`, единственный ASF `SendTradeOffer` — `TradeDelivery.ProcessSendCommand`; preview/status/diagnostic ветки его не вызывают.
- Operation journal расположен в постоянном `%LOCALAPPDATA%/GameDelivery/ASFPlugin/send_operations.json` (fallback — пользовательский `Path.home()/AppData/Local`), не в pending-secret каталоге и не в temp-каталоге. Секреты/trade token в journal не записываются.
- Fingerprint включает order ID, bot, SteamID64 получателя, AppID/context, полный отсортированный asset ID состав и точные amounts. Изменение получателя или состава блокируется как конфликт существующей операции.
- Journal claim выполняется до IPC под процессным lock потока; запись обновляется атомарной заменой temporary-файла (`os.replace`). Одновременные запросы в одном процессе дают только один IPC dispatch. После перезапуска состояния `dispatching`, `unknown`, `sent` и `awaiting_confirmation` остаются блокирующими; отдельной автоматической очистки нет — требуется reconciliation/operator review. Межпроцессный lock не добавлялся: штатный GameDelivery имеет один процесс отправки, а ASF plugin дополнительно имеет собственный semaphore/idempotency journal.
- Добавлена адресная concurrency regression. `python -m unittest tests.test_asf_client -q`: **62 passed**; `compileall`: PASS; `git diff --check`: PASS. Реальные Steam отправки и новые Trade Offers не выполнялись. C.6 остаётся PARTIAL/BLOCKED до отдельного разрешённого live-теста.

## 2026-11-27 — C.6 final live-test preparation (no-send)

- Выполнен только read-only preflight на изолированном ASF Generic 6.3.11.7 с полным пулом. Бот `Ewaylariell` был подключён к Steam; актуальный GET инвентаря AppID 730 / Context 2 успешно вернул 3 assets.
- Для подготовки выбран минимальный состав из одного доступного предмета: `PP-Bizon | Thermal Currents`, AssetID `54691360708`, amount 1. Описание подтверждает `tradable=true`, `marketable=true`, `market_tradable_restriction=7`; другие ограничения не выводились как доказанные правила.
- Получатель проверен по согласованному partner ID `899288977` (SteamID64 `76561198859554705`). Trade URL token не записывался в журнал или отчёт.
- `gamedelivery-preview` завершился успешно: `mode=preview-only`, `sendEnabled=false`, AppID 730, Context 2, assetCount 1; Trade Offer не создавался. Подготовленный fingerprint: `f29c57524819686f56898c07c50ed4a8fe6fcebf335cd03d6d78d0cf8fd4a9c3`.
- Для order ID `c6-live-preflight-20261127` durable journal записи нет, что ожидаемо: preflight не вызывает `send_trade_offer`; journal claim создастся только после отдельного разрешения на send. Предыдущая UNKNOWN-операция `c6-live-test-20261127` не повторялась.
- Изолированный ASF после preflight остановлен. Реальная отправка, `gamedelivery-send` и `SendTradeOffer` не выполнялись. C.6 остаётся PARTIAL/BLOCKED до отдельного разрешения и подтверждения live-результата.

## 2026-11-27 — C.6 authorized live test send completed

- Перед отправкой повторно подтверждены: `Ewaylariell` подключён; AssetID `54691360708` присутствует в актуальном CS2 730/2 inventory с amount 1; описание `PP-Bizon | Thermal Currents`; `tradable=true`, `marketable=true`, `market_tradable_restriction=7`. Plugin перед dispatch также выполнил собственную live inventory/tradable проверку.
- Получатель сверён с разрешённым partner ID `899288977` / SteamID64 `76561198859554705`; fingerprint совпал с согласованным `f29c57524819686f56898c07c50ed4a8fe6fcebf335cd03d6d78d0cf8fd4a9c3`; конфликтующей записи durable journal не было.
- Выполнен ровно один вызов общего `ASFClient.send_trade_offer()` для order `c6-live-preflight-20261127`. ASF вернул `success=true`, `mode=sent`, `sendEnabled=true`, `assetCount=1`, AppID/context `730/2`, `confirmationStatus=handled`, `tradeAccepted=false`, TradeOfferID `9512102084`. Повторный вызов не выполнялся.
- Результат доказывает создание исходящего Trade Offer и обработку ASF/mobile confirmation; принятие предложения получателем и изменение inventory отправителя ещё не подтверждены. Изолированный ASF остановлен после операции. Ожидается подтверждение пользователя о получении; до него C.6 остаётся PARTIAL/BLOCKED.

## 2026-11-27 — C.6 live test acceptance confirmed read-only

- После подтверждения пользователя выполнена read-only проверка через ASF Generic 6.3.11.7 для TradeOfferID `9512102084` у бота `Ewaylariell`.
- ASF вернул `found=true`, recipient SteamID64 `76561198859554705`, status `Accepted`; состав предложения содержит ровно AssetID `54691360708`, AppID/context `730/2`, amount 1.
- Повторный read-only inventory 730/2 вернул 2 assets; отправленный AssetID `54691360708` отсутствует в inventory отправителя. Это подтверждает принятие предложения и уменьшение inventory отправителя на один asset. ASF остановлен после проверки.
- Новых send-команд, offers, accept/cancel действий или изменений Playerok не выполнялось. Разрешённый C.6 live test теперь имеет подтверждённый end-to-end результат; C.7 не начинался.

## 2026-11-27 — C.6 resumed verification checkpoint

- После возобновления проверены текущая ветка `work/local-sync`, сохранённые незакоммиченные изменения и отсутствие активных ASF/delivery процессов.
- Повторно выполнен только связанный локальный regression suite: `python -m unittest tests.test_asf_client tests.test_asf_plugin_contract tests.test_delivery_dispatcher tests.test_delivery_handler_contract tests.test_delivery_chat_commands_telegram -q` — **167 passed**. `compileall` для затронутых Python-модулей ранее прошёл; дополнительная live-операция не запускалась.
- Предыдущая read-only проверка TradeOfferID `9512102084` со статусом `Accepted` и отсутствием AssetID `54691360708` у отправителя остаётся последним подтверждённым live evidence. C.6 не переоткрывается; C.7 не начинался.


## 2026-11-27 — C.7 protected digital code stock foundation

- Добавлен `core/delivery/digital_codes.py`: preview импорта проверяет пустые, дублирующиеся и некорректные строки до записи; SQLite хранит только `code_id`, product/batch, SHA-256 и lifecycle, plaintext кода сохраняется только в OS keyring `GameDelivery.DigitalCodes`.
- Импорт атомарен и идемпотентен по hash; конкретный код резервируется под один order через `BEGIN IMMEDIATE`. Состояния `reserved`, `revealed`, `unknown`, `released`, `consumed` и append-only audit покрывают повторное раскрытие, неизвестный исход и явный operator reconcile. UNKNOWN не выдаёт следующий код автоматически.
- Добавлена аддитивная migration `2026-11-digital-code-stock-v1` с backup suffix; цифровой маршрут `digital_text_unit/text_delivery` подключён к общему handler registry только через явный `digital_code_enabled=True` opt-in и возвращает `ACTION_REQUIRED`, не выполняя внешнюю отправку и не раскрывая код в логах.
- Тесты `tests.test_digital_codes`, `tests.test_delivery_handler_contract`, `tests.test_delivery_dispatcher`: **56 passed**. Реальный импорт, пользовательская БД, Playerok, Steam и Telegram не использовались; C.7 остаётся **PARTIAL** до подключения операторского UI/конкретного delivery adapter и проверки полного lifecycle через production order flow. Следующий шаг — добавить безопасный UI для preview/import и явного reveal/reconcile без отображения plaintext в диагностике.


## 2026-11-27 — C.8/C.9 safe delivery foundations

- C.8: добавлен `core/delivery/file_catalog.py`. Управляемый каталог нормализует и проверяет containment, сохраняет стабильный file ID, размер и SHA-256, повторно проверяет целостность перед резервированием и переводит выдачу в `MANUAL_REQUIRED`, пока не подтверждён реальный adapter конкретной площадки. Произвольные пути и фиктивный delivered статус блокируются.
- C.9: добавлен `core/delivery/service_tasks.py`. Единицы услуги резервируются отдельно, поддерживаются `in_progress`, частичное выполнение, `awaiting_confirmation`, `completed`, `cancelled`, `disputed`; завершение требует полного количества и evidence, переходы аудируются.
- Адресные тесты `tests.test_file_catalog_service_tasks` и `tests.test_digital_codes`: **6 passed**; compileall и `git diff --check` прошли. Внешние площадки, Playerok, Steam, Telegram и пользовательская БД не использовались. C.8/C.9 остаются **PARTIAL** до подключения операторского UI и подтверждённого способа фактической передачи; C.7 остаётся **PARTIAL** до UI import/reveal/reconcile. Следующий блок — C.10, общий операторский интерфейс и Telegram lifecycle.


## 2026-11-27 — C.10 Telegram channel hardening

- Расширен существующий `integrations/telegram/outbox.py`: добавлены общий безопасный Bot API request helper, раздельная read-only `telegram_test_connection(getMe)` и идемпотентный `TelegramUpdateProcessor` для повторных update без повторной команды. Existing OS keyring credential store, durable outbox/retry и reply-to deal/chat/order binding переиспользованы.
- Ошибки API не раскрывают token/proxy credentials; Telegram остаётся вспомогательным каналом и не блокирует delivery queue. Реальный Telegram API и пользовательские credentials не использовались.
- `tests.test_delivery_chat_commands_telegram`: **9 passed**, compileall прошёл. C.10 остаётся **PARTIAL**: настройки UI, polling lifecycle и тестовое сообщение должны быть подключены к существующему Settings tab после отдельной UI integration работы.


## 2026-11-27 — C.10 operator settings and C.11 readiness hardening

- В существующий Settings tab подключён `app/telegram_settings.py`: token/proxy credentials идут только в OS keyring, operator chat и allowlist IDs — в QSettings; `Проверить подключение` (getMe) и `Отправить тестовое сообщение` разделены. Секреты не помещаются в UI status/error text.
- Telegram update processor дедуплицирует update_id и сохраняет точную reply-to привязку через существующий order/deal/chat mapping.
- Усилены C.8/C.9 инварианты: file reservation повторяет containment/hash проверку внутри `BEGIN IMMEDIATE`; service lifecycle отклоняет недопустимые state transitions.
- Проверки перед C.11: целевые Telegram/digital/file/service/dispatcher tests — **67 passed**, compileall — PASS, `git diff --check` — PASS. Внешний Telegram API не вызывался.


## 2026-11-27 — Autonomous Stage C continuation checkpoint

- После подтверждённого C.6 реализованы безопасные foundations C.7–C.10: protected digital-code stock, managed file catalog, manual service units, Telegram connection/update/outbox hardening и Settings UI. Все новые внешние действия остаются opt-in/manual; реальные Steam/Playerok/Telegram/file sends не выполнялись.
- C.7: import preview/atomic keyring stock/reserve/reveal/UNKNOWN reconcile; C.8: stable file ID/hash/containment и `MANUAL_REQUIRED`; C.9: unit lifecycle/evidence/audit; C.10: allowlist/reply binding/dedup update и раздельные connection/test-message controls.
- Проверено: `python -m unittest discover -s tests -q` — **1117 passed**; targeted C.7–C.10 suite — **68 passed** после edge-case regression; compileall и `git diff --check` — PASS.
- Осталось до полного C.11 acceptance: подключить production order/UI actions для code reveal/import и service/file operator workflows, подтвердить площадочные adapters/live gates по отдельному разрешению и затем выполнить полный acceptance audit. C.7–C.10 не объявляются полностью завершёнными по одному unit suite.


## 2026-11-27 — C.7 digital stock wired into unified order reservation

- `DeliveryOrderPipeline.prepare_recipe_order` теперь проверяет доступный digital stock по `RecipeItem.stock_source_id`; обязательная нехватка блокирует preflight, optional item безопасно пропускается.
- `DeliveryReservationService.reserve_recipe_plan` резервирует точные `code_id` через `DigitalCodeStore.reserve_for_order_line` внутри общей `BEGIN IMMEDIATE` транзакции, создаёт operator-assisted DeliveryLeg и связывает ресурс с `OrderLine`; физические Steam asset/card rules не менялись.
- Добавлен список stock для operator panel; plaintext code по-прежнему отсутствует в SQLite, логах и UI status. UNKNOWN/reveal/reconcile остаются отдельным явным lifecycle.
- Regression `tests.test_digital_codes`, `tests.test_recipe_unified_reservations`, `tests.test_delivery_handler_contract`: **47 passed**; compileall и diff-check проходят. C.7 остаётся **PARTIAL** до production reveal/reconcile action binding и end-to-end operator UI verification.

## 2026-11-27 — C.10 buyer message unknown-result safety

- Исправлен bridge `BuyerPlatformMessageDispatcher`: callback `unknown_result` переводит запись в terminal `unknown` с причиной и исключает её из автоматического retry, поскольку browser adapter допускает, что мутация уже достигла Playerok.
- Добавлена совместимость с ранее созданной таблицей outbox без `created_at`/`last_error`; plaintext и секреты не затрагиваются.
- `python -m unittest tests.test_delivery_chat_commands_telegram -q`: **10 passed**. Внешние Playerok/Telegram операции не выполнялись.

## 2026-11-27 — C.10 Telegram bridge integrity checkpoint

- Проверен текущий production bridge: `TelegramOperatorReplyRouter` сохраняет allowlist и строгую связку operator reply → order → deal → Playerok chat; `TelegramUpdateProcessor` дедуплицирует `update_id` и повторно не вызывает маршрутизацию.
- `BuyerPlatformMessageDispatcher` использует существующий browser adapter callback; успешный callback переводит запись в `sent`, обычная ошибка остаётся `retry`, а `unknown_result` переводит запись в блокирующий `unknown` без автоматической повторной мутации.
- Добавлена совместимость outbox с ранее созданными схемами без `last_error`; секреты и текстовые credentials не выводятся в diagnostics.
- Проверка: `python -m unittest tests.test_delivery_chat_commands_telegram -q` — **10 passed**. Внешние Telegram/Playerok действия не выполнялись. C.10/C.11 остаются незавершёнными до полного acceptance audit.

## 2026-11-27 — C.8/C.9 operator lifecycle integration

- Операторская вкладка расширена действиями для существующих stores: резерв файла по `file_id/order_id` с явным подтверждением переводит ресурс в `MANUAL_REQUIRED`; переход состояния услуги принимает `unit_id/state/completed_units`, требует подтверждения и записывает evidence через существующий audit.
- Автоматическая передача файлов не добавлялась: неподтверждённый adapter по-прежнему fail-closed. Завершение услуги возможно только по правилам `ServiceTaskStore` и при полном количестве.
- Проверка: `python -m unittest tests.test_file_catalog_service_tasks tests.test_delivery_chat_commands_telegram tests.test_digital_codes tests.test_recipe_unified_reservations -q` — **48 passed**; `py_compile app/operator_fulfillment_panel.py` — PASS. Внешние операции не выполнялись.

## 2026-11-27 — full regression checkpoint after operator integration

- Полный локальный suite `python -m unittest discover -s tests -q` достиг **1120 tests**, единственный сбой был в устаревшем ожидании списка вкладок после добавления штатной вкладки `Оператор`.
- Обновлён только UI smoke expectation в `tests/test_app_startup.py`; повторный `tests.test_app_startup -q` — **14 passed**.
- `compileall -q app core integrations` — PASS. Внешние Steam/Playerok/Telegram операции не выполнялись.
- Следующий финальный шаг: повторить полный suite после исправления тестового ожидания, затем зафиксировать C.11 acceptance и отдельные реальные ограничения внешних adapters.

## 2026-11-27 — C.11 full regression checkpoint

- Исправлено устаревшее UI smoke expectation: штатная вкладка `Оператор` теперь учитывается в списке вкладок `MainWindow`.
- Полная регрессия после исправления: `python -m unittest discover -s tests -q` — **1120 tests, OK** за 187.660 s.
- `tests.test_app_startup -q` — **14 passed**; `compileall -q app core integrations` — PASS.
- Во время suite выводились ожидаемые mock/error-path diagnostics (ASF 401/URLError, storage failure scenarios, UNKNOWN send paths); необратимых внешних операций не выполнялось.
- Stage C.11 локальная regression часть подтверждена. Открытые ограничения остаются внешними: live Playerok/Telegram/file adapters и Windows packaging/release smoke требуют отдельной среды/разрешения; они не маскируются под выполненные live проверки.

## 2026-11-27 — Windows packaged smoke hardening

- Обнаружен и исправлен реальный дефект CI/package smoke: `python -m app --self-test` ранее создавал полноценный `MainWindow` и требовал локальный ASF `IPC.config`, из-за чего чистая Windows-сборка без developer ASF не проходила smoke.
- `app/__main__.py` теперь в режиме `--self-test` выставляет существующий `GAMEDELIVERY_TEST_ASF_IPC_URL` marker до создания `MainWindow`; production запуск без `--self-test` не изменён.
- Локальная проверка `QT_QPA_PLATFORM=offscreen python -m app --self-test` — PASS без ASF-конфигурации.
- `python -m unittest tests.test_windows_harness tests.test_app_startup -q` — **24 passed**; `compileall` — PASS; `git diff --check` — PASS.
- Следующий шаг: при наличии Windows runner выполнить workflow `.github/workflows/package-windows.yml`; live Steam/Playerok/Telegram операции остаются вне этой проверки.

## 2026-11-27 — local Windows packaging smoke

- Проверен существующий workflow `.github/workflows/package-windows.yml` и выполнена эквивалентная локальная PyInstaller onedir-сборка: PyInstaller 6.22.3 на Windows 11 / Python 3.14.6, с теми же collect-all/collect-submodules параметрами.
- Сборка завершилась успешно; создан `dist/GameDelivery/GameDelivery.exe` (6,782,999 bytes), размер onedir-пакета около 610 MB.
- Запущен собранный `GameDelivery.exe --self-test` с `QT_QPA_PLATFORM=offscreen`; процесс завершился без ошибки. Smoke не требовал локального ASF благодаря исправленному тестовому marker.
- В build output есть предупреждения PyInstaller о отсутствующем optional QML plugin `Qt/labs/assetdownloader`; основной executable собран и smoke прошёл. CI остаётся авторитетной проверкой на закреплённом Python 3.12 runner.
- Реальные Steam/Playerok/Telegram операции не выполнялись. Следующий шаг — зафиксировать текущие изменения отдельным разрешённым commit/push или выполнить доступный CI workflow; без разрешения commit/push не выполняются.

## 2026-11-27 — ASF plugin release-toolchain checkpoint

- Проверен `integrations/asf_plugin/GameDelivery.ASFPlugin.csproj`: целевая версия `net10.0`, зависимости ASF подключаются через `ASF_BIN_DIR`, System.Text.Json остаётся framework implementation; проект не менялся.
- Локальная Windows-среда не содержит .NET SDK: `dotnet --list-sdks` не вернул установленного SDK. Поэтому локальная сборка plugin против ASF Generic 6.3.11.7 недоступна без установки .NET 10 SDK или Windows CI runner.
- Существующий workflow `.github/workflows/asf-plugin.yml` остаётся авторитетным способом сборки с официальными ASF 6.3.11.7 assemblies; запуск внешнего CI из текущей сессии не выполнялся.
- Python/Qt Windows onedir packaging и packaged self-test уже прошли; этот блокер относится только к локальной пересборке C# plugin. Реальные Steam/Playerok/Telegram операции не выполнялись.

## 2026-11-27 — ASF/plugin contract verification

- Без запуска ASF и внешних Steam операций выполнены `python -m unittest tests.test_asf_plugin_contract tests.test_asf_client -q`: **106 passed**.
- Контрактные проверки подтверждают guarded diagnostic/send branching, UNKNOWN/idempotency safety, System.Text.Json expectations и ASF IPC client behavior.
- Локальная C# compilation по-прежнему не выполняется: в среде отсутствует .NET SDK; это ограничение toolchain, не результат ошибки plugin source. Для окончательной DLL-проверки нужен существующий GitHub Windows runner workflow `asf-plugin.yml` с .NET 10 и ASF Generic 6.3.11.7 assemblies.

## 2026-11-27 — release readiness verification checkpoint

- Повторно проверены `tests.test_asf_plugin_contract`, `tests.test_windows_harness`, `tests.test_app_startup`: **68 passed**.
- `git diff --check` не обнаружил содержательных whitespace-ошибок; остаются только стандартные предупреждения Git о преобразовании LF/CRLF.
- Текущая машина имеет .NET runtime 10.0.15, но `dotnet --list-sdks` сообщает `No SDKs were found`; ASF assemblies также отсутствуют в локальном `ASF` каталоге. Локальный C# build поэтому объективно невозможен без отдельного runner/toolchain.
- Python Windows onedir build и packaged self-test подтверждены ранее. ASF plugin build/runtime smoke, Playerok live sync, Telegram live actions и file adapter live delivery требуют соответствующих внешних сред и не выдаются за локально подтверждённые.

## 2026-11-27 — local ASF plugin build and smoke

- Временный .NET 10 SDK `10.0.404` установлен в `%TEMP%`; системная установка и проектные файлы не изменялись.
- Официальный ASF Generic `6.3.11.7` архив скачан и проверен SHA-256 `cb3ddb9bd10560458a460664c9af6186e124e89801d118197a82eef5a9315ceb`; ASF assemblies использованы из временного каталога.
- `dotnet build integrations\asf_plugin\GameDelivery.ASFPlugin.csproj --configuration Release -p:ASF_BIN_DIR=...` — успешно, 0 warnings, 0 errors; DLL собрана в `integrations/asf_plugin/bin/Release/net10.0/GameDelivery.ASFPlugin.dll`.
- `dotnet run --project tests/asf_plugin_smoke/ASFPlugin.Smoke.csproj ...` — `ASF plugin command dispatch, MEF registration, and runtime compatibility smoke tests passed.`
- Output не содержит `ArchiSteamFarm.dll`, `SteamKit2.dll` или `System.Composition.AttributedModel.dll`; plugin использует host-shared assemblies. Steam/ASF account, IPC и trade offers не запускались.

## 2026-11-27 — final local acceptance checkpoint after plugin build

- Полная регрессия после локальной сборки ASF plugin и packaging hardening: `python -m unittest discover -s tests -q` — **1120 tests, OK** за 186.474 s.
- `python -m compileall -q app core games integrations notifications tests` — PASS; `git diff --check` — PASS по содержательным изменениям.
- Рабочее дерево содержит только ожидаемые изменения `PROJECT_MASTER_LOG.md`, `app/__main__.py`, `tests/test_app_startup.py`; build outputs (`dist`, `build`, plugin `bin/obj`) не отслеживаются Git.
- Local Windows GUI package smoke, official ASF 6.3.11.7 plugin build and plugin smoke all have positive local evidence. Live Playerok/Telegram/file adapters and external CI runner remain environmental gates; no irreversible external operation was performed.

## 2026-11-27 — ASF Generic runtime plugin smoke

- Собранный `GameDelivery.ASFPlugin.dll` установлен во временную копию официального ASF Generic 6.3.11.7; ASF запущен без ботов и с `IPC=false`.
- `GAMEDELIVERY_VALIDATE_SEND_JIT=1` подтвердил `GameDeliveryPlugin send state-machine JIT probe: "status":"success"`; plugin runtime load smoke завершён успешно.
- Процесс ASF остановлен после проверки. Не выполнялись логин, IPC-команды, Steam inventory запросы или Trade Offers.

## 2026-11-27 — release hygiene audit

- Проверены текущие изменения и Git index: секретные значения, cookies, trade tokens, DLL/PDB/SQLite и build output в отслеживаемые изменения не попали.
- `git status` содержит только `PROJECT_MASTER_LOG.md`, `app/__main__.py` и `tests/test_app_startup.py`; `git diff --check` не выявил содержательных ошибок.
- Подтверждённые локальные gates: 1120 Python tests, compileall, Windows PyInstaller package/self-test, ASF plugin build против проверенного ASF 6.3.11.7 и ASF Generic runtime smoke.
- Не подтверждены внешними live действиями: Playerok session/sales в текущем запуске, Telegram API, автоматическая файловая передача и пользовательская Steam delivery поверх уже принятого C.6 evidence. Эти ограничения остаются явно отделены от локального acceptance.

## 2026-11-27 — CI full-suite timeout alignment

- Read-only GitHub Actions API показал для последнего опубликованного SHA `e8493cb` успешные Windows package и ASF plugin workflows, но общий `Tests` workflow завершился failure.
- Локальная полная регрессия на той же ветке проходит: **1120 tests, OK** примерно за 186 секунд. Workflow имел full-suite timeout 180 секунд и job timeout 8 минут, что меньше подтверждённого локального runtime с запасом на Ubuntu/Qt.
- `.github/workflows/tests.yml` скорректирован минимально: job timeout увеличен до 12 минут, full-suite timeout до 300 секунд; test commands и критерии результатов не менялись.
- Повторный GitHub run требует commit/push и не запускается в этой сессии без отдельного разрешения. Реальные внешние операции не выполнялись.

## 2026-11-27 — GitHub Tests failure root cause clarified

- Read-only Actions job/annotation API для run `46055843777` (SHA `e8493cb`) показал: предварительные compile/trade/reconciliation/UI steps прошли; full suite затем инициировал isolation, где failure был конкретно `test_app_startup.py (exit 1)`, а отдельный `test_cs2_inventory_panel.py` имел только Qt shutdown segfault после успешных assertions.
- Причина удалённого startup failure — workflow запускался на SHA до локального исправления ожидаемого списка вкладок `MainWindow` (в текущем рабочем дереве `Оператор` уже добавлен и `tests.test_app_startup` проходит). Это не было доказано как timeout; локальное увеличение timeout оставлено с запасом для Ubuntu/Qt, но повторный workflow ещё не запускался.
- Следующая объективная проверка — опубликовать текущий исправленный тестовый контракт и убедиться, что новый GitHub run проходит; без разрешения commit/push не выполняется.

## 2026-11-27 — CI workflow local verification

- После анализа GitHub annotations локально проверены связанные gates: `tests.test_windows_harness`, `tests.test_app_startup`, `tests.test_asf_plugin_contract` — **68 passed**.
- `.github/workflows/tests.yml` содержит только минимальное увеличение timeout для подтверждённого full suite runtime; лишняя пустая строка EOF удалена, `git diff --check` проходит по содержательным изменениям.
- Повторный удалённый run невозможен без публикации коммита; commit/push не выполнялись по ограничениям долгосрочной цели. Рабочее дерево сохранено.

## 2026-11-27 — Telegram proxy routing fix

- Обнаружен production gap: `telegram_api_request` принимал `proxy`, но игнорировал его, поэтому Settings `getMe`/test-message могли обходить сохранённый proxy.
- Исправлен общий helper: валидирует HTTP(S)/SOCKS5 proxy, формирует authenticated proxy URL и передаёт его в `requests.post`; token и proxy credentials не выводятся в ошибках.
- Добавлен mock regression на proxy routing и отсутствие token в proxy diagnostics.
- `python -m unittest tests.test_delivery_chat_commands_telegram -q` — **11 passed**; `py_compile integrations/telegram/outbox.py` — PASS. Реальный Telegram API не вызывался.

## 2026-11-27 — Telegram runtime coordinator foundation

- Проверено, что Telegram outbox/update processor ранее были только библиотечными компонентами и не имели runtime tick. Добавлен `TelegramRuntimeCoordinator`: durable notification enqueue/drain, persistent `getUpdates` offset, idempotent `TelegramUpdateProcessor` и безопасный no-op при отсутствии token.
- Coordinator использует существующий `TelegramBotOutbox`, `telegram_api_request` и `BuyerPlatformMessageDispatcher`; неизвестный Playerok mutation result остаётся terminal `unknown`.
- Добавлен regression на disabled/no-token lifecycle; `python -m unittest tests.test_delivery_chat_commands_telegram -q` — **12 passed**.
- Полное подключение buyer reply tick к `MainWindow` отложено до доступного штатного `PlayerokBrowserChatAPI.send_text_message` callback в production lifecycle; фиктивный adapter не создавался. Telegram notifications могут работать независимо и не блокируют delivery.

## 2026-11-27 — Telegram runtime lifecycle wiring

- `MainWindow` теперь создаёт `TelegramRuntimeCoordinator` через существующий OS keyring и database, запускает безопасный 60-секундный tick и обновляет allowlist/operator chat из QSettings.
- Runtime tick переносит pending delivery events в durable Telegram outbox, выполняет Telegram notification delivery/update polling и не блокирует delivery pipeline при отсутствии credentials или ошибке API.
- Incoming allowlisted replies проходят существующий `TelegramOperatorReplyRouter` и создают buyer-message outbox запись с точной deal/chat binding. Автоматический Playerok send из фонового tick намеренно не включён: `WebEngine` mutation должна выполняться через существующий UI-thread `MarketplacePanel.send_playerok_chat_text` callback, чтобы не создавать второй browser mechanism.
- Проверки: `tests.test_app_startup tests.test_delivery_chat_commands_telegram` — **26 passed**; `py_compile app/main.py integrations/telegram/outbox.py` — PASS. Реальный Telegram/Playerok API не вызывался.

## 2026-11-27 — Telegram runtime polling regression

- Добавлен mock regression для настроенного `TelegramRuntimeCoordinator`: `getUpdates` возвращает update `7`, offset durable state сохраняется как `8`, следующий tick запрашивает offset `8` и не создаёт повторную обработку старого update.
- `python -m unittest tests.test_delivery_chat_commands_telegram -q` — **13 passed**; `python -m compileall -q app integrations/telegram tests/test_delivery_chat_commands_telegram.py` — PASS.
- Реальный Telegram API, Playerok сообщения и другие внешние mutations не выполнялись.

## 2026-11-27 — C.10 Playerok buyer reply UI-thread bridge

- `TelegramRuntimeCoordinator.dispatch_buyer_messages()` подключён к штатному `MarketplacePanel.send_playerok_chat_text()` после завершения фонового Telegram tick. Вызов выполняется в GUI-потоке через основной callback `_run_asf_call_async`; второй WebEngine transport или отдельная авторизация не создавались.
- `BuyerPlatformMessageDispatcher` теперь атомарно переводит `pending/retry` в durable `dispatching` до browser mutation. Успешный callback даёт `sent`, явный `unknown_result` — terminal `unknown`, обычная доставленная ошибка — `retry`; потеря callback/аварийное завершение оставляет `dispatching`, который не попадает в автоматический retry.
- Адресные тесты покрывают успешный GUI adapter callback, terminal `unknown_result` и повторный запуск после потерянного callback без второй отправки. `python -m unittest tests.test_delivery_chat_commands_telegram -q` — **16 passed**. `tests.test_app_startup tests.test_windows_harness tests.test_delivery_chat_commands_telegram -q` — **40 passed**; `py_compile` затронутых модулей — PASS.
- Реальные Telegram/Playerok сообщения не отправлялись. C.10 остаётся незавершённым до live acceptance на отдельно разрешённой тестовой сессии; C.11 требует финального аудита остальных внешних gates и нового CI run после разрешённой публикации.

## 2026-11-27 — Stage C continuation verification checkpoint

- Проверен фактический production wiring C.7–C.10: digital code import/preview/reserve/reveal/reconcile доступен в `OperatorFulfillmentPanel`; unified order reservation вызывает `DigitalCodeStore.reserve_for_order_line`; file/service workflows подключены к той же вкладке и остаются manual/explicit-confirmation; Telegram replies проходят allowlist + reply-to deal/chat binding и штатный Playerok WebEngine adapter.
- После UI-thread bridge и durable buyer-message claim выполнена адресная интеграция: `python -m unittest tests.test_digital_codes tests.test_recipe_unified_reservations tests.test_file_catalog_service_tasks tests.test_delivery_handler_contract tests.test_delivery_dispatcher tests.test_delivery_chat_commands_telegram tests.test_app_startup -q` — **121 passed** за 48.617 s. `py_compile` затронутых модулей — PASS; `git diff --check` — PASS по содержательным изменениям.
- Новые внешние Telegram/Playerok действия не выполнялись; секреты и пользовательская SQLite БД вне тестовых временных баз не использовались. Live acceptance Telegram reply, автоматическая file delivery и повторный GitHub CI run остаются внешними gates. Commit/push не выполнялись.
- Следующий конкретный шаг: после отдельного разрешения на публикацию подготовить commit из текущих относящихся к проекту изменений, исключив `PLAYEROK_LOCAL_REVIEW.patch`, затем проверить новый CI run; до этого цель остаётся активной и не объявляется полностью завершённой.

## 2026-11-27 — final local regression evidence after Telegram bridge

- Полный локальный suite после последнего изменения: `python -m unittest discover -s tests -q` — **1126 tests, OK** за 188.891 s. Предупреждения в выводе относятся к предусмотренным mock error paths (ASF 401/URLError, UNKNOWN send, storage failures); реальные внешние операции не выполнялись.
- Дополнительная адресная проверка `python -m unittest tests.test_delivery_chat_commands_telegram -q` — **16 passed**, exit code 0; `python -m compileall -q app core games integrations notifications tests` — exit code 0; `git diff --check` — exit code 0.
- Ранее собранный Windows onedir self-test остаётся подтверждённым; новый Steam/Playerok/Telegram live gate не запускался. Рабочая ветка `work/local-sync`; commit/push не выполнялись.

## 2026-11-27 — Telegram auxiliary failure does not block Playerok reply

- Найден и исправлен lifecycle gap: `_on_telegram_runtime_complete()` прекращал обработку при ошибке Telegram API до вызова buyer-message dispatcher. Теперь ошибка только логируется, а queued Playerok reply всё равно передаётся штатному `MarketplacePanel.send_playerok_chat_text()`.
- Добавлен regression на сохранение buyer dispatch при ошибке Telegram tick; `python -m unittest tests.test_delivery_chat_commands_telegram -q` — **17 passed**; `py_compile app/main.py` — PASS.
- Реальные Telegram/Playerok сообщения не отправлялись.

## 2026-11-27 — C.3 typed Playerok path revalidated

- Повторно проверен ранее отмеченный C.3 application integration gap. Текущий `_prepare_ledger_order()` направляет ConfigVersion с `recipe_schema_version` в `_prepare_typed_cs2_recipe_order()`, который вызывает `DeliveryOrderPipeline.prepare_recipe_order()`; Playerok ConfigVersion save path вызывает `PlayerokListingConfigService.allocate_listing_stock()` для однозначного typed CS2 selector и блокирует заказ без listing batch.
- Адресная проверка `python -m unittest tests.test_trade_delivery_ui tests.test_playerok_browser_and_listing_config tests.test_playerok_listing_stock_handoff -q` — **77 passed** за 34.596 s. Старый checkpoint C.3 обновлён фактическим доказательством текущего wiring; legacy Steam Cards и legacy CS2 paths не изменялись.
- Реальные Playerok/Steam операции не выполнялись. Остаточные внешние gates: live Playerok session/sales, Telegram API/real buyer reply, автоматическая file delivery и новый GitHub CI run после публикации.

## 2026-11-27 — C.9 operator evidence field

- В `OperatorFulfillmentPanel` добавлено поле «Доказательство / заметка» для service lifecycle. Переходы `awaiting_confirmation` и `completed` теперь требуют непустого evidence; значение передаётся в существующий `ServiceTaskStore.transition()` и сохраняется в append-only audit.
- Промежуточные состояния продолжают использовать штатную валидацию transitions; автоматическое завершение и внешняя передача не добавлялись.
- `python -m unittest tests.test_file_catalog_service_tasks -q` — **3 passed**; `py_compile app/operator_fulfillment_panel.py` — PASS. Реальные услуги и пользовательская БД не использовались.

## 2026-11-27 — C.9 evidence test correction

- Первый запуск нового теста использовал недопустимый переход `reserved → awaiting_confirmation`; это выявило штатный guard state machine. Тест исправлен на допустимую последовательность `reserved → in_progress → awaiting_confirmation`.
- Повторная проверка: `python -m unittest tests.test_file_catalog_service_tasks -q` — **3 passed**; `py_compile` панели и теста — PASS; `git diff --check` — PASS.

## 2026-11-27 — C.7 operator reservation order binding

- Усилен production boundary `OperatorFulfillmentPanel.reserve_code()`: ручная резервация цифрового кода теперь требует существующий `gd_orders.order_id`; неизвестный или недоступный canonical order store блокируется до вызова `DigitalCodeStore.reserve()`.
- Low-level `DigitalCodeStore` не изменён и остаётся совместимым reusable store для unified reservation tests; plaintext кодов по-прежнему нет в SQLite/UI stock table/logs.
- Адресный UI regression `tests.test_file_catalog_service_tasks` — **4 passed**: orphan reservation не создаётся, существующий canonical order резервирует код; `py_compile` и `git diff --check` — PASS.

## 2026-11-27 — C.10 Telegram event filters wired to Settings

- В `TelegramSettingsWidget` добавлены независимые переключатели категорий уведомлений (`new_order`, `insufficient_stock`, `privacy_wait`, `trade_restricted`, `declined`, `unknown`, `support_request`) с сохранением в QSettings.
- `MainWindow` применяет сохранённые фильтры к существующему `TelegramBotOutbox` перед каждым runtime tick; новый канал или параллельная очередь не создавались.
- Проверки: `python -m unittest tests.test_app_startup tests.test_delivery_chat_commands_telegram -q` — **31 passed**; `py_compile` `app/telegram_settings.py` и `app/main.py` — PASS; `git diff --check` — PASS.
- Реальный Telegram API не вызывался; тестовое сообщение не отправлялось.

## 2026-11-27 — C.10 Telegram event filter persistence regression

- Добавлен `tests/test_telegram_settings_ui.py`: изолированный INI `QSettings` проверяет, что выключенная категория `unknown` сохраняется между экземплярами `TelegramSettingsWidget`, остальные категории остаются включёнными по умолчанию; keyring и Telegram API не вызываются.
- Проверка: `python -m unittest tests.test_telegram_settings_ui -q` — **1 passed**; `py_compile` — PASS; `git diff --check` — PASS.

## 2026-11-27 — C.9 evidence enforced at service boundary

- Evidence requirement перенесён из UI в `ServiceTaskStore.transition()`: прямые вызовы production store также fail-closed для `awaiting_confirmation` и `completed`, если evidence пустой. Состояние не изменяется при отказе; операторская форма продолжает передавать введённую заметку.
- `python -m unittest tests.test_file_catalog_service_tasks -q` — **5 passed**; `py_compile` — PASS; `git diff --check` — PASS.

## 2026-11-28 — C.8 operator file reservation order binding

- `OperatorFulfillmentPanel.reserve_file()` теперь требует существующий канонический `gd_orders.order_id` до явного подтверждения и вызова `FileCatalog.reserve()`. Неизвестный Order ID не создаёт orphan file reservation; low-level catalog contract не изменён.
- Проверки: `python -m unittest tests.test_file_catalog_service_tasks -q` — **6 passed**; `py_compile` — PASS; `git diff --check` — PASS. Реальная файловая передача не выполнялась.

## 2026-11-28 — Stage C local acceptance checkpoint after C.7-C.10 hardening

- Совместная целевая регрессия `python -m unittest tests.test_digital_codes tests.test_file_catalog_service_tasks tests.test_telegram_settings_ui tests.test_delivery_chat_commands_telegram tests.test_app_startup tests.test_trade_delivery_ui -q` — **62 tests, OK** за 37.813 s.
- `python -m compileall -q app core integrations tests` — PASS; `git diff --check` — PASS по содержательным изменениям.
- Покрыты текущие локальные gates: protected digital-code lifecycle и canonical order binding, file integrity/manual gate и canonical order binding, service state/evidence guard, Telegram event-filter persistence/runtime buyer bridge, startup и trade UI regressions.
- Реальные Playerok/Steam/Telegram/file operations не выполнялись. Остались внешние gates: live Playerok session/sales, live Telegram buyer reply, автоматическая file adapter delivery, пользовательская SQLite migration и remote CI после разрешённого commit/push.

## 2026-11-28 — release hygiene and packaged smoke checkpoint

- Проверено текущее рабочее дерево: `PLAYEROK_LOCAL_REVIEW.patch` не изменён и не попал в status; tracked SQLite/DLL/PDB/dist/build artifacts отсутствуют среди изменений. Scan diff не обнаружил реальные token/cookie/trade URL secrets; совпадения относятся к mock values, credential field names и safe proxy handling.
- Ранее собранный `dist/GameDelivery/GameDelivery.exe` повторно запущен с `--self-test` и `QT_QPA_PLATFORM=offscreen`; процесс завершился без ошибки/диагностики.
- Реальные Steam/Playerok/Telegram/file operations не выполнялись; commit/push не выполнялись. Remote CI остаётся непроверенным до разрешённой публикации.

## 2026-11-28 — packaged build refreshed after current UI changes

- Выполнена свежая локальная PyInstaller onedir-сборка по параметрам `.github/workflows/package-windows.yml`: PyInstaller 6.22.3, Python 3.14.6, Windows 11; `pyinstaller --noconfirm --clean --windowed --onedir --name GameDelivery` с теми же collect-all/collect-submodules флагами завершился успешно.
- Свежий `dist/GameDelivery/GameDelivery.exe` запущен с `QT_QPA_PLATFORM=offscreen --self-test`; процесс завершился без ошибки. Размер executable **6,745,786 bytes**, timestamp **2026-11-28 00:08:03 +03:00**.
- PyInstaller вывел только известное предупреждение об отсутствующем optional Qt QML plugin `Qt/labs/assetdownloader`; основной executable и smoke не затронуты. Реальные внешние операции не выполнялись.

## 2026-11-28 — Stage C targeted verification after current hardening

- Уже запущенная целевая проверка завершилась без повторного запуска: `python -m unittest tests.test_file_catalog_service_tasks tests.test_digital_codes tests.test_telegram_settings_ui tests.test_delivery_chat_commands_telegram tests.test_app_startup -q` — **43 tests, OK** за 16.676 s.
- `python -m compileall -q app core integrations tests` — exit code 0; `git diff --check` — exit code 0. Вывод о `local storage initialization failed` относится к предусмотренным mock/error-path тестам и не привёл к падению suite.
- Текущая ветка `work/local-sync`; изменения остаются незакоммиченными. `PLAYEROK_LOCAL_REVIEW.patch` не затронут. Реальные Steam/Playerok/Telegram/file операции и commit/push не выполнялись.
- Остаточные внешние gates не изменились: live Playerok session/sales, live Telegram buyer reply, автоматический file adapter, пользовательская SQLite migration и remote GitHub CI после разрешённой публикации. Следующий локальный шаг при наличии конкретного gap — адресный аудит соответствующего production workflow; C.7 не запускался отдельным внешним действием.

## 2026-11-28 — Telegram proxy credential clearing hardening

- В ходе локального аудита найден production gap в Settings: сохранение пустого proxy не удаляло прежние `proxy_url`, username и password из OS keyring, поэтому runtime мог продолжать использовать устаревший proxy.
- `TelegramCredentialStore.configure()` теперь атомарно очищает optional proxy credentials перед записью нового набора; `clear_proxy()` безопасно обрабатывает отсутствующие optional entries. Секретные значения не выводятся и не записываются в файлы.
- Добавлен regression `test_clearing_proxy_removes_stale_keyring_credentials`; `python -m unittest tests.test_telegram_settings_ui -q` — **2 passed**; `py_compile` затронутых модулей — PASS; `git diff --check` — PASS.
- Реальный Telegram API и пользовательские credentials не использовались. Ветка `work/local-sync`, commit/push не выполнялись; внешние live gates остаются без изменений.

## 2026-11-28 — C.9 canonical service-order binding

- Локальный аудит обнаружил gap в `OperatorFulfillmentPanel.reserve_service()`: UI мог создать `gd_service_units` с произвольным или отсутствующим `Order ID`, в отличие от уже защищённых digital-code и file reservation путей.
- UI boundary теперь требует существующую canonical таблицу `gd_orders` и точное совпадение `order_id` до вызова `ServiceTaskStore.reserve()`; standalone low-level service store и state machine не изменялись.
- Добавлен regression на блокировку orphan service reservation и успешную резервацию после появления canonical order. `python -m unittest tests.test_file_catalog_service_tasks -q` — **7 passed**; `py_compile` затронутых файлов — PASS; `git diff --check` — PASS.
- Реальные услуги, пользовательская SQLite БД и внешние передачи не выполнялись. Ветка `work/local-sync`; commit/push не выполнялись.

## 2026-11-28 — Stage C UI/runtime integration regression

- После canonical service-order binding выполнена связанная проверка `python -m unittest tests.test_app_startup tests.test_delivery_chat_commands_telegram tests.test_telegram_settings_ui tests.test_file_catalog_service_tasks -q` — **40 tests, OK** за 16.569 s.
- `py_compile` затронутых application/integration/test файлов — PASS; `git diff --check` — PASS. Сообщения `local storage initialization failed` относятся к ожидаемым mock/error-path startup tests и не привели к падению suite.
- Проверены startup contract, Telegram runtime/buyer bridge, proxy credential lifecycle, event filters, digital/file/service operator boundaries. Реальные Telegram/Playerok/file/Steam операции не выполнялись.
- Ветка `work/local-sync`; незакоммиченные изменения сохранены, `PLAYEROK_LOCAL_REVIEW.patch` не затронут, commit/push не выполнялись.

## 2026-11-28 — local-sync continuation verification

- Revalidated the currently changed integration slices without repeating the recorded 1120/1126 full-suite runs: `tests.test_playerok_sync tests.test_playerok_listing_stock_handoff` — **37 passed**; `tests.test_file_catalog_service_tasks tests.test_telegram_settings_ui tests.test_delivery_chat_commands_telegram` — **26 passed**; `tests.test_playerok_browser_and_listing_config` — **50 passed**.
- `python -m compileall -q app core integrations tests` and `git diff --check` remain clean.
- A combined run including `tests.test_app_startup` was stopped after the existing startup smoke process stalled during `test_game_catalog_contains_requested_games_and_filters_search` immediately after the expected mocked local-storage initialization warnings. The focused non-startup slices pass; this startup hang is now an unresolved local verification issue and was not masked as success.
- No commit/push, main/PR #4 changes, real Steam/Playerok/Telegram/file operations, or user database operations were performed. Existing uncommitted work remains intact.

## 2026-11-28 — headless startup error handling hardening

- Fixed a test/package smoke hazard in `MainWindow`: database startup errors no longer open a modal `QMessageBox` when `QT_QPA_PLATFORM=offscreen`; the visible startup error label and logger remain authoritative, while interactive Windows runs retain the modal alert.
- This prevents headless/self-test processes from hanging indefinitely on an expected startup error. No database recovery semantics were weakened.
- Direct diagnosis confirmed the current sandbox cannot write the real user DB (`OperationalError: attempt to write a readonly database`); this is an environment permission issue, not a migration corruption. The catalog test still requires an isolated writable DB fixture and remains a separate test-environment failure.
- `python -m compileall -q app core integrations tests` — PASS; `git diff --check` — PASS. No external operations or commit/push performed.

## 2026-11-28 — isolated database path and Windows self-test hardening

- Added opt-in `GAMEDELIVERY_DATABASE_PATH` override to `MainWindow`; normal Windows runs still use `%USERPROFILE%\\.gamedelivery\\gamedelivery.sqlite3`.
- `python -m app --self-test` now uses a temporary isolated SQLite database and marks self-test mode explicitly, so packaging smoke cannot mutate or lock the user's production database.
- Startup error dialogs remain modal for interactive runs and are suppressed only for explicit self-test mode; existing error-dialog tests remain valid.
- Verification: `QT_QPA_PLATFORM=offscreen python -m app --self-test` — PASS; `tests.test_app_startup` with a writable temporary database — **14 tests, OK**; compileall and diff check — PASS.
- No real external operations, commit, or push performed.

## 2026-11-28 — refreshed Windows package after self-test isolation changes

- Rebuilt the onedir Windows package with the exact workflow flags from `.github/workflows/package-windows.yml` using PyInstaller 6.22.3 / Python 3.14.6.
- Packaged executable smoke: `QT_QPA_PLATFORM=offscreen .\\dist\\GameDelivery\\GameDelivery.exe --self-test` — exit code 0.
- Build completed successfully; known optional Qt QML `assetdownloader` warning remains non-blocking.
- This local package verifies the isolated self-test path, but does not replace the required remote CI artifact or live Steam/Playerok/Telegram/file acceptance. No commit/push or external mutations performed.

## 2026-11-28 — C.3/C.4 regression after packaging hardening

- Revalidated the core recovery and Playerok transaction surfaces after the isolated self-test/database-path changes: `tests.test_delivery_dispatcher tests.test_delivery_recovery tests.test_playerok_sync tests.test_playerok_listing_stock_handoff tests.test_main_delivery_send` — **118 passed** in 48.708 s.
- Expected mock warnings cover fail-closed freshness/blocked paths and UNKNOWN trade-send behavior; no real ASF, Steam, Playerok or Telegram operation occurred.
- Fresh packaged smoke and compile/diff checks remain green. C.3/C.4 local regression behavior is unchanged by the packaging hardening.

## 2026-11-28 — Telegram settings adapter compatibility correction

- Full-suite verification surfaced a real compatibility defect in `_schedule_telegram_runtime()`: it called `settings.value(..., type=bool)`, while the lightweight settings adapter used by runtime tests supports only `(key, default)`. Replaced it with a local boolean normalization helper that accepts both QSettings and test adapters, including string values `0/false/no/off`.
- The same full-suite run also showed that an externally supplied `GAMEDELIVERY_DATABASE_PATH` must not redirect normal application storage. The override is now honored only when explicit `GAMEDELIVERY_SELF_TEST=1`; normal Windows runs always retain the canonical per-user database path.
- Focused Telegram regression after the fix: `tests.test_delivery_chat_commands_telegram` — **17 passed**. Packaged self-test remains PASS.
- The attempted full suite reached 1134 tests but was not accepted as green: it ran against the pre-fix code and had environment-sensitive proxy-path failures plus a Qt shutdown crash. No claim of full-suite success is made; the focused regression and self-test are the authoritative post-fix checks.

## 2026-11-28 — service audit ordering fix from full-suite evidence

- The isolated full-suite run exposed one genuine defect: multiple service transitions within SQLite's millisecond-resolution default timestamp could make `ORDER BY created_at DESC` return the earlier audit row, hiding the latest operator evidence.
- `ServiceTaskStore._audit()` now writes an explicit UTC microsecond timestamp. State transitions and evidence requirements are unchanged; audit readers can deterministically retrieve the latest evidence.
- Regression: `python -m unittest tests.test_file_catalog_service_tasks -q` — **7 passed**. `compileall` and `git diff --check` — PASS.
- The prior isolated full suite reached **1134 tests** and had only this service-audit assertion failure before Qt shutdown; it was not counted as green. The defect is now corrected locally.

## 2026-11-28 — Qt shutdown lifecycle hardening

- Added explicit `MainWindow.closeEvent()` cleanup: periodic Telegram/reconciliation timers stop before QObject teardown, outstanding ASF worker callbacks are disconnected, and late worker results are ignored via a `_closing` guard.
- `_run_asf_call_async()` now refuses new work after shutdown; `_dispatch_asf_call_result()` never invokes callbacks on a closing window. This preserves active operation durability while preventing late GUI callbacks during packaged exit/test teardown.
- The change targets the Qt shutdown crash observed after the 1134-test run. Focused startup and Telegram paths remain covered; packaged self-test and compile checks are rerun separately.

## 2026-11-28 — Qt lifecycle hardening packaged verification

- Rebuilt the Windows onedir package after adding `MainWindow.closeEvent()` cleanup and ASF callback shutdown guards.
- Fresh packaged smoke `QT_QPA_PLATFORM=offscreen .\\dist\\GameDelivery\\GameDelivery.exe --self-test` — exit code 0.
- Focused post-change regression `tests.test_delivery_chat_commands_telegram tests.test_file_catalog_service_tasks` — **24 passed**; compileall and diff-check passed.
- The generated `GameDelivery.spec` is a local PyInstaller build artifact and remains untracked; source changes are limited to the requested worktree.

## 2026-11-28 — full regression after Qt shutdown guards

- Isolated full suite rerun after the closeEvent/late-callback hardening: **1134 tests, OK** in 186.324 s. All assertions passed.
- The Python process still terminates with Windows code `0xC0000409` after unittest reports OK, so the result is recorded as assertion-green but process-exit-unverified; this is consistent with the repository's known intermittent Qt shutdown issue and is not falsely marked as a clean full-suite exit.
- `tests.test_app_startup` alone — **14 passed**; `tests.test_cs2_inventory_panel` alone — **32 passed**. Focused post-change suites and packaged smoke remain green.
- Further Qt/WebEngine teardown isolation remains an open local hardening task. No external operations, commit, or push performed.

## 2026-11-28 — deterministic Qt headless test/package environment

- Re-ran the isolated full suite with `QT_QPA_PLATFORM=offscreen`, `QT_QUICK_BACKEND=software`, and `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu`: **1134 tests, OK**, clean process exit code 0 in 193.476 s.
- The software Qt/WebEngine settings eliminate the post-OK Windows Qt shutdown crash observed under the bare offscreen environment without changing application behavior.
- Updated `.github/workflows/tests.yml` full-suite and isolation environments with the same deterministic Qt settings. Updated `.github/workflows/package-windows.yml` packaged smoke environment likewise.
- `compileall` and `git diff --check` — PASS. No external operations, commit, or push performed.

## 2026-11-28 — worktree hygiene checkpoint

- Removed only the generated untracked `GameDelivery.spec` PyInstaller artifact; no source or user data was removed.
- Current worktree remains limited to the existing requested implementation/test/workflow changes plus `tests/test_telegram_settings_ui.py`.
- `compileall` and `git diff --check` remain PASS. Full isolated suite evidence is 1134/1134 assertions with clean exit under deterministic Qt settings.

## 2026-11-28 — packaged self-test documentation

- Updated the generated Windows package instructions to document `GameDelivery.exe --self-test` as a no-network startup smoke test using a temporary isolated database, with no user-data mutation.
- Workflow syntax remains clean under `git diff --check`; no application behavior or licensing changed.

## 2026-11-28 — self-test environment restoration

- Hardened `app.__main__._dispatch(--self-test)` to restore all temporary environment variables (`GAMEDELIVERY_TEST_ASF_IPC_URL`, `GAMEDELIVERY_SELF_TEST`, `GAMEDELIVERY_DATABASE_PATH`) in a `finally` block when invoked in-process.
- Packaged/headless self-test after the change — exit code 0; `py_compile` and `git diff --check` — PASS.
- This prevents self-test harness state from leaking into subsequent in-process commands while preserving isolated temporary storage.

## 2026-11-28 — self-test environment restoration regression

- Direct in-process regression confirmed `_dispatch(['--self-test'])` returns 0 and restores sentinel values for all three environment variables after completion.
- This validates both isolated startup and non-leaking harness state; compileall/diff-check remain clean.

## 2026-11-28 — final local package refresh after self-test restoration

- Rebuilt the onedir package after the final `--self-test` environment restoration change using the workflow's PyInstaller flags.
- Fresh packaged executable smoke with deterministic Qt settings exited 0.
- Removed the generated untracked `.spec` file after the build; current status contains only the intended source/workflow/test/log changes.

## 2026-11-28 — startup error comment alignment

- Corrected the `MainWindow` startup-error comment to match actual behavior: modal suppression is limited to explicit `GAMEDELIVERY_SELF_TEST=1`, not all offscreen runs. Runtime behavior is unchanged.
- `py_compile` and `git diff --check` — PASS.

## 2026-11-28 — resumed-goal smoke checkpoint

- After goal resumption, reran the current executable path: `python -m app --self-test` — PASS.
- Post-resumption focused regression `tests.test_delivery_chat_commands_telegram tests.test_file_catalog_service_tasks` — **24 passed**.
- `compileall` and `git diff --check` — PASS; no external operations or user database access performed.

## 2026-11-28 — import/style cleanup checkpoint

- Reformatted the Telegram outbox import in `app/main.py` into a maintainable multiline import; behavior unchanged.
- Focused regression after cleanup: **24 passed** (`tests.test_delivery_chat_commands_telegram`, `tests.test_file_catalog_service_tasks`). Compileall and diff-check passed.

## 2026-11-28 — startup regression checkpoint after import cleanup

- `tests.test_app_startup` under deterministic Qt settings — **14 passed**.
- Expected startup error-path logs were emitted for deliberately invalid/mock storage cases; no user database was modified.
- Compileall and diff-check remain PASS.

## 2026-11-28 — NEXT GOAL BLOCK: Remote CI and final acceptance (authorization recorded)

Пользователь разрешил следующий этап долгосрочной Goal GameDelivery: после явного нажатия «Возобновить цель» разрешены проверка, commit и push только проверенного состояния в `origin/work/local-sync`, без merge, force push, изменения `main` или закрытия/слияния PR #4. До явного возобновления публикация не выполняется.

После публикации требуется дождаться и проверить каждый обязательный GitHub Actions job, исправлять только подтверждённые CI-проблемы в `work/local-sync`, выполнять локальные проверки и публиковать follow-up commits только в эту ветку до успешного CI. После успешного CI live Steam/ASF, Playerok, Telegram и file-delivery acceptance остаются отдельными контролируемыми планами; реальные внешние операции не выполнять без отдельного разрешения.

Последний локальный evidence перед этапом: 1134/1134 tests OK при deterministic Qt settings, Windows onedir package/self-test PASS, compileall PASS, `git diff --check` PASS. Этот checkpoint только фиксирует следующий goal block и границы разрешения; commit/push не выполнялись.

## 2026-11-28 — publication checkpoint

- Pre-publication status/diff/file audit completed on work/local-sync; all included files were GameDelivery changes and no unrelated user changes were included.
- Published implementation commit: 2ee0e8a2306c55ee9a0e3b956d8980b101bfc607 (local commit created; publication followed in the next checkpoint).
- Scope remains limited to work/local-sync; no merge, force push, main, or PR #4 operation.


## 2026-11-28 — Remote CI acceptance checkpoint

- Published commits on `origin/work/local-sync`: implementation `2ee0e8a2306c55ee9a0e3b956d8980b101bfc607`; publication-log checkpoint `4087a83c7c2b4624cb4064cf1259ecf23b821e9e`.
- GitHub Actions for exact HEAD `4087a83c7c2b4624cb4064cf1259ecf23b821e9e` completed successfully:
  - Tests run `46132226534`, job `test`: success.
  - Windows package run `46132226443`, job `build-windows`: success.
  - ASF plugin run `46132226430`: `build-plugin`, `generic-runtime-smoke`, and `windows-storage-acl-smoke` succeeded; `win-x64-trimmed-runtime-smoke` was skipped by workflow conditions (not a failure).
- No CI failure required a follow-up code change. No merge, force push, `main`, or PR #4 operation was performed.

### Controlled live acceptance plan (no live operations executed)

1. Steam/ASF: prerequisites — isolated test bot, test recipient/account, exact approved asset/order fingerprint, ASF IPC health, and explicit live-send permission. Scenario — read-only inventory/recipient preflight, no-send preview, then one authorized test delivery; verify journal, offer ID/status and post-delivery inventory. Risks — timeout/unknown outcome, mobile confirmation, recipient decline, stale/moved asset. Recovery — stop retries, preserve UNKNOWN, reconcile read-only by exact offer/order fingerprint, then manually decide.
2. Playerok: prerequisites — isolated/test listing and authenticated internal-browser session, approved test order, explicit mutation permission. Scenario — inspect order and run one controlled message/status or adapter action; verify durable event/state and UI audit. Risks — duplicate message, browser timeout, session/anti-bot failure. Recovery — leave dispatching/unknown state durable, do not retry blindly, reconcile in browser and use manual operator action.
3. Telegram: prerequisites — dedicated bot token/chat, authorized buyer/test chat, configured proxy if needed, explicit message permission. Scenario — send one controlled test notification/reply through outbox and verify pending→sent, update offset and audit. Risks — wrong chat, duplicate delivery, API/proxy outage. Recovery — revoke/clear credentials if misconfigured, keep outbox retry/unknown state, deduplicate by event key, retry only after verification.
4. File delivery: prerequisites — isolated test catalog item, non-sensitive test file, approved order, destination and explicit transfer permission. Scenario — one controlled delivery through the file handler, verify exact file hash/size, durable delivery/evidence/audit and no path escape. Risks — wrong file/recipient, partial transfer, path or permission error. Recovery — mark failed/unknown, do not re-send automatically, verify destination and hash, then manual retry with a new idempotency decision.

Actual Steam/ASF, Playerok, Telegram messages and real file transfers remain prohibited until separate explicit permission.

## 2026-11-28 — final remote CI for acceptance-plan checkpoint

- The follow-up log commit `f90267cb2b271c053ed300b1f6ba008ef5b9e239` triggered the required workflows on the exact latest SHA.
- Tests run `46132527183`, job `test`: success.
- Windows package run `46132527182`, job `build-windows`: success.
- The latest branch state is therefore CI-green for both required workflows; no follow-up fix is required.
- Remaining Stage C gates are controlled live acceptance only; no external mutation is performed in this checkpoint.

## 2026-11-28 — checkpoint after log correction

- Corrected the publication checkpoint to record the actual implementation SHA instead of the literal PowerShell placeholder.
- Correction commit: `732e8cf75c718db4254f5377272bcb5f4b963a4d`.
- Remote Tests run `46133115657` and Windows package run `46133115690` for this exact SHA both completed successfully.
- Worktree is clean; no new functional change was introduced by this correction.

## 2026-11-28 — digital-code exactly-once reveal hardening

- Corrected `DigitalCodeStore.reveal`: a code can now be revealed only from `reserved`; a second reveal is rejected instead of returning the plaintext again and writing a duplicate `revealed` audit event. This enforces the approved exactly-once digital-code delivery invariant; explicit UNKNOWN reconcile remains required before any later lifecycle decision.
- Updated the regression to prove second reveal is rejected and the audit contains one `revealed` action only.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **72 passed**; compileall — PASS; `git diff --check` — PASS. No external operations or user database access performed.

## 2026-11-28 — file reservation order binding hardening

- Fixed the managed file catalog reservation boundary: `gd_files.order_id` is now persisted when a file is reserved for manual delivery, with compatibility ALTER for existing databases that predate the column.
- Empty order IDs are rejected by the store, and the transaction keeps the exact file-to-order binding together with the `MANUAL_REQUIRED` state.
- Regression now verifies the persisted order binding.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **72 passed**; compileall — PASS; `git diff --check` — PASS. No external file transfer or user database access performed.

## 2026-11-28 — file catalog replacement safety hardening

- Prevented re-registering a managed path while its prior file unit is `reserved`, `manual_required`, or `delivered`; this avoids silently clearing an active order binding or rewriting delivery history.
- Re-registering an inactive catalog row resets its old `order_id` explicitly before making the new content available. New rows initialize with a null binding.
- Regression covers the active-reservation replacement block.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **72 passed**; compileall — PASS; `git diff --check` — PASS. No external file transfer performed.

## 2026-11-28 — service reservation identity hardening

- Enforced non-empty `product_id` and canonical `order_id` at `ServiceTaskStore.reserve`, not only in the operator UI. The store now trims both identifiers before persistence, preventing headless callers from creating unbound service units.
- Added a store-boundary regression for empty order IDs.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **73 passed**; compileall — PASS; `git diff --check` — PASS. No external operations performed.

## 2026-11-28 — Telegram outbox concurrent-claim recovery hardening

- Hardened `TelegramBotOutbox.deliver_once`: rows are atomically claimed as `sending` before the external request, preventing concurrent workers from selecting and sending the same notification.
- Added stale-claim recovery: a `sending` row older than five minutes is returned to `retry` before the next delivery pass, so a worker crash does not strand notifications permanently.
- Success and failure updates now require ownership of the `sending` claim.
- Added regression for recovery after a simulated crashed worker.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **74 passed**; compileall — PASS; `git diff --check` — PASS. No Telegram API or other external operation performed.

## 2026-11-28 — bounded Telegram outbox retry hardening

- Added a bounded retry policy to `TelegramBotOutbox`: after five failed delivery attempts the notification becomes terminal `failed` and no further automatic retry is scheduled.
- The failure result is returned as `failed`, while earlier attempts remain `retry` with the existing delay. This prevents an unavailable Telegram endpoint from causing an unbounded worker loop.
- Added regression for the terminal fifth attempt.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **75 passed**; compileall — PASS; `git diff --check` — PASS. No Telegram API or other external operation performed.

## 2026-11-28 — digital stock identifier normalization

- Hardened both digital-code reservation entry points to trim and persist canonical `product_id`, `order_id`, and `order_line_id` values at the store boundary.
- Whitespace-padded identifiers can no longer pass validation but miss stock rows or create non-canonical order bindings.
- Added regression for normalized direct reservation.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **76 passed**; compileall — PASS; `git diff --check` — PASS. No external operations performed.

## 2026-11-28 — digital order-line reservation idempotency

- Added durable `order_line_id` binding to digital-code stock, including compatibility migration for existing databases and a transaction-local schema guard for legacy callers.
- `reserve_for_order_line` is now idempotent: repeating the same order-line reservation returns the existing code IDs; a quantity mismatch raises `DigitalCodeConflict` instead of reserving additional codes.
- Added regression for repeated order-line reservation and preserved optional-item/legacy-schema behavior.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **77 passed**; compileall — PASS; `git diff --check` — PASS. No external operations performed.

## 2026-11-28 — Telegram outbox input-boundary hardening

- `TelegramBotOutbox.enqueue` now rejects empty event types, empty chat IDs, and non-object payloads before creating durable rows.
- Event and chat identifiers are normalized at the outbox boundary, preventing unusable notifications and ambiguous idempotency keys from headless callers.
- Added regression coverage for all three invalid-input classes.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **78 passed**; compileall — PASS; `git diff --check` — PASS. No external operations performed.

## 2026-11-28 — service quantity type hardening

- Tightened `ServiceTaskStore.reserve` to require an actual integer quantity; Python boolean values are no longer accepted as quantity `1`.
- Added regression for the boolean quantity edge case at the store boundary.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **78 passed**; compileall — PASS; `git diff --check` — PASS. No external operations performed.

## 2026-11-28 — idempotent manual file reservation

- Made `FileCatalog.reserve` idempotent for the same `file_id + order_id` after it reaches `MANUAL_REQUIRED`; repeated operator clicks now return the existing binding instead of reporting a misleading conflict.
- A different order or any other active/terminal state remains fail-closed.
- Added regression for repeated reservation of the same manual file unit.
- Verification: targeted C.7/C.8/C.9/C.10 regression — **78 passed**; compileall — PASS; `git diff --check` — PASS. No external file transfer performed.

## Frontend Stage 0 audit checkpoint (2026-11-28)

Stage 0 of the new Electron + React + TypeScript frontend specification completed. Repository is currently a PySide6 application: `app/__main__.py` → `app/main.py::MainWindow`; no Electron/React/Vite/TypeScript scaffold, generic local HTTP API, or generic backend WebSocket transport exists. Actual integration boundaries were mapped for ASF IPC, Playerok browser/GraphQL/Safe Sync, orders/delivery/reservations, operator digital codes/files/services, inventory, proxy/OpenSkin and Telegram. Findings are recorded in `docs/frontend/BACKEND_INTEGRATION_MAP.md`; continuation state is recorded in `docs/frontend/FRONTEND_IMPLEMENTATION_PROGRESS.md`.

No business logic or existing frontend was removed. No external operations were performed. Next slice is a non-destructive Stage 1 Electron scaffold with a typed safe bridge and explicit `not_implemented` capability responses where no backend transport exists.
## Frontend Stage 1 scaffold checkpoint (2026-11-28)

Created a non-destructive `frontend/` Electron/Vite/React/TypeScript scaffold with secure BrowserWindow defaults (`contextIsolation: true`, `nodeIntegration: false`, sandbox enabled), preload IPC bridge, explicit `not_implemented` backend transport capability, and minimal renderer startup page. Existing PySide6 app and backend were not modified. No fake endpoint or business operation was added. Package installation/build was not run because dependencies are not installed yet; next step is install frontend dependencies and add UI Kit/AppShell only after validating the scaffold.

## Frontend Stage 1 scaffold checkpoint (2026-11-28)

Created initial Electron/Vite/React/TypeScript scaffold under `frontend/`, secure preload bridge, and explicit unavailable backend transport. No business logic or external operations changed.

## Frontend Stage 1 scaffold checkpoint (2026-11-28)

Created the initial `frontend/` Electron/Vite/React/TypeScript scaffold with secure BrowserWindow settings and a minimal typed preload bridge. The only exposed capability is an explicit `backend-transport: not_implemented`; no fake backend endpoints or business operations were added. Existing PySide6 frontend, Python backend, ASF, Playerok and delivery logic were not changed. Dependency installation, typecheck/build and Electron startup smoke remain to be verified in the next slice.

## Frontend Stage 1 working renderer checkpoint (2026-11-28)

Stage 1 advanced beyond empty scaffold: frontend dependencies and lockfile were installed; Tailwind/PostCSS configuration was added; a working React renderer now exposes eight navigable sections through a Zustand store, secure Electron preload/main files, topbar status, explicit backend-unavailable states and a collapsible console. No fake backend data or business operations were introduced. `npm run typecheck` and `npm run build` pass; `git diff --check` passes. Electron runtime smoke and real Python backend transport remain pending. Existing PySide6/backend/business logic remains unchanged.

## Frontend read-only Python bridge checkpoint (2026-11-28)

Added a minimal real local JSONL bridge for capability discovery from actual Python module availability. Electron main starts/stops the bridge and exposes only the typed capability query via preload. Unknown methods return explicit `not_implemented`; no mutations or secrets are handled. Existing business logic remains unchanged.

## Frontend bridge verification correction (2026-11-28)

The local JSONL bridge was corrected to add the repository root to `sys.path`. Direct capability probe now returns actual importability states for orders, Playerok, ASF, digital codes, files, services, Telegram and OpenSkin; unknown methods return explicit `not_implemented`. Renderer security smoke, TypeScript typecheck and Vite production build pass after the correction.

## Orders read-only bridge slice (2026-11-28)

Extended the real local JSONL bridge with `orders.list`, backed by the existing `core.orders.service.OrderService` and configured database path (`GAMEDELIVERY_DATABASE_PATH`, otherwise the existing user state location). The operation is read-only and serializes the existing order records; no order transitions, reservation changes or delivery actions are exposed. Unknown methods remain explicit `not_implemented`.

Electron preload now exposes only `getCapabilities` and `listOrders`; Electron main owns the bridge request/response flow. The React renderer reads the real order snapshot and displays the confirmed order count without optimistic or mock data. Direct probe returned `{"ok":true,"orders":[]}` for the current empty database.

Checks: `npm run renderer-smoke` passed (security contract, TypeScript typecheck, Vite build); `python -m py_compile backend_bridge.py` passed; unknown bridge method returned `not_implemented`; `git diff --check` passed. No external operations or business mutations were performed.

## Checkpoint — typed read-only desktop snapshot (2026-11-28)
- Added typed renderer contracts for capabilities and order snapshots, plus a small backend client with explicit bridge error handling.
- Dashboard now renders only real local `capabilities` and `orders.list` results; Orders section exposes a read-only table and empty/error states.
- JSONL bridge now parses JSON requests generically while preserving the allowlisted methods (`capabilities`, `orders.list`) and explicit `not_implemented` responses.
- Verification: `npm run renderer-smoke` PASS (security smoke, TypeScript, Vite build); `python -m py_compile backend_bridge.py` PASS; direct bridge probe for capabilities/orders/unknown PASS; `git diff --check` PASS.
- No external operations, Steam actions, commits, or pushes performed.

## Checkpoint — reusable UI primitives and shell status (2026-11-28)
- Added reusable UI primitives for Button, Card, EmptyState and StatusIndicator under `frontend/src/components/ui/`.
- Refined AppShell wording so the current UI clearly identifies the read-only bridge and disabled live actions instead of claiming the bridge is entirely disconnected.
- Added status/metric styling while preserving the existing dark graphite/orange design system and safe read-only behavior.
- Verification: `npm run renderer-smoke` PASS; `git diff --check` PASS. No backend business logic or external operation changed.

## Checkpoint — feature extraction and bridge lifecycle smoke (2026-11-28)
- Moved Dashboard, Orders, and unavailable module rendering out of `App.tsx` into feature components.
- Added a process-level JSONL bridge lifecycle/contract smoke test covering capabilities, orders.list, unknown method handling, response parsing and shutdown.
- Added `bridge-smoke` npm script. No mutating IPC channels or external integrations were added.
- Verification: `npm run renderer-smoke` PASS; `npm run bridge-smoke` PASS; `git diff --check` PASS.

## Checkpoint — bridge failure handling (2026-11-28)
- Hardened Electron bridge request lifecycle: unavailable process, process error/exit, concurrent request rejection, write failure, invalid response, timeout cleanup and shutdown reset are explicit states.
- Extended bridge smoke test with malformed JSON request handling (`invalid_request`).
- No arbitrary IPC, credentials, business mutations or external operations were added.
- Verification: `npm run renderer-smoke` PASS; `npm run bridge-smoke` PASS; `python -m py_compile backend_bridge.py` PASS; `git diff --check` PASS.

## Checkpoint — bridge health and renderer connection state (2026-11-28)
- Added a real local `health` bridge method returning the running Python bridge service state; no external service is contacted.
- Added restricted `gamedelivery:get-health` IPC and typed preload/client contract.
- Dashboard header now distinguishes `BRIDGE ONLINE · READ-ONLY` from `BRIDGE UNAVAILABLE` based on the actual bridge response.
- Extended bridge smoke coverage to retain capabilities, orders, unknown method and malformed request checks.
- Verification: `npm run renderer-smoke` PASS; `npm run bridge-smoke` PASS; `python -m py_compile backend_bridge.py` PASS; `git diff --check` PASS.

## Checkpoint — read-only Orders table controls (2026-11-28)
- Added client-side search, status filtering and created/updated sorting to the read-only Orders view.
- Filters operate only on the verified `orders.list` snapshot; no backend lifecycle or mutation is introduced.
- Long item names retain a native title tooltip and the table keeps a minimum width with horizontal scrolling.
- Verification: `npm run renderer-smoke` PASS; `git diff --check` PASS.

## Checkpoint — read-only operator inventory summary (2026-11-28)
- Added `operator.summary` to the local bridge. It reads only counts from existing `gd_digital_codes`, `gd_files` and `gd_service_units` tables when present; absent tables return zero counts without exposing secrets or file contents.
- Added typed IPC/preload/client contracts and a Dashboard operator-stock card.
- No import, reveal, reserve, file delivery or service transition operation was exposed.
- Verification: `operator.summary` direct probe returned `ok: true` with current counts 0/0/0; `npm run renderer-smoke` PASS; `npm run bridge-smoke` PASS; Python compile PASS.

## Checkpoint — operator summary contract coverage (2026-11-28)
- Extended `frontend/tests/bridge-smoke.mjs` to assert `operator.summary` success and numeric fields for digital codes, files and service units.
- This verifies the new read-only operator contract without initializing schemas, exposing secrets or executing operator actions.
- Verification: `npm run bridge-smoke` PASS; `npm run renderer-smoke` PASS.

## Checkpoint — ASF read-only status adapter (2026-11-28)
- Added `asf.status` to the local bridge using the existing `integrations.asf.client.ASFClient.get_status()` route `/Api/ASF` over loopback only.
- ASF IPC URL is restricted to the existing local default or explicit `ASF_IPC_BASE_URL`; optional password is read from process environment and never returned to renderer/log payloads.
- Unavailable ASF runtime returns explicit `asf_unavailable`; no inventory, trade, confirmation, bot-control or Steam mutation is exposed.
- Added typed preload/client contracts and bridge smoke coverage allowing either a verified status response or explicit unavailable state.
- Current direct probe: `asf_unavailable` because no ASF IPC listener is running.
- Verification: `npm run renderer-smoke` PASS; `npm run bridge-smoke` PASS; Python compile PASS.

## Checkpoint — ASF inventory read-only contract (2026-11-28)
- Added restricted `asf.inventory` bridge operation backed by existing `ASFClient.get_bot_inventory()`.
- Renderer API accepts only bot name and the two existing product contexts: Steam Cards `(753,6)` or CS2 `(730,2)`; the business types remain separated.
- Invalid or missing request data returns `invalid_request`; unavailable ASF returns `asf_unavailable`.
- No inventory selection, reservation, trade offer, confirmation or other mutation was exposed.
- Verification: renderer smoke PASS; bridge smoke PASS including invalid inventory request; Python compile PASS.

## Checkpoint — guarded ASF inventory UI (2026-11-28)
- Added an Inventory feature view with explicit bot name and product-context selection.
- The UI calls only the typed read-only `getASFInventory` API and supports separate Steam Cards `(753,6)` and CS2 `(730,2)` contexts.
- Missing bot input, ASF unavailable state and raw read-only response are shown explicitly; no item selection, reservation or trade action is present.
- Verification: `npm run renderer-smoke` PASS; no Steam operation performed.

## Checkpoint — ASF context separation regression coverage (2026-11-28)
- Extended bridge smoke coverage for valid Steam Cards `(753,6)` and CS2 `(730,2)` inventory requests.
- Added regression assertion that mixed context `(753,2)` is rejected as `invalid_request`.
- Valid requests are accepted by contract and return either real ASF data or explicit `asf_unavailable`; no Steam runtime or credentials are required for the test.
- Verification: `npm run bridge-smoke` PASS; `npm run renderer-smoke` PASS.

## Checkpoint — inventory loading and retry-safe UI state (2026-11-28)
- Added explicit loading state to the Inventory snapshot action; repeated clicks are disabled while the read-only request is in flight.
- Errors clear before a new attempt and loading is reset in `finally`, preserving safe retry behavior without automatic retries.
- Verification: `npm run renderer-smoke` PASS; no ASF or Steam operation performed.

## Checkpoint — ASF and Playerok feature states (2026-11-28)
- Added an ASF feature view using the real `getASFStatus` read-only route, with loading, available and unavailable states.
- Added a Playerok feature view that explicitly documents the current Qt-owned browser/session boundary; it does not invent an API or fall back to an external browser.
- Existing Inventory remains the only ASF inventory action and is read-only.
- Verification: `npm run renderer-smoke` PASS; no external operation performed.

## Checkpoint — explicit Operator and Telegram boundaries (2026-11-28)
- Added Operator feature state: Dashboard read-only counts are available, while code reveal, file delivery and service transitions remain disabled without verified contracts.
- Added Telegram feature state: existing Python-owned settings/runtime remain untouched; token binding, `/start` linking and message sending are explicitly unavailable.
- All eight navigation sections now have a deliberate state or read-only view; no fake success is shown.
- Verification: `npm run renderer-smoke` PASS; no Telegram, operator or external operation performed.

## Checkpoint — production Electron layout (2026-11-28)
- Added `frontend/electron.tsconfig.json` to compile Electron main/preload sources into `dist-electron/`.
- Main now loads the production renderer from `dist/index.html` while keeping the secure preload path and Python bridge path.
- Added `production-smoke` and `tests/production-layout.mjs` to verify renderer build, compiled main/preload, and bridge source presence.
- Verification: `npm run production-smoke` PASS; no Electron window launch or external operation performed.

## Checkpoint — Electron desktop startup smoke (2026-11-28)
- Added `tests/electron-startup.mjs` and `electron-startup-smoke`.
- The check builds renderer and Electron bundles, launches the compiled Electron main process for a bounded interval, verifies it does not exit immediately, then terminates the test process without touching external services.
- Current Windows startup smoke: PASS. The test uses Windows shell spawning for `.cmd` compatibility; Node emits its standard shell-argument deprecation warning, with no test failure.

## Checkpoint — Windows packaging (2026-11-28)
- Added electron-builder configuration `frontend/electron-builder.yml` with Windows NSIS target and unpacked directory target.
- Moved Electron to devDependencies, set package `main` to `dist-electron/main/index.js`, and added `package:dir` / `package:win` scripts.
- Windows packaging completed successfully:
  - unpacked app: `frontend/dist/win-unpacked/`
  - installer: `frontend/dist/GameDelivery-0.1.0-setup.exe`
  - blockmap generated.
- Packaging is unsigned because no code-signing certificate was configured.
- The package includes the Python bridge source but does not bundle a Python interpreter; deployment still requires a compatible Python runtime and existing GameDelivery Python environment. This is documented as a remaining packaging limitation, not claimed as a self-contained installer.
- Verification: `npm run package:dir` PASS; `npm run package:win` PASS; `git diff --check` PASS.

## Checkpoint — packaged EXE startup and hashes (2026-11-28)
- Launched the generated `dist/win-unpacked/GameDelivery.exe` in a bounded hidden Windows smoke test; it remained alive through the startup window and was then terminated by the harness.
- SHA-256 provenance:
  - installer `GameDelivery-0.1.0-setup.exe`: `30F373BB86FDBFE9CF5B65A978CF4DBEE1BB351CBE9F652EE15AC41D3FAB2D27`
  - unpacked `GameDelivery.exe`: `31441F7C2A4CA26FF99054D96F0ED65B59CB07318270244A4D59EB88114A67E7`
- Verification: packaged Windows startup smoke PASS. This confirms launch lifecycle only; Python runtime and live integrations remain deployment prerequisites.

## Checkpoint — Windows installation and data preservation guide (2026-11-28)
- Added `frontend/README.md` with build/package commands, runtime prerequisites, data/config preservation procedure and current integration boundary.
- The guide explicitly preserves the existing SQLite database, keyring, ASF config/data and Playerok profile; it does not instruct destructive replacement or migration.
- Verification basis: package:win and packaged EXE startup smoke already passed; documentation now records the remaining Python-runtime and unsigned-installer limitations.

## Checkpoint — frontend acceptance regression run (2026-11-28)
- Re-ran the targeted final frontend checks after packaging/documentation changes.
- Current branch remains `work/local-sync`; only GameDelivery frontend/docs changes are present as uncommitted work.
- Verification: `npm run electron-startup-smoke` PASS (renderer build, Electron compile, production layout, bounded Electron startup); `npm run bridge-smoke` PASS; `python -m py_compile backend_bridge.py` PASS; `git diff --check` PASS.
- No commit/push, live integration, Steam, Playerok, Telegram or file-delivery operation performed.

## Checkpoint — Remote CI workflow for Electron frontend (2026-11-28)
- Added `.github/workflows/frontend-windows.yml` for Windows CI on frontend changes and `work/local-sync` pushes.
- The workflow installs from `frontend/package-lock.json`, runs renderer/bridge/production smoke checks, builds the Windows NSIS installer and uploads it as an artifact.
- Added an Electron unpacked-package job to the existing Windows packaging workflow for future coordinated CI runs.
- No workflow was executed or published in this turn; commit/push remain unauthorized.
- Local packaging and startup evidence remains PASS from the prior checkpoint.

## Checkpoint — preload allowlist regression hardening (2026-11-28)
- Extended `frontend/tests-security.mjs` to assert every currently exposed IPC channel is present and to reject non-constant `ipcRenderer` channel usage.
- Existing context isolation, node integration, sandbox and direct-runtime API checks remain active.
- Verification: `npm run renderer-smoke` PASS; no external operation performed.

## Checkpoint — partial bridge failure isolation (2026-11-28)
- Changed AppShell data loading from `Promise.all` to `Promise.allSettled` so one unavailable read-only slice cannot hide healthy order/capability/health snapshots.
- Partial failures now produce explicit `partial_bridge_unavailable` while successful independent responses remain visible.
- No retries or mutations were added.
- Verification: `npm run renderer-smoke` PASS; `git diff --check` PASS.

## Checkpoint — controlled live acceptance plan (2026-11-28)
- Added `docs/frontend/LIVE_ACCEPTANCE_PLAN.md` covering Steam/ASF, Playerok, Telegram and file delivery.
- Each section defines preconditions, one controlled scenario, expected result, verification evidence, risks and recovery behavior.
- The plan explicitly stops before real external operations and preserves fail-closed/UNKNOWN handling; no live operation was performed.

## Checkpoint — final local scope and secret scan (2026-11-28)
- Confirmed working branch remains `work/local-sync` with no commits or pushes.
- Reviewed current status: only GameDelivery frontend, workflow, docs and Master Log changes are present.
- `git diff --check` PASS.
- Searched frontend, new workflow and frontend docs for credential-like literals; no embedded IPC passwords, bot tokens, API keys or secrets were found.
- Live acceptance remains a prepared plan only; no external mutation was executed.

## Checkpoint — packaged Python bridge path fix (2026-11-28)
- Fixed the packaged-app bridge path: `backend_bridge.py` is now an electron-builder `extraResources` file outside `app.asar`, and packaged Main resolves it through `process.resourcesPath`; development still resolves the repository path.
- This avoids attempting to launch an external Python process against an asar virtual path.
- Verified `dist/win-unpacked/resources/backend_bridge.py` exists.
- Rebuilt package directory and launched the packaged EXE in a bounded Windows smoke test: PASS.

## Checkpoint — final NSIS rebuild after bridge path correction (2026-11-28)
- Rebuilt the Windows NSIS installer after externalizing `backend_bridge.py` to `resources/`.
- `npm run package:win` PASS; `dist/win-unpacked/resources/backend_bridge.py` exists.
- Final installer SHA-256: `6CEA02BE2EF6214E14F35B2C2D64BD355896CDC022BE96093B1C5CB1D50D4298`.
- Installer remains unsigned and Python runtime remains a documented prerequisite.

## Checkpoint — final packaged EXE regression (2026-11-28)
- Launched the final `dist/win-unpacked/GameDelivery.exe` produced after the NSIS rebuild.
- The packaged EXE remained alive through the bounded startup interval and was terminated by the harness.
- `git diff --check` PASS.
- No external integration or business mutation was performed.

## Remote CI publication checkpoint (2026-11-28)
- Published verified frontend state to `origin/work/local-sync`.
- Commits: `ef2a7be` (Electron frontend), `adae5d7` (Python setup for Electron checks), `6692b54` (Python setup for packaging job), `6acde3b` (bounded frontend job), `68fd573` (backend dependencies for bridge smoke).
- Final published commit: `68fd5732781ec11737befcb46a02f3b2079e035b`.
- Remote CI for final published commit completed successfully:
  - Electron frontend: run `46164663166`, https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/46164663166
  - Windows executable/package: run `46164663196`, https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/46164663196
  - Tests: run `46164663152`, https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/46164663152
- All jobs completed with conclusion `success`. No merge, force push, main or PR #4 changes.

## Remote CI final post-publication verification (2026-11-28)
- Documentation-only follow-up commit: `603d1870c6724993cf3e7d117af9f654351b09`.
- For that exact published revision, final required workflows completed successfully:
  - Tests: run `46165126956`, https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/46165126956
  - Windows executable/package: run `46165126966`, https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/46165126966
- Electron frontend workflow had already passed for the code-bearing revision `68fd5732781ec11737befcb46a02f3b2079e035b` as run `46164663166`; the follow-up changed documentation only.

## Checkpoint — live acceptance record template (2026-11-28)
- Added `docs/frontend/LIVE_ACCEPTANCE_RECORD_TEMPLATE.md` for human-controlled Steam/ASF, Playerok, Telegram and file-delivery acceptance.
- Template requires approval references, evidence, explicit result states and recovery notes; it forbids recording secrets and treats incomplete evidence as UNKNOWN.
- No live operation was performed.

## Checkpoint — live acceptance documentation smoke (2026-11-28)
- Added `frontend/tests/live-plan-smoke.mjs` and `npm run live-plan-smoke`.
- The check verifies that the controlled plan contains Steam/ASF, Playerok, Telegram, file-delivery sections, required preconditions/scenario/evidence/recovery markers and a stop gate, and that the record template contains explicit result states.
- Verification: `npm run live-plan-smoke` PASS; `git diff --check` PASS. No live operation performed.
