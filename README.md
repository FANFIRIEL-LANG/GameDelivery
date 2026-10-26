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

- Адаптер использует собственные HTTPS-запросы на стандартной библиотеке Python `urllib`; сторонний клиент PlayerokAPI из зависимостей удалён.
- У Playerok нет документированного публичного API для разработчиков. Используемые здесь приватные веб-эндпоинты могут измениться или перестать работать. Это собственная реализация протокола, а не официальная интеграция.
- Вкладка «Площадки» поддерживает вход по коду из e-mail и дополнительный шаг 2FA. Токен хранится в системном хранилище учётных данных через `keyring`, а не в SQLite. Если системное хранилище недоступно, сессия остаётся временной только в памяти процесса.
- Разрешены только вход/выход и чтение профиля/списка продаж в статусах PAID/SENT. Код не предоставляет операций вывода денег, покупок, изменения статуса сделки, редактирования/создания товара или отправки сообщения.
- Выбранную сделку можно импортировать в очередь GameDelivery; повторный импорт не создаёт дубликат. Для подготовки оператор вручную указывает Steam trade URL покупателя и выбирает активы ASF; резервы связываются с тем же локальным заказом. Фактическая отправка Steam-обмена отключена.
- Токен сессии даёт доступ к аккаунту. Не копируй его в переписку, логи или GitHub. Перед реальным использованием проверь актуальность эндпоинтов и правила площадки; автоматизированные тесты не заменяют проверку на тестовом аккаунте.

## Запуск Windows

Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app
```
