# GameDelivery C.6.3.5 — handoff

Дата: 2026-11-09  
Рабочая папка: D:\GameDelivery  
Ветка: work/local-sync  
HEAD: 29b36dd1d44245f933346947a1ee9cb85ef6588d  
Commit/push: не выполнялись. Незакоммиченные C.6.3.1–C.6.3.5/test-mode изменения сохранены.

## Текущее состояние

Рабочее дерево намеренно dirty. В него входят предыдущие файлы C.6.3.1–C.6.3.4/test-mode и C.6.3.5:
- modified: PROJECT_MASTER_LOG.md, app/asf_management.py, app/game_inventory_cards.py, app/game_inventory_panel.py, app/inventory_panel.py, docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md, games/cs2/inventory_view.py, games/steam_cards/inventory_views.py, integrations/asf/client.py, tests/test_cs2_inventory_view.py, tests/test_inventory_views.py;
- untracked: app/inventory_display.py, games/inventory_display.py, tests/test_cs2_inventory_panel.py, tests/test_inventory_display.py, tests/test_inventory_panel_display.py, docs/superpowers/checkpoints/C-6-3-4-handoff.md и этот handoff.
Не использовать reset, clean, stash или checkout. Не коммитить/пушить.

## Что изменено в C.6.3.5

- GameItemCard и CS2InventoryGroupCard показывают quality/exterior только когда значение известно; StatTrak остаётся только при подтверждении. На CS2 карточке ASF amount показан отдельно от числа физических assets.
- CS2 variant identity: game_id, appid, context_id, class_id, instance_id, name, item_type, market_hash_name, quality, exterior, rarity, normalized tags. Owner, asset_id, amount, tradable/marketable, restriction metadata и date не входят в ключ варианта и хранятся по физическому экземпляру.
- Статусы передаваемости отображаются счётчиками. Однородно непередаваемые карточки получают более тёмный фон и приглушённое изображение; текст даёт явный статус. Unknown отдельно, rarity border сохранена.
- Transfer filter фильтрует экземпляры внутри группы. Карточки и таблица строятся из одной CS2 проекции; таблица показывает независимый агрегат marketable и tooltip с per-asset restriction details.
- Countdown форматирует только уже типизированный timezone-aware datetime tradable_after. На expiry остаётся статус ASF snapshot; текст требует обновления. Таймер не меняет флаг tradable и не инициирует IPC/trade. Permanent restriction не получает таймер; неизвестный срок показывается как «Срок неизвестен».
- ASF adapter не создаёт tradable_after из полей или текстов ASF. В модели SteamKit2 3.5.0 описаны числовые market restriction flags и item_expiration, но нет типизированного tradable_after. item_expiration и локализованные descriptions не подтверждены как absolute timezone-aware release date. На текущем ASF 6.3.11.7 live payload автоматический countdown поэтому недоступен.
- Цены: в коде и plugin отсутствует price collector. Официальный ISteamEconomy/GetAssetPrices требует user Web API key и адресован товарам Economy app; GetMarketPrices требует publisher key и server-side call. Это не общий публичный Community Market listing feed. Автоматический priceoverview не включён: endpoint не документирован официально, а Steam Subscriber Agreement §4.C запрещает автоматизированное взаимодействие скриптами/ботами. Кэш и тесты его нет, цена не выдумывается и не превращается в ноль.
- Точное поле, разделявшее две live-группы Kilowatt Case, пока неизвестно. Пользователь ответил: сверить на следующей live-сессии. Не утверждать конкретную причину до анализа семи анонимизированных ASF assets/descriptions.

Ссылки: SteamKit2 CEconItem_Description: https://github.com/SteamRE/SteamKit/blob/3.5.0/SteamKit2/SteamKit2/Base/Generated/SteamMsgEcon.cs ; Steamworks ISteamEconomy: https://partner.steamgames.com/doc/webapi/ISteamEconomy?l=english ; Steam Subscriber Agreement: https://store.steampowered.com/subscriber_agreement/?l=english

## Проверки

- python -m unittest tests.test_cs2_inventory_view -v — 29 passed, exit 0.
- python -m unittest tests.test_cs2_inventory_panel -v — 28 passed, exit 0.
- python -m unittest tests.test_inventory_display -v — 8 passed, exit 0.
- python -X faulthandler -m unittest tests.test_inventory_panel_display -v — 5 passed, exit 0.
- python -m compileall -q app/game_inventory_cards.py app/game_inventory_panel.py games/cs2/inventory_view.py tests/test_cs2_inventory_view.py tests/test_cs2_inventory_panel.py — exit 0.
- git diff --check — exit 0, только warnings о нормализации LF/CRLF.
Полный suite не запускался. В ходе TDD один промежуточный ожиданный RED прогон завершился 4 failures + 3 errors, затем реализация и focused tests прошли. Отдельно был один промежуточный fail из-за неверного тестового ожидания marketable; тест скорректирован под три true/false/unknown значения и последующий suite прошёл.

## Не менялось

Production parser/InventoryItem, SQLite/schema/store/migrations, Steam Cards parser, delivery/preflight/trade, ASF plugin/DLL, другие игровые адаптеры. Не запускались Steam/ASF live, Trade Offers или production ASF. Приложение не собиралось.

## Точная следующая задача

1. На следующей live-сессии с отдельным ASF Generic 6.3.11.7 и 730/2 сверить семь обезличенных assets Kilowatt Case, заменив asset IDs на A1–A7. Нужны classid, instanceid, amount, name/type/market_hash_name, tags/quality/rarity, tradable/marketable и имеющиеся restriction fields. Секреты/BotName/IPC credentials не передавать. Проверить, объединяются ли все 7 одной variant identity и сохраняются ли owner/asset/status.
2. Вручную запустить GameDelivery test-mode и проверить quality, status text, muted state, filters и таблицу; пароль вводить только в скрытом диалоге приложения, не в чат.
3. Подтвердить временно-ограниченный предмет с explicit timezone-aware release timestamp, если ASF/test account фактически его выдаёт. При отсутствии такого поля оставить «Срок неизвестен» и не выводить срок из числа дней или локализованного текста.
4. Для цен получить отдельный источник с подтверждённым разрешённым способом доступа; до этого оставить значение недоступным. Не добавлять API key/платный provider без отдельного согласования.

Для проверки изменений Python-сборка не нужна. Safe test-mode launch:
PowerShell: $env:GAMEDELIVERY_TEST_ASF_IPC_URL = 'http://127.0.0.1:1243'; python -m app
После закрытия: Remove-Item Env:GAMEDELIVERY_TEST_ASF_IPC_URL
Секреты вводить только в GUI.

