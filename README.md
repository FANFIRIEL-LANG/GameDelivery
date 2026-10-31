# GameDelivery

Локальная программа для автоматизации заказов игровых предметов.

## Принципы проекта
- Playerok — источник заказов; вход выполняет пользователь в локальном окне приложения.
- **Все операции Steam** — подключение аккаунтов, просмотр инвентаря и торговые операции — интегрируются через ArchiSteamFarm (ASF).
- Steam Web API напрямую не используем.
- Telegram используется только для уведомлений и статистики.
- Отправка Steam-предложения запускается только оператором: повторная проверка живого инвентаря ASF, показ точного состава и явное подтверждение. Неизвестный результат блокирует автоматический повтор. До проверки на тестовом аккаунте интеграция не считается подтверждённой.
- Поддержка игр расширяется отдельными модулями: Steam-карточки, Counter-Strike 2 и будущие игры.

## ASF integration
GameDelivery manages the local ASF process and its IPC connection from the `ASF-боты` tab, including optional auto-start, an embedded console, and a merged list of configured and active bots. See [integrations/asf/README.md](integrations/asf/README.md). The inventory tab has an explicit, on-demand refresh for one ASF bot and stores per-bot snapshots. A separately built plugin provides `gamedelivery-preview` for a no-send live inventory check and `gamedelivery-send` for one explicitly confirmed order. The send path validates the exact card asset IDs against live ASF inventory, uses a one-time local secret file so the trade token is not included in the ASF command, stores a persistent order fingerprint and offer IDs, and blocks an automatic retry when the send result is unknown. This has passed CI compilation/tests where reported, but has not yet been verified against the user's live ASF/Steam accounts; do not treat a CI build as proof of a successful live trade. The recipient must still accept the offer. Build/install the plugin against the ASF version running locally; see `integrations/asf_plugin/README.md`.

Keep ASF IPC bound to localhost and protect it with `IPCPassword`. Never commit Steam credentials, ASF passwords, cookies, or session tokens.

## Интеграция торговых площадок

Площадки реализуются через общий контракт `integrations/marketplaces.py` и реестр адаптеров. UI и будущая логика заказов не должны напрямую зависеть от API конкретного маркетплейса; новая платформа добавляется отдельным адаптером.

### Playerok

- Адаптер использует собственные HTTPS-запросы на стандартной библиотеке Python `urllib`; сторонний клиент PlayerokAPI из зависимостей удалён.
- У Playerok нет документированного публичного API для разработчиков. Используемые здесь приватные веб-эндпоинты могут измениться или перестать работать. Это собственная реализация протокола, а не официальная интеграция.
- Вкладка «Площадки» поддерживает вход по коду из e-mail и дополнительный шаг 2FA. Токен хранится в системном хранилище учётных данных через `keyring`, а не в SQLite. Если системное хранилище недоступно, сессия остаётся временной только в памяти процесса.
- Разрешены только вход/выход и чтение профиля/списка продаж в статусах PAID/SENT. Код не предоставляет операций вывода денег, покупок, изменения статуса сделки, редактирования/создания товара или отправки сообщения.
- Выбранную сделку можно импортировать в очередь GameDelivery; повторный импорт не создаёт дубликат. Оператор вручную указывает Steam trade URL покупателя и выбирает активы ASF; резервирование связывается с тем же локальным заказом. Отправка доступна только отдельной кнопкой после проверки живого состава и явного подтверждения оператора; автоматического принятия входящих обменов нет. Сначала проверь интеграцию на тестовом аккаунте.
- Токен сессии даёт доступ к аккаунту. Не копируй его в переписку, логи или GitHub. Перед реальным использованием проверь актуальность эндпоинтов и правила площадки; автоматизированные тесты не заменяют проверку на тестовом аккаунте.

## Запуск Windows

Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app
```
