# GameDelivery

Локальная программа для автоматизации заказов игровых предметов.

## Принципы проекта
- Playerok — источник заказов; вход выполняет пользователь в локальном окне приложения.
- **Все операции Steam** — подключение аккаунтов, просмотр инвентаря и торговые операции — интегрируются через ArchiSteamFarm (ASF).
- Steam Web API напрямую не используем.
- Telegram используется только для уведомлений и статистики.
- Реальная отправка предметов выключена, пока ASF workflow, резервы, подтверждения и тесты не проверены.
- Поддержка игр расширяется отдельными модулями: Steam-карточки, Counter-Strike 2 и будущие игры.

## ASF integration
GameDelivery communicates with the locally running ASF IPC interface. See [integrations/asf/README.md](integrations/asf/README.md). The inventory tab has an explicit, on-demand refresh for one already-configured ASF bot using the Steam Community inventory endpoint. It validates the response and saves a per-bot local snapshot only after parsing succeeds. This import path has automated tests, but has not yet been verified against the user's live ASF instance. It does not implement full inventory pagination, recipient badge lookup, trade offer construction, or outgoing trade delivery. Use the `/swagger` documentation from the installed ASF version to verify endpoint schemas before enabling further operations.

Keep ASF IPC bound to localhost and protect it with `IPCPassword`. Never commit Steam credentials, ASF passwords, cookies, or session tokens.

## Запуск Windows
Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app
```

Текущая версия — локальный UI-каркас с SQLite-очередью заказов, ручным импортом инвентаря одного ASF-бота в локальный снимок и базовым клиентом ASF IPC. Импорт проверен автоматическими тестами, но ещё не подтверждён на реальном экземпляре ASF пользователя. Playerok не подключён; поиск значка получателя, формирование и отправка реальных обменов не реализованы.
