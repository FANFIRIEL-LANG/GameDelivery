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

## Интеграция торговых площадок

Площадки реализуются через общий контракт `integrations/marketplaces.py` и реестр адаптеров. UI и будущая логика заказов не должны напрямую зависеть от API конкретного маркетплейса; новая платформа добавляется отдельным адаптером.

### Playerok

- Вкладка «Площадки» поддерживает вход по коду из e-mail и дополнительный шаг 2FA.
- Для загрузки активных продаж используется сторонний пакет `PlayerokAPI`. Это **неофициальный клиент**, а не официальный публичный API Playerok; схема и доступность запросов могут изменяться.
- Сессионный токен не сохраняется в SQLite и не выводится в интерфейсе. Приложение использует системное хранилище учётных данных через `keyring`; если оно недоступно, вход обозначается как временный и не обещается сохранённым.
- После входа доступна загрузка активных продаж в режиме чтения (статусы PAID/SENT). Они пока отображаются в таблице площадки отдельно от локальных черновиков GameDelivery: автоматический импорт в рабочую очередь, поиск значка покупателя и автоматическая отправка Steam-обмена ещё не включены.
- Перед реальным использованием проверь клиент Playerok и правила площадки. Сессионный токен даёт доступ к аккаунту, поэтому не копируй его в переписку, логи или GitHub.

## ASF integration

GameDelivery communicates with the locally running ASF IPC interface. See [integrations/asf/README.md](integrations/asf/README.md). The inventory tab has an explicit, on-demand refresh for one already-configured ASF bot using the Steam Community inventory endpoint. It validates the response and saves a per-bot local snapshot only after parsing succeeds. This import path has automated tests, but has not yet been verified against the user's live ASF instance. Full inventory pagination, recipient badge lookup, trade offer construction, and outgoing trade delivery are not implemented.

Keep ASF IPC bound to localhost and protect it with `IPCPassword`. Never commit Steam credentials, ASF passwords, cookies, or session tokens.

## Запуск Windows

Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app
```

