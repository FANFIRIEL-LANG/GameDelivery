# GameDelivery — Playerok Safe Sync handoff

**Срез:** 2026-11-17
**Репозиторий:** D:\GameDelivery
**Ветка:** work/local-sync
**Локальный и удалённый HEAD:** 64b16f918ed33d27d0679e8cc1ea327e2a62e101
**Коммиты/push:** не выполнялись.

## 1. Git и происхождение текущего diff

Перед продолжением в доступном выводе git status было **23 изменённых tracked-файла и 5 untracked-файлов**. Это не совпадает с упомянутым ранее отчётом «15 файлов (+801/-247)»; установить соответствие тому снимку без его Git-вывода нельзя. Текущие изменения сохранены без reset/checkout/discard.

После этой работы перед созданием handoff было **25 изменённых tracked-файлов и 7 untracked-файлов**; tracked diff показывал 3,994 вставки и 259 удалений (включая ранее накопленные изменения). Handoff добавляет ещё один untracked-файл. Изменения не закоммичены.

Файлы, уже присутствовавшие в исходном выводе status:

- PROJECT_MASTER_LOG.md, app/main.py, app/platforms_panel.py, app/playerok_browser.py;
- core/database/db.py, core/database/ledger_migrations.py;
- core/delivery/chat_commands.py, core/delivery/dispatcher.py, core/delivery/order_pipeline.py, core/delivery/reservation_service.py;
- docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md;
- integrations/playerok/browser_sales.py, integrations/playerok/client.py, integrations/playerok/sync.py;
- tests/test_app_startup.py, tests/test_delivery_chat_commands_telegram.py, tests/test_delivery_dispatcher.py, tests/test_delivery_reservation_service.py, tests/test_main_delivery_send.py, tests/test_multi_game_order_preflight.py, tests/test_playerok_browser_and_listing_config.py, tests/test_playerok_browser_sales.py, tests/test_trade_delivery_ui.py.

Исходные untracked-файлы: core/delivery/playerok_eligibility.py, core/delivery/playerok_freshness.py, tests/playerok_freshness_fixtures.py, tests/test_playerok_eligibility.py, tests/test_playerok_freshness.py.

В ходе продолжения дополнительно изменены уже грязные app/main.py, app/platforms_panel.py, core/database/ledger_migrations.py, core/delivery/playerok_eligibility.py, integrations/playerok/sync.py, tests/playerok_freshness_fixtures.py, tests/test_playerok_browser_and_listing_config.py, tests/test_trade_delivery_ui.py; впервые изменены tests/test_playerok_read_only_sales.py и tests/test_playerok_sync.py. Добавлены tests/test_playerok_import_guard.py и tests/test_playerok_sync_migration.py. Эти изменения отражают проверяемые правки ниже; остальные файлы из исходного списка относятся к незавершённым предыдущим этапам и сохранены как были.

## 2. Реализованное и проверенное в рабочей копии

- core/database/ledger_migrations.py, core/database/db.py: добавлена Playerok sync migration v2 и отдельная sanitized pre-migration backup; gd_deals перестраивается с nullable listing/config, явным listing_resolution_status, исходной ссылкой и account-scoped unique index. Перед rebuild проверяются конфликты account/deal; миграция останавливается без удаления строк. Неизвестная принадлежность аккаунту или незавершённый baseline переводит запись в historical. Миграция требует соединение без активной транзакции.
- integrations/playerok/client.py: deal_id обязателен; отсутствие item.id сохраняет listing_id=None, slug остаётся отдельным source_listing_ref; chat_id не подменяет ни один идентификатор.
- integrations/playerok/sync.py: browser path принимает только provenance-validated BrowserSalesSnapshot, проверяет список и уникальные deal_id до записей; baseline, сделки и marker сохраняются транзакционно. Отсутствующий listing даёт unresolved запись без заказа. Новые заказы создаются только после baseline, для PAID и подходящей ConfigVersion; количество берётся из delivery_quantity_per_deal. Добавлен метод сопоставления существующей unresolved записи с точным уже зарегистрированным listing ID, не создающий вторую сделку и сохраняющий historical.
- core/delivery/playerok_eligibility.py: legacy Playerok delivery path fail-closed; canonical path требует точный PAID, account, завершённый baseline, разрешённую связь listing/config и подтверждённый non-historical статус. Ошибочная/старая схема не должна подтверждать eligibility.
- app/main.py: импорт требует account ID и уже существующий canonical order; legacy-строка вместо него больше не создаётся/обновляется. Legacy Playerok identity отклоняется. Freshness lookup ограничен account + deal. Старые legacy строки отображаются помеченными как legacy/account unknown, а не дедуплицируются только по внешнему deal ID.
- app/platforms_panel.py: account ID браузерного snapshot передаётся в payload выбранной строки для scoped canonical import.
- Тесты покрывают миграцию v1 и сохранение FK-связей, отказ при account/deal conflict без удаления, account-scoped одинаковые ID, несколько сделок в одном чате, baseline unresolved без orders/lines/legs/reservations, rollback при ошибке записи, idempotency, sticky historical, manual mapping существующей строки, отказ legacy import и отделение slug от item.id.

## 3. Проверки

Подтверждённый изолированный прогон:

python -m unittest tests.test_playerok_sync_migration tests.test_playerok_sync tests.test_playerok_eligibility tests.test_playerok_read_only_sales tests.test_playerok_import_guard -v
Ran 42 tests — OK

После последних небольших изменений миграции и sync повторно выполнены только:

python -m unittest tests.test_playerok_sync_migration tests.test_playerok_sync -v
Ran 17 tests — OK

Подтверждено дополнительно:

- В tests/test_playerok_browser_and_listing_config.py добавлен только импорт Qt из PySide6.QtCore для assertion с Qt.ItemDataRole.UserRole.
- python -m unittest tests.test_playerok_browser_and_listing_config.PlayerokListingConfigTests.test_saved_sales_rows_carry_browser_account_for_scoped_canonical_import -v — 1/1 passed.
- В запуске с неверным именем класса был один unittest.loader._FailedTest. Один из двух UI-тестов Playerok import прошёл; следующий тест был прерван, его результат неизвестен.
- Те UI-тесты создают MainWindow на временной БД. Конструктор выполняет recovery и запускает 15-секундный QTimer. Настройки в тестах подменены MemorySettings, ASF в тесте подготовки подменён. Тем не менее запуск такого UI-теста выходит за безопасную границу чисто изолированного sync-теста; повторять его не следует до отдельного решения.
- Полный набор UI/Playerok tests не запускался.
- После создания handoff выполнены compileall всех изменённых Python-модулей/тестов — exit 0 и git diff --check — exit 0. Миграция на пользовательской production SQLite БД не запускалась; live Playerok/ASF/Steam не проверялись.

## 4. Автоматическая отправка и внешние системы

Read-only чтение QSettings('FANFIRIEL-LANG','GameDelivery') вернуло значение ключа delivery/auto_send_enabled типа str, значение 'false'. DeliverySendPolicy принимает только настоящий bool, поэтому это значение интерпретируется fail-closed как OFF. Настройка пользователя не записывалась; unit-тесты MainWindow используют подменённые настройки.

За время этой проверки не запускались GameDelivery как приложение, live Playerok-запросы, ASF/Steam отправка или dispatcher напрямую. В UI unit-test, однако, создавался MainWindow на временном каталоге, где конструктор запускает recovery и QTimer; результат возможного срабатывания таймера во время прерванного теста отдельно не подтверждён. Не объявлять это отсутствием любого выполнения таймера.

## 5. Открытые риски и незавершённое

1. Другой UI import regression test был прерван; отдельная проверка этого сценария не завершена.
2. Не запускать миграцию на реальной пользовательской БД до review backup/rollback и текущих данных; проверены только временные SQLite fixtures.
3. resolve_unresolved_deal() является API без UI. Корректность зависит от того, что оператор передаст подтверждённый canonical listing ID. Схема пока не хранит provenance такого подтверждения; slug нельзя подставлять.
4. Legacy Playerok записи без account ID остаются неразрешимыми и помечаются/показываются отдельно; автоматически связывать их с аккаунтом нельзя.
5. Реальный DOM-reader не подключён; GraphQL BrowserSalesReader остаётся используемым существующим путём/резервом. Live-контракт не подтверждался.

**Ровно один следующий шаг:** пользовательский review сохранённого uncommitted diff и решение о дальнейшей работе; реализацию новых функций не начинать до этого решения.

## NEXT SESSION INSTRUCTIONS

1. Прочитать актуальный PROJECT_MASTER_LOG.md из GitHub, затем этот handoff.
2. Проверить work/local-sync, HEAD, remote SHA и полный git status; сверить список выше с реальностью.
3. Не повторять уже прошедшие 42/42 и 17/17 тестов без конкретной причины.
4. Сохранить весь незакоммиченный diff; ничего не сбрасывать и не коммитить.
5. Не запускать MainWindow/его recovery timer, dispatcher, worker, scheduler или обработку очереди.
6. Не включать auto-send и не выполнять операции Playerok, ASF, Steam.
7. Действовать только после review единственного следующего шага выше; остальные найденные риски сначала представить пользователю.
