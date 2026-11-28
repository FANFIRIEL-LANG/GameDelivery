# Backend integration map — frontend audit

Date: 2026-11-28
Branch: `work/local-sync`

## Current desktop architecture

The existing desktop application is a PySide6 Qt application, not Electron. The entrypoint is `app/__main__.py`; production UI is constructed by `app/main.py::MainWindow`. The legacy frontend remains active and must not be removed during migration.

There is currently no `frontend/`, `package.json`, Electron main process, Vite configuration, React renderer, or TypeScript source in the repository.

## Existing integration boundaries

### ASF

- `integrations/asf/client.py::ASFClient` is the authoritative local ASF IPC client.
- IPC is HTTP localhost, resolved from `IPC.config`; the client enforces loopback and uses `/Api/Command` for plugin commands.
- Existing read operations include ASF status, bot data, bot inventory, bot/global config, trade status, order trade status, recipient diagnostics and bot listing.
- Existing mutating operations include bot start/stop/config updates and guarded GameDelivery commands. React/Electron must call an adapter around this client; it must not call Steam Web API directly.
- `app/asf_management.py` owns current ASF process lifecycle, console input and log signals.

### Playerok

- `integrations/playerok/client.py` provides the marketplace adapter and read-only GraphQL client creation.
- `integrations/playerok/graphql_http.py` contains authenticated read-only GraphQL transport, sales/listing/chat models, pagination and session keyring persistence.
- `integrations/playerok/sync.py` owns durable deal synchronization and Safe Sync rules.
- `app/platforms_panel.py` and `app/playerok_browser.py` own the current Qt browser/session UI.
- `integrations/playerok/browser_chat_listener.py` uses the browser's actual GraphQL WebSocket protocol; no generic backend WebSocket server was found.
- `integrations/playerok/listing_config.py` and `core/delivery/ledger.py` own immutable ConfigVersion persistence.

### Orders and delivery

- `core/orders/service.py::OrderService` lists/reads canonical orders.
- `core/delivery/ledger.py` creates deals, orders, ConfigVersions, delivery attempts and ledger records.
- `core/delivery/order_pipeline.py` prepares Steam Cards, CS2 and typed recipe orders.
- `core/delivery/reservation_service.py`, `listing_stock.py`, `recovery.py` and `reconciliation.py` own reservation/recovery invariants.
- `core/delivery/dispatcher.py` and handler modules own delivery dispatch; frontend must only invoke existing service/UI boundaries.

### Operator modules

- Digital codes: `core/delivery/digital_codes.py`.
- Files: `core/delivery/file_catalog.py`.
- Services: `core/delivery/service_tasks.py`.
- Existing Qt surface: `app/operator_fulfillment_panel.py`.

### Inventory, proxies and Telegram

- ASF inventory UI/controller: `app/inventory_panel.py`, `app/game_inventory_panel.py`, `app/asf_inventory_controller.py`.
- Proxy pool/config: `integrations/asf/proxy_store.py`, `app/proxy_settings.py`, `integrations/openskin/proxy.py` and `app/openskin_proxy_settings.py`.
- OpenSkin: `integrations/openskin/client.py`.
- Telegram settings/runtime/outbox: `app/telegram_settings.py`, `integrations/telegram/outbox.py`.

## Transport findings

- No FastAPI/Flask server or local frontend HTTP API was found in the current source tree.
- No project-wide generic WebSocket backend was found. Playerok's WebSocket is an integration-specific browser protocol.
- The current UI calls Python services directly in the Qt process and uses Qt signals/worker threads for asynchronous operations.
- An Electron frontend therefore requires a new, explicitly designed local bridge/API boundary. It must not invent endpoints or silently bypass existing service contracts.

## Required adapter boundary for the new frontend

The safest first implementation slice is a typed local desktop bridge with explicit `not_implemented` responses for operations that have no existing transport contract. It should wrap existing Python capabilities only after a real transport is selected. The bridge must expose read-only snapshots first; mutating operations remain disabled until their backend command contract is implemented and tested.

## Known gaps / constraints

1. No Electron/React scaffold exists yet.
2. No generic backend HTTP or WebSocket transport exists for a renderer.
3. Existing Qt widgets contain orchestration and must be audited before extraction.
4. Backend capabilities are currently Python callables and ASF IPC commands, not a published JSON schema.
5. The current app persists sensitive values through keyring/Qt settings/backend stores; secrets must not be copied into a renderer store.
6. `PROJECT_MASTER_LOG.md` still records open Stage C acceptance boundaries; frontend work must not claim those business criteria are closed.

## Initial screen mapping

| New screen | Existing source of truth | Initial status |
|---|---|---|
| Orders | `OrderService`, ledger, delivery pipeline, `app/main.py` | available in-process; no renderer transport |
| Playerok | Playerok adapter/GraphQL/browser/sync/listing config | available in-process; browser integration is Qt-specific |
| Operator | digital codes/file/service stores and Qt panel | available in-process; no renderer transport |
| Inventory | ASF IPC client and inventory stores/panels | available through ASF IPC and local stores |
| ASF bots | `ASFClient`, `ASFManagementWidget` | available through local IPC/process control |
| Proxy/prices | proxy stores and OpenSkin client | available in-process; no renderer transport |
| Telegram | Telegram settings/runtime/outbox | available in-process; no renderer transport |

## Audit conclusion

Stage 0 audit is complete at repository/architecture level. Implementation must begin with a non-destructive Electron scaffold and a typed bridge contract, while preserving the existing PySide6 application and avoiding invented backend endpoints.
