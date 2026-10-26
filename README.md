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
GameDelivery communicates with the locally running ASF IPC interface. See [integrations/asf/README.md](integrations/asf/README.md). The current adapter is a foundation only: it does not yet implement full inventory browsing or outgoing trade delivery. Use the `/swagger` documentation from the installed ASF version to verify endpoint schemas before implementing those features.

Keep ASF IPC bound to localhost and protect it with `IPCPassword`. Never commit Steam credentials, ASF passwords, cookies, or session tokens.

## Запуск Windows
Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app
```

Текущая версия — локальный UI-каркас с SQLite-очередью заказов и базовым клиентом ASF IPC. Playerok пока не подключён, полное чтение инвентаря и реальная отправка предметов ещё не реализованы.
