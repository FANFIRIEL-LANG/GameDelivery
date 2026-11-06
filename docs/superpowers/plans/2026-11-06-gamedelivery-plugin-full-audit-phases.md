# GameDelivery ASF Plugin — поэтапный полный аудит и исправление

> **Для исполнителя:** выполнять этапы последовательно; для каждого изменения сначала создать регрессионный тест, увидеть ожидаемое падение, затем внести минимальную правку. После этапа обновить этот план и PROJECT_MASTER_LOG.md. Не повторять уже завершённые этапы без новой причины.

**Goal:** обеспечить проверяемую и безопасную цепочку от команды GameDelivery UI через ASF IPC и плагин до безопасного Steam Trade Offer, устранив текущую маскировку ошибок под «Неизвестная команда!».

**Architecture:** сохранить существующий .NET 10 ASF plugin и официальный ASF IPC. Разделить аудит на командную загрузку/маршрутизацию, Python transport/UI, preview, send safety и live verification. Не переписывать приложение или торговую логику целиком.

**Tech Stack:** C# / .NET 10, ArchiSteamFarm 6.3.11.7, Python, PySide6, SQLite, GitHub Actions, Python unittest, .NET smoke tests.

**Spec:** PROJECT_MASTER_LOG.md; integrations/asf_plugin/README.md; docs/superpowers/plans/2026-11-06-asf-plugin-command-dispatch-audit.md; docs/superpowers/plans/2026-10-27-gamedelivery-completion.md.

## Global Constraints

- Работать только в feature/asf-bulk-account-import.
- Не изменять main, не менять base PR #4 и не сливать PR без отдельного разрешения.
- Сохранять существующие функции приложения и локальные пользовательские данные.
- Steam-вызовы выполнять через ASF; не обходить его прямым Steam Web API.
- Не создавать Trade Offer в CI, unit tests, smoke tests или диагностике.
- При неизвестном исходе отправки не разрешать автоматический повтор.
- Не логировать trade token, nonce, base64 payload, IPC password, maFiles или секреты.
- Не просить пользователя повторять уже введённые команды или повторно присылать уже полученные логи.
- Пользовательские действия запрашивать только после готовности соответствующего артефакта и прохождения автоматических gates.
- CI success не означает, что новая DLL установлена у пользователя, команда исполняется на его ASF или Steam Trade Offer реально создан.

## Review Focus

- DLL загружена, но несовместимость зависимости/интерфейса внутри callback скрывается ASF как общий unknown-command response.
- ASF передаёт prefix-stripped message и предварительно разобранные args; обрабатывается только первый token, а payload сохраняется без изменений.
- MEF export и фактическая регистрация exact IBotCommand2 в текущем процессе должны быть доказаны, а не выведены из одной строки «plugin loaded».
- Python client должен корректно разбирать ASF IPC envelope и вложенный JSON-ответ плагина, различая отказ до dispatch и неопределённый исход после dispatch.
- Preview обязан сверять точные order/bot/recipient/assets и живую передаваемость; он никогда не создаёт offer.
- Send обязан сохранять одноразовость секрета, идемпотентность, атомарный статус заказа и запрет повтора при неопределённом результате.

---

## Зафиксированная база и доказательства

**Дата фиксации:** 2026-11-06  
**Repository:** FANFIRIEL-LANG/GameDelivery  
**Рабочая ветка:** feature/asf-bulk-account-import  
**HEAD:** 95d8ff69a9430de2354e5e5545d50dcc060d65c8  
**PR #4:** open / draft / not merged; head = feature/asf-bulk-account-import; base = feature/steam-card-set-selection. Не менять.

### Подтверждённая причина старого unknown-command случая

1. Старый stack trace содержал FileNotFound для Newtonsoft.Json Version=13.0.0.0 внутри GameDeliveryPlugin.OnBotCommand.
2. В исходниках ASF 6.3.11.7 подтверждена цепочка: Commands.Response → PluginsCore.OnBotCommand → вызов активных IBotCommand2. Исключение callback обрабатывается внутри ASF; при отсутствии ответа ASF возвращает обычное «Неизвестная команда!».
3. Значит, unknown-command response сам по себе не означает, что имя команды ошибочно: он также скрывал падение callback на отсутствующей Newtonsoft dependency.
4. Консоль ASF принимает команду без !; предыдущий совет добавить ! как исправление был неверен.
5. Ранее присланный startup log показывал GameDeliveryPlugin V1.0.0.0. Это не подтверждает загрузку текущей версии 1.0.2.0. Поэтому live callback для текущего бинарника ещё не подтверждён.

### Уже выполненная работа, которую нельзя потерять или делать заново

- Newtonsoft заменён на System.Text.Json; package/source не содержит Newtonsoft runtime dependency.
- Реализована проверка регистрации в OnASFInit: activePlugins, commandHandlers, thisIsIBotCommand2, thisIsRegistered.
- Реализован CommandArgumentParser для первого токена ASF args, fallback на raw message и удаления только настроенного prefix.
- Callback OnBotCommand изолирован try/catch; предусмотрен безопасный лог и структурированный ответ.
- Торговая логика усилена: канонические asset IDs, semaphore на проверку/идемпотентность/отправку, проверка существующей записи до inventory fetch, обязательное удаление одноразового секрета, token redaction.
- Smoke test теперь загружает и вызывает parser из собранной GameDelivery.ASFPlugin.dll через reflection. Ранее smoke проект компилировал parser source отдельно — это было ложным подтверждением теста; добавленный регрессионный Python test обнаружил проблему, затем harness исправлен.
- Полный ASF plugin source уже прочитан: GameDeliveryPlugin.cs, CommandArgumentParser.cs, TradeDelivery.cs, csproj, smoke project, relevant workflow/tests. Полная сквозная проверка Python client и UI пока не завершена.

### Проверенные Actions на HEAD 95d8ff69a9430de2354e5e5545d50dcc060d65c8

| Workflow | Run | Event | Result |
|---|---:|---|---|
| ASF plugin — ASF 6.3.11.7 | 42232166024 | pull_request | success |
| Tests | 42232166023 | pull_request | success |
| Tests | 42232158414 | push | success |
| Build GameDelivery Windows executable | 42232165607 | pull_request | in_progress на момент последней проверки |

Точный ASF plugin artifact с HEAD 95d8ff6:
- Artifact ID: 13508697553
- Name: GameDelivery-ASFPlugin
- SHA-256 digest: 69da2f9c966e84e504ee561c6a14a3a231cfc6923834d57a52612deb0fff7f70
- Workflow run: 42232166024
- Expiration: 2026-12-06
- Artifact ещё не подтверждён как установленный на пользовательском ASF.

Контроль правильной маркировки старых runs на commit 00f7346:
- 42231803632 = ASF plugin push, success, artifact 13509111949, digest aef835db1b83fd208297e73860d31a937697397751b248c4137ed0819c7d9533.
- 42231813264 = ASF plugin pull_request, success, artifact 13510825591, digest 6d4b57cef745bfc7a50d7768829b719f5a8232520b29f8390275090b4d3d1059.
- 42231803346 = Tests push, success.
- 42231813367 = Tests pull_request, success.
Предыдущие записи в журнале перепутали push и pull_request IDs; здесь приведена проверенная привязка.

---

## Этапы выполнения

### Этап 0 — восстановить контекст и зафиксировать Actions ✅

**Область:** branch, PR, Git history, Actions, artifacts, предыдущий план и мастер-журнал.

- [x] Подтвердить актуальный HEAD и рабочую ветку.
- [x] Подтвердить состояние PR #4 (open, draft, not merged).
- [x] Сопоставить текущие и предыдущие plugin/test Actions с их event type, SHA, conclusion и artifact.
- [x] Зафиксировать ранее внесённые изменения и не повторять их.
- [x] Указать в журнале точную следующую задачу.

**Gate:** все будущие исполнители могут восстановить состояние по этому файлу и PROJECT_MASTER_LOG.md, не перечитывая весь чат.

### Этап 1 — ASF загрузка и маршрутизация пользовательских команд ✅ (код и CI)

**Файлы:** integrations/asf_plugin/GameDeliveryPlugin.cs; CommandArgumentParser.cs; GameDelivery.ASFPlugin.csproj; tests/asf_plugin_smoke/*; tests/test_asf_plugin_contract.py; .github/workflows/asf-plugin.yml.

- [x] Устранить Newtonsoft runtime dependency.
- [x] Подтвердить exact ASF 6.3.11.7 interface compatibility и MEF discovery.
- [x] Проверять реальную регистрацию IBotCommand2 при ASF init.
- [x] Разбирать первый ASF command token, prefix и raw message fallback.
- [x] Не допускать необработанного исключения callback, замаскированного ASF под unknown command.
- [x] Smoke test выполняет parser именно из собранной plugin DLL, не source-link копию.
- [x] ASF plugin workflow и smoke test завершились success на HEAD 95d8ff6.

**Ограничение:** runtime на компьютере пользователя ещё не проверен. Установка текущей DLL и одна диагностическая команда относятся к Этапу 5, не к этому этапу.

### Этап 2 — Python ASF client и сквозной IPC-контракт ✅

**Область:**
- integrations/asf/client.py
- app/main.py (вызовы preview/send и переходы статусов)
- tests/test_asf_client.py
- tests/test_main_delivery_send.py
- tests/test_trade_delivery_ui.py
- ASF 6.3.11.7 IPC /Api/Command contract

**Подтверждённый контракт ASF 6.3.11.7:**
- CommandController принимает POST /Api/Command, выбирает Bot.GetDefaultBot(), снимает настроенный CommandPrefix и вызывает targetBot.Commands.Response(EAccess.Owner, command).
- Commands.Response передаёт неизвестные команды в PluginsCore.OnBotCommand; plugin response возвращается как GenericResponse<string> с полем Result.
- ASFClient.request и _unwrap_result корректно извлекают Result (с регистронезависимым поиском Success/Result).
- Python preview строит JSON/base64url payload, не помещает trade token в команду и проверяет order/bot/SteamID64/asset IDs в ответе.
- Python send хранит trade token только в одноразовом защищённом файле, а в ASF передаёт nonce; UI разделяет локальные ошибки до dispatch и ASFError с неопределённым исходом после dispatch.

**Найденный дефект:** если ASF уже получил команду отправки, но ответ содержит malformed offer ID, проверка str.isdigit() могла принять символ вроде superscript ², после чего int() выбрасывал ValueError. app/main.py трактует ValueError из клиента как локальную ошибку до dispatch и возвращает заказ в retryable status. Это небезопасно: ошибка разбора результата после попытки внешней отправки должна блокировать повтор.

- [x] Проследить точный /Api/Command request, ASF Success/Result envelope и custom command result type.
- [x] Проследить preview request: Python JSON → base64url → ASF IPC → args/message → C# DTO → JSON response → Python validation.
- [x] Проследить send request: одноразовый nonce, secret file, dispatch marker, IPC result parsing и очистку секрета.
- [x] Подтвердить опасную классификацию malformed post-dispatch response через regression test.
- [x] TDD RED: новый test_send_trade_offer_treats_unexpected_response_parse_error_after_dispatch_as_unknown упал на ValueError при int("²") на commit 08dc20b4.
- [x] GREEN: integrations/asf/client.py теперь перехватывает непредвиденные исключения после command_dispatched; всё, что не ASFError, превращается в безопасный ASFError с указанием проверить исходящие Steam и не повторять. Локальные ошибки до dispatch остаются ValueError/OSError.
- [x] Проверить, что одноразовый секрет удаляется, trade token не раскрывается, а ошибка после dispatch не может попасть в retryable ветку UI.
- [x] Полный Python suite: push run 42233271868 — success, 281 tests; PR run 42233279698 — success, 281 tests.
- [x] ASF plugin build/smoke: push run 42233271867 — success; PR run 42233279523 — success.
- [x] Записать commit SHA, Actions IDs и артефакты.

**Изменения этапа:** tests/test_asf_client.py; integrations/asf/client.py.
**Исправление:** commit 4e54000509e4811e4e448c4c0a5537b209564aaa.
**Plugin artifacts, собранные с тем же HEAD:**
- push run 42233271867: artifact 13508613685; SHA-256 b44844680e033e527ae532ca31a1232c1a9f47bf71d7c393a024fdf58429d9f9.
- PR run 42233279523: artifact 13510537170; SHA-256 735392ad36aaeccd756b098f09e4dbc1162b5ac1f07b0f0e21df1873bdaeea67.
Оба — installable GameDelivery-ASFPlugin пакеты; production plugin DLL не менялся на этом этапе, поэтому эти артефакты не являются обязательным новым пользовательским обновлением.
**Windows EXE:** PR run 42233279452 оставался in_progress на момент последней проверки; это отдельная упаковочная проверка, не меняющая выводы Python client tests. Следить за ней на этапе 8 / финальной проверке.
**Gate:** тесты доказывают, что unexpected exception после попытки dispatch не возвращает заказ в retryable состояние. Этап 2 завершён; live ASF/Steam не проверен.
**Следующий этап:** Этап 3 — preview pipeline и инвентарь без отправки.
 

### Этап 3 — Preview pipeline и инвентарь без отправки ✅

**Область:** GameDeliveryPlugin.cs preview; ASFClient.preview_trade_offer; app/main.py preview; tests.

**Подтверждённый поток:** UI читает order/recipient/status/assets/reservations; Python клиент валидирует Steam Trade URL, партнёра, token и канонические asset IDs, строит base64url JSON для /Api/Command; плагин проверяет Owner access, bot, connected state, individual Steam recipient, живой инвентарь 753/6, точный Asset ID, context и Tradable; ответ содержит order/bot/SteamID64/asset list, которые повторно сверяются Python-клиентом и UI. Preview path не вызывает SendTradeOffer.

**Найденный дефект:** preview path загружал текущие reservations из БД, но не сверял их с сохранённым составом заказа. Это позволяло запросить и показать preview для состава, который уже не был зарезервирован. Send path эту проверку уже выполнял.

- [x] Добавлен тест test_preview_requires_exact_current_asset_reservations: при удалённой резервации ASF preview не должен вызываться.
- [x] TDD RED: тест на старом коде упал ожидаемым assertion: preview_trade_offer был вызван один раз, хотя не должен. Run 42234242488 (PR) / 42234234970 (push) на SHA 578550fe4237e0d9a2042cccbc70104d1884a70c.
- [x] Исправлена тестовая оснастка (импорт MagicMock; модальный QMessageBox.information заглушён и валидный fake response задан), чтобы тест не зависал и падал именно по production behavior.
- [x] GREEN: app/main.py::_preview_selected_order_with_asf() теперь fail-closed отклоняет preview, если delivery_order_assets не равны steam_asset_reservations. Ошибка показывается до обращения к ASF.
- [x] Полные Tests push run 42234343133 — success, 282 tests; PR run 42234360054 — success, 282 tests.
- [x] ASF plugin push run 42234343234 — success; PR run 42234359777 — success. .NET smoke и точные ASF interfaces/MEF проходили; plugin DLL не менялся в этом этапе.
- [x] Windows EXE package workflow на финальном контрольном HEAD 3235530 завершился success: run 42235272472; Windows ACL test, build, packaged EXE smoke и artifact upload — все success.

**Изменения:** tests/test_trade_delivery_ui.py; app/main.py.
**Commits:** test harness RED source 578550fe4237e0d9a2042cccbc70104d1884a70c; production fix c4816aacb1ab925b61972a50c58f9d3c02bc14ed.
**Артефакты:** ASF plugin code unchanged; no new user plugin installation required from this UI-only fix. Fresh artifact uploads are CI outputs, not a request to reinstall the ASF plugin.
**Gate:** полный Python suite 282/282; preview blocked without exact reservations; no trade offer creation. Этап 3 завершён. Live Steam/ASF preview ещё не проверен.
**Следующий этап:** Этап 4 — аудит send pipeline, одноразового секрета, идемпотентности и recovery.

### Этап 4 — Send pipeline, защита секретов и восстановление ⬜

**Область:** TradeDelivery.cs; ASFClient.send_trade_offer; app/main.py send; local SQLite delivery state; tests.

- [x] Проследить одноразовый trade secret end-to-end: генерация nonce, защищённая запись, сравнение order/bot/recipient/assets/token, удаление до вызова ASF send.
- [ ] Проверить ACL на Windows и POSIX, поведение при ошибке chmod/ACL, неполной записи и невозможности удалить файл.
- [x] Провести code review semaphore, idempotency journal, сохранения состояния sending до внешнего вызова и реакции на повторный вызов; регрессионные тесты остаются частью CI gate.
- [ ] Проверить все точки отказа до и после SendTradeOffer: MissingMethodException, HTTP/Steam error, timeout, пустые offer IDs, success=false, mobile confirmation failure и journal write failure.
- [ ] Завершить проверку отсутствия trade token в логах, исключениях, UI, IPC payload, тестовых артефактах и Git. Исправлено хранение в SQLite и добавлена очистка текста mobile confirmation; полный CI gate ещё не пройден.
- [ ] Проверить обработку известного offer ID, pending mobile confirmation, unknown result и recipient acceptance как разных состояний.
- [ ] Добавить регрессионные тесты для каждой найденной дырки; каждое исправление — отдельный TDD цикл.
- [ ] Запустить focused tests, полный Python suite и ASF plugin workflow.

**Gate:** невозможен автоматический дубль после неопределённого результата; каждый исход однозначно классифицирован либо безопасно заблокирован для ручной сверки.

### Этап 5 — Один runtime smoke на пользовательском ASF ⬜

**Условия входа:** Этапы 2–4 завершены; ASF plugin build и tests зелёные; есть точный артефакт с текущего HEAD.

**Действия пользователя — только один раз после готовности:**
- Остановить ASF полностью и установить DLL из указанного artifact.
- Запустить ASF и проверить строку регистрации с version 1.0.2.0 и thisIsRegistered=True.
- Однократно выполнить read-only gamedelivery-diagnose Ewaylariell.

**Проверяется:** загрузка текущей DLL, exact IBotCommand2 registration, фактический вызов callback, получение инвентаря 753/6, безопасный JSON output.

**Gate:** callback возвращает диагностический JSON и не создаёт торговое предложение. При ошибке пользователь присылает один новый startup/command log; ассистент сначала исследует его и исправляет код без повторения старых команд.

### Этап 6 — Live preview без отправки ⬜

**Условия входа:** Этап 5 подтверждён.

- [ ] Выполнить preview из интерфейса на одном выбранном боте и одном тестовом asset.
- [ ] Сверить имя бота, recipient SteamID64, order ID и asset ID.
- [ ] Убедиться, что исходящие Steam offers не изменились.
- [ ] Зафиксировать результат в заказе/журнале.

**Gate:** фактический live preview соответствует ожидаемому составу; никаких отправок не было.

### Этап 7 — Контролируемая тестовая отправка (только с отдельным разрешением) ⬜

**Условия входа:** все предыдущие этапы зелёные; пользователь отдельно разрешил тестовую отправку.

- [ ] Использовать отдельный тестовый аккаунт и один недорогой tradable asset.
- [ ] Проверить финальное подтверждение в UI: order, bot, recipient, asset IDs.
- [ ] Отправить ровно один offer; проверить Offer ID и mobile confirmation status.
- [ ] Не трактовать offer created как recipient accepted.
- [ ] При timeout/unknown не повторять; сверить исходящие Steam offers и локальный журнал.

**Gate:** зафиксирован подтверждённый исход одной тестовой операции. Не использовать ценные предметы.

### Этап 8 — Финальная проверка и передача результата ⬜

- [ ] Выполнить свежие ASF plugin, Tests и Windows build workflows для финального HEAD.
- [ ] Провести независимый review кода или явно зафиксировать self-review, если отдельный reviewer недоступен.
- [ ] Проверить артефакты, SHA-256, package contents и отсутствие секретов.
- [ ] Обновить README и PROJECT_MASTER_LOG.md фактическими результатами.
- [ ] Оставить PR #4 draft/open; не менять base и не merge без разрешения.

---

## Правило передачи между этапами

После каждого этапа обновлять этот файл и PROJECT_MASTER_LOG.md в том же рабочем цикле:
1. статус этапа и точные выполненные пункты;
2. список изменённых файлов и commit SHA;
3. focused/full test commands и результаты;
4. Actions run IDs, event, head SHA, conclusion;
5. artifact ID + SHA-256, если был сформирован;
6. неустранённые риски и что именно ещё не доказано;
7. один точный следующий шаг.

Новый чат/исполнитель должен сначала сверить HEAD и этот ledger с GitHub. Завершённые этапы не повторять. Если новый коммит появился после последней записи, определить его diff и Actions до любых новых изменений. Нельзя считать этап завершённым по одному лишь наличию кода или успешному workflow.


## Контрольная точка после Этапа 2

**Current HEAD:** 4e54000509e4811e4e448c4c0a5537b209564aaa
**PR #4:** open/draft/not merged.
**Этап 2:** завершён, commit 4e54000509e4811e4e448c4c0a5537b209564aaa; push/PR Tests по 281 тесту — success; ASF plugin push/PR — success.
**Этап 8:** Windows EXE PR run 42233279452 ещё проверялся отдельно.
**Следующий этап:** Этап 3 — добавить тест, который доказывает отказ от preview при несовпадении saved assets и active reservations; затем минимально исправить app/main.py.


## CI checkpoint — 2026-11-06, HEAD 3235530

**Branch:** feature/asf-bulk-account-import
**HEAD:** 3235530d8e8dc41f225969f598295c5661386e36
**PR #4:** open / draft / not merged; base feature/steam-card-set-selection.

| Workflow | Run | Event | Result |
|---|---:|---|---|
| Tests | 42235262460 | push | success — 283 tests |
| Tests | 42235272506 | pull_request | success — 283 tests |
| ASF plugin — ASF 6.3.11.7 | 42235262499 | push | success |
| ASF plugin — ASF 6.3.11.7 | 42235272525 | pull_request | success |
| Build GameDelivery Windows executable | 42235272472 | pull_request | success; ACL test + package build + EXE smoke + upload |

**Artifacts from this exact HEAD:**
- ASF plugin Push: ID 13511300707; SHA-256 8a4142dca2504b420c9b9a4a2a7dab3d83703146b813d01d81b6decd9e9e2b88.
- ASF plugin PR: ID 13509874607; SHA-256 09b0e605683f1408062c35c1a92baf06097423b028440f6616b1fe8f4dfcf269.
- Windows x64 PR: ID 13511305899; SHA-256 a8e96d67884aebbd2ea9b4366c6014cdbf1770f6c62e0f4637c084b0eddbfbcc.
- Artifacts expire 2026-12-06. CI artifact upload is not proof the package was installed on the user's PC.

**Resolved CI regression from HEAD 0b1a73e:** strict offer-ID tests rejected values 0 and ², but expected error text did not match the ASFError returned by the client. The client message now explicitly says to inspect Steam outgoing offers and not retry. On HEAD 3235530 both Push/PR test suites pass. The Windows ACL workflow uses the same focused ASF client test module and now passes too.

**Current task:** all automatic gates are green. Stage 3 remains complete. Next planned stage is Stage 4 — full audit of send pipeline, one-time trade secret, idempotency, state transitions and recovery. Do not start Stage 4 without recording the decision/progress separately; do not ask the user to repeat old commands.

## Stage 4 progress checkpoint — 2026-11-06, HEAD 2de4791

**Current branch:** feature/asf-bulk-account-import  
**HEAD:** 2de47912794a8f1802263ee989264b7c9ea1bc36  
**PR #4:** open / draft / not merged; base feature/steam-card-set-selection.  
**main:** remains at f27f567336cd1249d60cfbfa237314f2b54c0352; no merge performed.

### Findings and changes
- Confirmed plaintext Steam Trade URL tokens were stored in the delivery_orders.recipient_trade_url column and shown in order details.
- Added integrations/asf/trade_credentials.py: stores tokens under per-order keys in the OS credential vault (Windows Credential Manager via keyring); SQLite stores only normalized partner URL.
- Existing order rows migrate on schema open. The token is written and verified in the vault before the SQLite URL is replaced. PRAGMA secure_delete = ON is enabled during migration.
- New send and preview flows restore the full URL only in memory. Terminal outcomes (sent, unknown, cancelled) attempt to delete the per-order vault secret; retryable pre-dispatch failures keep it.
- Added tests for vault storage, legacy migration, missing/unavailable vault, inline-token rejection, SQLite migration/redaction, and cleanup on terminal states.
- ASF mobile confirmation message is now redacted against the exact trade token before it is stored in the idempotency journal or returned to UI.
- Added a smoke assertion that invokes the sanitizer from the built plugin DLL.
- Windows ASF job previously proved the actual plugin ACL routine works while the temporary journal file is opened with FileShare.None. The current sanitizer change still requires a fresh plugin workflow pass.
- Local isolated checks in this review sandbox: 6 trade-credential tests passed; SQLite secure_delete reproduction confirmed the legacy token bytes are absent after update. These checks do not replace GitHub CI.

### CI and blocker
- GitHub Actions runs on the latest HEAD 2de4791 are queued: Tests push 42244623221; Tests PR 42244634009; ASF plugin PR 42244633963; Windows EXE PR 42244633974.
- At the latest repository snapshot, 18 old jobs remained in progress (all stuck on the full unittest step) and 28 runs for this branch were queued. The latest 2de4791 runs had not started; no fresh artifact exists.
- .github/workflows/tests.yml now adds event/ref-scoped concurrency, an 8-minute job timeout, and separate 90/120/180-second timeouts for credential, trade UI, and full-suite tests. The isolated steps will identify which test stalls once a runner becomes available.
- Do not attribute the runner backlog to a confirmed GitHub outage: the official status feed's latest observed snapshot reported Actions operational, but that snapshot was not current enough to explain this repository's queue.

### Remaining before Stage 4 gate
1. Obtain successful fresh Tests push + PR on the exact HEAD, including the UI migration/redaction tests and complete suite.
2. Obtain fresh ASF plugin Push/PR builds and Windows journal ACL + sanitizer smoke success.
3. Obtain a fresh Windows EXE package build on the same final code HEAD and review package contents/hashes.
4. Finish send-failure state-transition review and update this checkpoint with actual run IDs/results.
5. Only then move to Stage 5. No user download or ASF installation request yet.


### 2026-11-06 — ASF JSON reflection resolver runtime blocker

**Live evidence:** on ASF 6.3.11.7, gamedelivery-preview reaches JsonSerializer.Deserialize<PreviewRequest> and fails because reflection-based serialization is disabled. The error handler then tries to serialize its diagnostic response with the same options, causing a second failure. No trade offer was created.

**Scope:** both GameDeliveryPlugin.cs and TradeDelivery.cs configure JsonSerializerOptions without an explicit TypeInfoResolver. The latter contains send request, one-use secret, idempotency journal dictionary and send/error responses, so the defect is not limited to preview.

**Fix under test:** explicitly set TypeInfoResolver = new DefaultJsonTypeInfoResolver() in both existing option sets. This is the explicit resolver configuration named by the runtime exception and preserves the existing DTO/anonymous-response contract. Both plugin and smoke projects explicitly set JsonSerializerIsReflectionEnabledByDefault=false; smoke reads the actual private options from both production classes and runs serialize/deserialize round trips using those options.

**Stage status:** Stage 4 security audit remains in progress; Stage 5 runtime smoke remains pending until this focused smoke and the full ASF workflow pass. Stage 6 live preview remains pending. Stage 7 real send remains unauthorized. No user download or installation until fresh CI artifacts are green.

**Next step:** execute the focused contract test and ASF plugin workflow, inspect any failure logs, then run full Tests and Windows package checks on the exact same branch HEAD.


### 2026-11-07 — Send-pipeline correction checkpoint (tested source HEAD 1066b45)

**Confirmed ASF command route:** ASF 6.3.11.7's IPC `CommandPost` dispatches through its default bot context and `IBotCommand2` is explicitly intended for otherwise unrecognized commands from IPC/console/Steam. The plugin must therefore treat the callback Bot as `ipcContextBot` only and resolve the actual sender from the request's `BotName`. The selected sender is then used for its live inventory and `ArchiWebHandler.SendTradeOffer`.

**Implemented:**
1. The self-trade guard compares two SteamID64 `ulong` values, avoiding the unnecessary SteamKit implicit operator binding.
2. Added an explicit callback immediately before `SendTradeOffer`; pre-dispatch failures are known/retryable, while failures after dispatch are unknown/manual-review only.
3. Logging now separates `ipcContextBot`, `requestedSenderBot`, and `resolvedSenderBot`.
4. Added contract checks and a .NET smoke invoking the compiled plugin guard for equal and unequal IDs.

**Exact HEAD CI:** `1066b45fa77888f5f8c205cd66fb19db228aec4c`:
- Tests #1166 / run 42294601707 — success, 300 tests.
- ASF plugin #539 / run 42294601720 — build success against official ASF 6.3.11.7 and Windows journal ACL smoke success.
- Windows EXE #587 / run 42294601847 — ACL hardening, GUI build, packaged smoke and artifact upload success.
- Plugin artifact 13541805858 SHA-256 `de7934129eebb4315b9dd76878bbdbd5db82b0830924d20f687036d4e95a1de1`.
- Windows artifact 13542210475 SHA-256 `949439ac2a73bd14cc788340ee43f1d9e33528d2a44a5471898df7a275fe6546`.

**Not done:** No live preview/send was performed. The smoke does not yet exercise the complete safe pre-send pipeline with a controlled ASF Bot/inventory seam. Existing UnknownFailure carries exception type/message, MissingMethodException message, TargetSite and stack trace, but structured MissingMemberException.MemberName/ClassName and UI exposure for pre-send errors remain open. The user's actual MissingMethodException root cause is therefore not conclusively identified.

**Next:** add a deterministic safe pre-send harness using exact ASF 6.3.11.7 assemblies without calling Steam send; add structured MissingMemberException metadata for pre-send and send exceptions with token redaction; then request only an installed read-only diagnose/preview. Keep real send disabled until preview is confirmed. PR #4 remains Draft; do not merge or retarget.
