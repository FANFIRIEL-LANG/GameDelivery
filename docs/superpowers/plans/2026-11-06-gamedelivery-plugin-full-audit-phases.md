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

### Этап 2 — Python ASF client и сквозной IPC-контракт 🟡 (следующий этап)

**Область:**
- integrations/asf/client.py
- app/main.py (вызовы preview/send и переходы статусов)
- tests/test_asf_client.py
- tests/test_main_delivery_send.py
- tests/test_trade_delivery_ui.py
- ASF 6.3.11.7 IPC /Api/Command contract

**Задача:** доказать, что запросы, сформированные UI, доходят до ASF в правильном формате, ответы корректно разбираются, ошибки транспортного уровня не маскируются и состояние заказа не становится повторно отправляемым после уже состоявшегося dispatch.

- [ ] Проследить точный формат /Api/Command request, ASF Success/Result envelope и тип результата custom command.
- [ ] Проследить preview request: Python JSON → base64url → ASF IPC → args/message → C# DTO → JSON response → Python validation.
- [ ] Проследить send request: одноразовый nonce, secret file, dispatch marker, IPC timeout, response parsing и очистка секрета.
- [ ] Проверить классификацию pre-dispatch errors vs post-dispatch unknown result на всех исключениях.
- [ ] Проверить, что UI не возвращает заказ в retryable status после потенциально состоявшегося trade dispatch.
- [ ] Добавить/исправить точечные regression tests на реальные контракты, не ограничиваться проверкой строк в исходнике.
- [ ] Запустить focused Python tests, затем полный tests workflow.
- [ ] Обновить журнал и зафиксировать точный commit/run IDs.

**Gate:** все пути IPC имеют проверенный request/response contract; ошибка до dispatch отделена от неизвестного результата после dispatch; повторная отправка при неизвестном результате блокируется.

### Этап 3 — Preview pipeline и инвентарь без отправки ⬜

**Область:** GameDeliveryPlugin.cs preview; ASFClient.preview_trade_offer; app/main.py preview; tests.

- [ ] Проверить Steam Trade URL parsing, partner → SteamID64 conversion и обработку повторных query-параметров.
- [ ] Проверить ограничения: один ASF bot на одно предложение, 1–255 assets, canonical positive asset IDs, отсутствие дубликатов.
- [ ] Проверить live GetInventory(753, 6), connection state, точное совпадение Asset IDs и Tradable.
- [ ] Проверить строгую сверку orderId, botName, targetSteamId64 и упорядоченного asset list на каждом boundary.
- [ ] Проверить fail-closed поведение при null inventory, ASF API exception, malformed response и неполном составе.
- [ ] Доказать тестами, что preview path не вызывает SendTradeOffer и не меняет order send status.
- [ ] Запустить .NET smoke/contract tests и Python focused/full suite.

**Gate:** preview корректно подтверждает или отклоняет точный состав, не создавая offer и не меняя delivery state.

### Этап 4 — Send pipeline, защита секретов и восстановление ⬜

**Область:** TradeDelivery.cs; ASFClient.send_trade_offer; app/main.py send; local SQLite delivery state; tests.

- [ ] Проследить одноразовый trade secret end-to-end: генерация nonce, защищённая запись, сравнение order/bot/recipient/assets/token, удаление до вызова ASF send.
- [ ] Проверить ACL на Windows и POSIX, поведение при ошибке chmod/ACL, неполной записи и невозможности удалить файл.
- [ ] Проверить semaphore, idempotency journal, сохранение состояния sending до внешнего вызова и реакцию на повторный вызов.
- [ ] Проверить все точки отказа до и после SendTradeOffer: MissingMethodException, HTTP/Steam error, timeout, пустые offer IDs, success=false, mobile confirmation failure и journal write failure.
- [ ] Проверить, что trade token отсутствует в логах, исключениях, UI, IPC payload, тестовых артефактах и Git.
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
