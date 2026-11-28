# Frontend implementation progress

Date: 2026-11-28
Branch: `work/local-sync`

## Completed

- Stage 0 repository and architecture audit.
- Confirmed current frontend is PySide6 in `app/`, with `app/__main__.py` as entrypoint and `app/main.py::MainWindow` as primary shell.
- Confirmed no Electron/React/Vite/TypeScript scaffold currently exists.
- Mapped actual ASF, Playerok, orders/delivery, operator, inventory, proxy/OpenSkin and Telegram integration boundaries.
- Confirmed no generic FastAPI/Flask frontend API or backend WebSocket transport exists in the repository.
- Documented the transport constraint: do not invent endpoints; introduce an explicit typed bridge before connecting a renderer.

## Changed files

- `docs/frontend/BACKEND_INTEGRATION_MAP.md`
- `docs/frontend/FRONTEND_IMPLEMENTATION_PROGRESS.md`
- `PROJECT_MASTER_LOG.md` (append-only checkpoint pending after this block)

## Verification

- Repository was clean before this audit.
- No business logic, database schema, ASF behavior, Playerok Safe Sync, or live operation was changed.
- No Steam, Playerok, Telegram, file delivery or other external mutation was performed.

## Known issues / decisions

- The target Electron frontend cannot directly call current Python in-process services from a renderer.
- A new local bridge is required. Until a real transport is implemented, unavailable operations must be explicit `not_implemented`, never simulated.
- The old PySide6 frontend remains the operational UI and must remain intact during migration.

## Next concrete step

Implement Stage 1 scaffold in a new `frontend/` directory: package metadata, Vite React TypeScript renderer, Electron main/preload with `contextIsolation: true`, `nodeIntegration: false`, a minimal safe typed bridge, and a non-destructive startup smoke test. Do not connect fake API endpoints or remove the Qt application.

## Stage 1 scaffold checkpoint (2026-11-28)

Created the first non-destructive Electron/Vite/React/TypeScript scaffold in `frontend/`: package metadata, Vite config, strict TypeScript config, renderer entrypoint, secure Electron main/preload entrypoints, and shared capability types. BrowserWindow uses `contextIsolation: true`, `nodeIntegration: false`, and `sandbox: true`. The bridge exposes only `getCapabilities`; backend transport is explicitly `not_implemented`, not simulated. Existing PySide6 and Python backend remain untouched.

Validation still required: install frontend dependencies, run typecheck/build, and add a real Electron startup smoke test. Tailwind, Zustand stores, UI Kit, AppShell, and real backend bridge are not implemented yet.

## Stage 1 implementation checkpoint (2026-11-28)

Implemented and validated the first working renderer/app shell slice. Added Electron/Vite/React/TypeScript package metadata and lockfile, Tailwind/PostCSS configuration, secure main/preload entrypoints, shared capability types, Zustand navigation store, eight-section sidebar, topbar status indicators, module placeholder pages, and collapsible console. All module placeholders are explicitly marked unavailable/not implemented; no fake backend data or mutations are exposed.

Checks: `npm install` passed with 0 vulnerabilities; `npm run typecheck` passed; `npm run build` passed; `git diff --check` passed. Electron process smoke has not yet been run; the current build is a renderer build and the backend bridge remains `not_implemented` until a real transport is implemented.

Next: add an explicit Electron build/start smoke harness, then implement a real local backend bridge without inventing endpoints.

## Stage 4 bridge slice checkpoint (2026-11-28)

Added a minimal real local read-only bridge process (`frontend/backend_bridge.py`) using JSON Lines over stdin/stdout. It performs capability discovery from actual importable Python modules and rejects every other method with `not_implemented`. Electron main now owns the bridge process and routes only `get-capabilities` through the restricted preload API. No credentials, filesystem operations, Steam commands, Playerok mutations, or business writes are exposed.

Validated with the existing renderer smoke, typecheck and production build after the bridge change. Full Electron process startup remains to be verified in a desktop environment.

Bridge verification correction: direct JSONL probe now passes from `frontend/` after adding the repository root to the bridge import path. `{"method":"capabilities"}` returned available states for existing orders, Playerok, ASF, operator, Telegram and OpenSkin modules; an unknown method returned `{"ok":false,"error":"not_implemented"}`. Renderer smoke, typecheck and production build pass after this correction. No Electron window launch or external operation was performed.

## Orders read-only bridge slice

- Added `orders.list` to the Python bridge using existing `OrderService.list_orders()`.
- Added restricted Electron IPC `gamedelivery:list-orders`.
- Renderer now displays confirmed order count from backend snapshot.
- No order mutation, reservation, delivery, Steam, Playerok or Telegram operation exposed.
- Validation: direct bridge probe, unknown-method probe, security smoke, typecheck, production build all pass.

Next: add read-only Dashboard snapshots and typed backend contracts, then start the UI Kit extraction.

### 2026-11-28 — typed bridge and read-only snapshot
Implemented the next renderer block without inventing backend operations. Added shared capability/order contracts and a typed bridge client. Dashboard KPI cards use only actual local bridge responses; Orders displays a read-only table and explicit unavailable/empty states. The Python JSONL bridge accepts parsed JSON requests and keeps unknown methods `not_implemented`.

Verification: `npm run renderer-smoke` PASS; `python -m py_compile backend_bridge.py` PASS; direct JSONL probes for `capabilities`, `orders.list`, and unknown method PASS; `git diff --check` PASS. No commit/push and no live integrations.

### 2026-11-28 — UI kit extraction checkpoint
Added the first reusable UI kit primitives (`Button`, `Card`, `EmptyState`, `StatusIndicator`) and refined the shell status labels to distinguish the working read-only bridge from disabled live actions. The renderer remains honest and mutation-free.

Verification: `npm run renderer-smoke` PASS (security smoke, typecheck, production build); `git diff --check` PASS. Next: move module-specific rendering into feature components and add bridge lifecycle/error tests before connecting additional real read-only backend slices.

### 2026-11-28 — feature components and bridge lifecycle test
Extracted Dashboard, Orders and unavailable-state views into feature components so `App.tsx` remains an orchestration shell. Added `frontend/tests/bridge-smoke.mjs` and the `bridge-smoke` npm script. The test starts the real Python JSONL bridge, exercises capability discovery, read-only order listing, explicit `not_implemented`, and terminates the process.

Verification: renderer smoke PASS; bridge lifecycle/contract smoke PASS; no external operations and no business logic changes. Next: strengthen backend bridge failure/timeout behavior and connect only additional verified read-only backend slices.

### 2026-11-28 — bridge failure handling checkpoint
Hardened Electron main bridge lifecycle and error paths: process startup/exit/error, busy requests, write failures, timeout cleanup, invalid JSON response and shutdown reset now return explicit errors rather than leaving listeners or state behind. Added malformed-request coverage to the real bridge smoke test.

Verification: renderer smoke PASS; bridge lifecycle/contract/error smoke PASS; Python compile PASS; diff check PASS. Next: add real read-only backend slices only after inspecting their exact existing models and failure semantics.

### 2026-11-28 — bridge health checkpoint
Added a local bridge health probe, exposed only through a dedicated typed IPC channel. The renderer now reports the actual Python bridge availability instead of a static connection label. This is process health only and does not claim ASF, Playerok, Telegram or Steam availability.

Verification: renderer smoke PASS; bridge lifecycle/contract/error smoke PASS; Python compile PASS; diff check PASS. Next: inspect and connect the next verified read-only backend model, preserving explicit unavailable/not_implemented states.

### 2026-11-28 — Orders table controls
The Orders feature now supports read-only search, status filtering and sorting by created/updated timestamps using only fields returned by the existing `OrderService.list_orders()` snapshot. Empty and no-match states remain explicit; no order action buttons or lifecycle changes were added.

Verification: renderer smoke PASS; diff check PASS. Next: add focused renderer contract tests or connect the next verified read-only operator/inventory model.

### 2026-11-28 — operator summary checkpoint
Connected a narrow read-only operator summary using existing database tables only. The frontend receives counts for digital-code rows, managed files and service units; it never receives code values, file content, keyring data or mutation capabilities. Missing tables are represented as zero counts by the snapshot query and do not imply inventory availability for delivery.

Verification: direct `operator.summary` probe PASS; renderer smoke PASS; bridge smoke PASS; Python compile PASS. Next: add focused contract coverage for operator.summary and then inspect inventory/ASF read-only adapters.

### 2026-11-28 — operator contract test
Added focused smoke coverage for `operator.summary`, including response success and numeric count fields. The test remains process-level and uses the existing local database path; it does not import, reveal, reserve, deliver or transition any operator item.

Verification: bridge smoke PASS; renderer smoke PASS. Next: inspect existing ASF inventory read-only methods and design an adapter that remains unavailable when ASF credentials/runtime are absent.

### 2026-11-28 — ASF read-only status checkpoint
Connected the first ASF adapter through the existing `ASFClient.get_status()` implementation. The bridge performs a loopback-only read and reports `asf_unavailable` when ASF is not running; it does not claim inventory readiness and does not expose any mutating ASF command.

Current environment has no ASF IPC listener, so the authoritative probe returned `{"ok":false,"error":"asf_unavailable"}`. Verification: renderer smoke PASS; bridge smoke PASS; Python compile PASS. Next: only add inventory snapshot mapping after validating the actual ASF response shape against the existing client and without Steam mutations.

### 2026-11-28 — ASF inventory contract checkpoint
Added a typed, restricted read-only ASF inventory adapter around the existing `ASFClient.get_bot_inventory()` method. The contract explicitly preserves Steam Cards `(AppID 753, ContextID 6)` and CS2 `(AppID 730, ContextID 2)` as separate contexts and rejects other combinations. The current environment still has no ASF IPC listener, so live inventory data remains unavailable until manual runtime setup.

Verification: renderer smoke PASS; bridge smoke PASS with invalid-request coverage; Python compile PASS. No Steam operation or inventory mutation performed. Next: add a guarded UI inventory snapshot flow that requires an explicit bot/context selection and displays unavailable states honestly.

### 2026-11-28 — guarded inventory UI
Added the first Inventory UI flow on top of the verified ASF read-only contract. The operator must explicitly enter a bot name and choose either Steam Cards or CS2 context; the view does not offer item selection or any mutating action. ASF absence remains visible as `asf_unavailable`.

Verification: renderer smoke PASS. No ASF runtime was started and no Steam operation was performed. Next: add UI-level contract tests for context separation and continue with the remaining module adapters.

### 2026-11-28 — inventory context regression coverage
Added regression coverage for the two permitted ASF inventory contexts and an explicit rejection for a mixed Steam Cards/CS2 context. This protects the business separation at the bridge boundary without requiring ASF credentials or performing Steam operations.

Verification: bridge smoke PASS; renderer smoke PASS. Next: continue with Playerok/operator read-only boundaries while keeping live browser and marketplace mutations disabled.

### 2026-11-28 — inventory loading state
Added explicit loading/error lifecycle handling to the guarded inventory view. The snapshot button is disabled during a request, errors are visible, and retry is manual only; the UI never retries an unknown or failed inventory operation automatically.

Verification: renderer smoke PASS. No live ASF runtime or Steam operation used. Next: continue remaining read-only module surfaces and keep all unsupported Playerok/Telegram operations explicitly unavailable.

### 2026-11-28 — ASF and Playerok feature states
Added explicit ASF and Playerok module views. ASF status is read from the existing loopback IPC adapter and is displayed as loading/available/unavailable. Playerok remains explicitly unavailable in the new renderer because its existing browser/session integration is Qt-owned; no fake API or external-browser substitute was introduced.

Verification: renderer smoke PASS. No ASF runtime, Playerok browser session, marketplace mutation or Steam operation was used. Next: continue extracting safe read-only module surfaces and document any backend transport gaps.

### 2026-11-28 — Operator and Telegram boundaries
Added explicit feature states for Operator and Telegram. The UI distinguishes available read-only operator counts from disabled mutations, and documents why Telegram token binding/linking/sending is unavailable until a real transport contract is added. No secrets or external operations are introduced.

Verification: renderer smoke PASS. Next: perform a frontend completion-gap audit against the full specification and add only the highest-value safe missing infrastructure (packaging/startup verification, tests, or documented limitations).

### 2026-11-28 — production Electron layout checkpoint
Added a production compilation path for Electron main/preload and a layout smoke test. The built main process now targets the Vite production renderer, while the existing secure BrowserWindow settings and local Python bridge remain unchanged.

Verification: `npm run production-smoke` PASS (renderer build, Electron TypeScript compilation, production layout assertions). A full desktop Electron launch has not been claimed; no external operation was performed. Next: continue packaging/startup verification and retain explicit limitations where runtime credentials or Qt-owned integrations are required.

### 2026-11-28 — Electron startup checkpoint
Performed the first actual bounded Windows Electron startup smoke against compiled main/preload and production renderer. The process remained alive beyond the startup window and was then terminated by the test harness. This confirms basic desktop launch lifecycle, not live backend or external integration acceptance.

Verification: `npm run electron-startup-smoke` PASS. No ASF runtime, Steam, Playerok, Telegram or file-delivery operation was performed. Next: improve packaging metadata or continue module contracts while keeping external actions disabled.

### 2026-11-28 — Windows packaging checkpoint
Added electron-builder Windows packaging metadata and produced both the unpacked application and an NSIS installer. The package points Electron Main at the compiled production entrypoint and includes `backend_bridge.py`. The installer is unsigned and currently depends on a compatible Python runtime/environment; it is not claimed to be a fully self-contained Python bundle.

Artifacts created locally: `frontend/dist/GameDelivery-0.1.0-setup.exe`, `frontend/dist/GameDelivery-0.1.0-setup.exe.blockmap`, `frontend/dist/win-unpacked/`. Verification: `npm run package:dir` PASS; `npm run package:win` PASS; diff check PASS. No external business operation was performed.

### 2026-11-28 — packaged EXE acceptance checkpoint
Ran the actual unpacked Windows executable generated by electron-builder, not only the source Electron entrypoint. The packaged executable stayed alive during the bounded startup test and was then stopped by the test harness. Recorded SHA-256 hashes for the installer and unpacked EXE for local provenance.

Installer SHA-256: `30F373BB86FDBFE9CF5B65A978CF4DBEE1BB351CBE9F652EE15AC41D3FAB2D27`.
Unpacked EXE SHA-256: `31441F7C2A4CA26FF99054D96F0ED65B59CB07318270244A4D59EB88114A67E7`.

No ASF, Steam, Playerok, Telegram or file-delivery operation was performed. The package still requires a compatible Python runtime/environment and is unsigned.

### 2026-11-28 — installation guide checkpoint
Added the Windows installation/replacement guide for the packaged frontend. It documents the Python prerequisite, environment variables, safe installation alongside the existing PySide6 UI, preservation of database/keyring/ASF/Playerok data, and the current read-only integration boundary.

No files or user data were migrated or deleted. No external operation was performed.

### 2026-11-28 — targeted frontend acceptance regression run
After packaging and installation documentation, reran the relevant frontend acceptance checks. The current state passes production renderer/Electron compilation, bounded Electron startup, bridge lifecycle/error contract, Python bridge compile and Git diff validation. The full project goal remains open because Playerok browser transport, Telegram runtime transport, self-contained Python packaging, signed distribution and live acceptance are not yet verified.

### 2026-11-28 — Electron Remote CI workflow checkpoint
Added a dedicated Windows GitHub Actions workflow for the Electron frontend. It is path-filtered to frontend changes, validates renderer security/type/build, bridge smoke, production layout, then builds/uploads the NSIS installer. The existing Python/Qt Windows workflow also includes a separate unpacked Electron packaging job.

The workflows are present locally but have not been pushed or executed remotely because publication is not currently authorized. Local package:dir, package:win and packaged startup checks already pass.

### 2026-11-28 — preload allowlist regression
Hardened the Electron security smoke test so the restricted preload surface is checked against the exact current channel allowlist. The test also rejects dynamic IPC channel expressions and direct Node runtime access in preload.

Verification: renderer smoke PASS. No backend mutation or external operation performed.

### 2026-11-28 — partial bridge failure isolation
Hardened initial dashboard loading against partial backend bridge failure. Health, orders, capabilities and operator summary are now handled independently; a failure in one read-only slice no longer suppresses successful data from the others, and partial failure remains explicitly visible.

Verification: renderer smoke PASS; diff check PASS. No external operation performed.

### 2026-11-28 — controlled live acceptance plan
Prepared the separate manual acceptance plan required before any live integration work. It covers Steam/ASF signature verification, Playerok session/baseline safety, Telegram token/message safety and file-delivery integrity/recovery. The plan contains a hard stop before real mutations and does not grant operational permission.

### 2026-11-28 — local scope and secret scan
Confirmed the current uncommitted scope and ran a credential-like literal scan across the new frontend/workflow/documentation paths. No embedded secrets were found. The branch remains local and unpublished; live acceptance is still manual and gated.

### 2026-11-28 — packaged bridge path correction
Corrected an important packaging integration issue. External Python cannot reliably read a script located only inside `app.asar`; the bridge script is now copied to `resources/backend_bridge.py` and the packaged Electron Main uses `process.resourcesPath`. Development builds retain the source-relative path.

Verification: `npm run package:dir` PASS; externalized bridge resource exists; packaged EXE startup with the corrected bridge path PASS. No external business operation performed.

### 2026-11-28 — final NSIS rebuild
Rebuilt the Windows installer after correcting the packaged Python bridge resource path. The final installer includes the externalized bridge resource and passed electron-builder packaging. The final installer hash was recorded for provenance.

Installer SHA-256: `6CEA02BE2EF6214E14F35B2C2D64BD355896CDC022BE96093B1C5CB1D50D4298`.

### 2026-11-28 — final packaged EXE regression
Revalidated the unpacked executable after the final NSIS rebuild and externalized bridge correction. The packaged EXE startup smoke passed again. This is desktop lifecycle evidence only and does not constitute live Steam/Playerok/Telegram acceptance.

### 2026-11-28 — Remote CI completed
The verified frontend state was published only to `origin/work/local-sync`. The first Electron CI attempt failed during frontend checks because the Windows runner did not have the Python backend dependencies required by the real bridge smoke. Added Python setup and `pip install -r requirements.txt` to the frontend workflow, then republished.

Final commit: `68fd5732781ec11737befcb46a02f3b2079e035b`.
All mandatory final jobs passed: Electron frontend run `46164663166`, Windows executable/package run `46164663196`, and Tests run `46164663152`. The package and test jobs were inspected at job level and completed successfully.
