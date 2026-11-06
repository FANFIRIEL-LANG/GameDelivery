# ASF Plugin Command Dispatch and Delivery Audit Plan

> **For agentic workers:** Execute this plan task-by-task with test-first changes. Preserve the branch and delivery-safety constraints below.

**Goal:** Make GameDelivery ASF plugin reliably dispatch custom commands, return actionable diagnostics instead of ASF generic unknown-command response, and close delivery-flow race conditions found during the full audit.

**Architecture:** Keep the existing .NET 10 ASF plugin and existing `IBotCommand2` integration. Isolate command token normalization in a pure helper, verify runtime interface registration during ASF initialization, and contain callback exceptions at the plugin boundary. Keep trade sending through ASF public APIs, but serialize idempotency lookup, live inventory preflight, and sending under one lock.

**Tech Stack:** C# / .NET 10, ASF 6.3.11.7, System.Text.Json, Python unittest, GitHub Actions.

**Spec:** Existing project requirements in `PROJECT_MASTER_LOG.md`, `integrations/asf_plugin/README.md`, and `docs/superpowers/plans/2026-10-27-gamedelivery-completion.md`.

## Global Constraints

- Work only in `feature/asf-bulk-account-import`; never change `main` or merge PR #4.
- Preserve the PySide6 application and existing ASF HTTP/IPC integration.
- Keep all Steam operations inside ASF; no direct Steam Web API.
- Do not create a real trade offer during CI or diagnostics.
- Keep unknown trade outcomes locked against automatic retry.
- Never log or return the trade token, pending secret payload, or base64 command payload.
- Do not ask the user to repeat commands or diagnostics already attempted.

## Review Focus

- ASF command parsing differs between prepared args and raw message — use the first command token, fall back to raw message only when needed, preserve payload, and do not hijack unrelated commands.
- ASF callback throws before its inner diagnostic try — return a structured error rather than silently turning it into "unknown command".
- Plugin loads but does not implement/register the exact runtime `IBotCommand2` interface — expose registration evidence at startup.
- Two concurrent sends target the same asset before inventory refresh — serialize idempotency lookup, live preflight, and send.
- Asset IDs are numerically equal but textually different (for example `001` and `1`) — reject noncanonical duplicates.
- One-time secret deletion fails — abort before the external trade call.

---

### Task 1: Add failing regression contracts

**Files:**
- Modify: `tests/test_asf_plugin_contract.py`

- [x] Add regression checks for command normalization from both ASF `args` and raw `message`.
- [x] Add checks that the callback logs entry, catches unhandled exceptions, and verifies its runtime `IBotCommand2` registration after ASF initialization.
- [x] Add checks that send idempotency lookup and live inventory preflight happen under the same semaphore, with duplicate lookup before inventory access.
- [x] Add checks for canonical numeric asset IDs and fail-closed secret deletion.
- [x] Run the focused test module and confirm the new assertions fail for the current implementation.

### Task 2: Repair custom-command dispatch and observability

**Files:**
- Create: `integrations/asf_plugin/CommandArgumentParser.cs`
- Modify: `integrations/asf_plugin/GameDeliveryPlugin.cs`
- Test: command normalization contracts in `tests/test_asf_plugin_contract.py`

- [x] Normalize the command name from pre-parsed arguments first, then fall back to the raw message.
- [x] Match only the first command token (ASF `Commands.Response` contract), fall back to raw `message` when prepared `args` do not carry the custom command, and preserve all payload tokens. A custom command appearing later inside an unrelated command is not intercepted.
- [x] Strip only the configured ASF command prefix, not arbitrary characters.
- [x] Implement `IASF.OnASFInit` and inspect ASF active plugins to log the number of command handlers and whether this instance is registered as `IBotCommand2`.
- [x] Log each recognized callback command name and argument count; never log payload values.
- [x] Catch unexpected callback exceptions and return a structured failure response; for send exceptions, keep `resultUnknown=true`.
- [x] Bump plugin assembly version to `1.0.2.0` so it is distinguishable from the old installed `1.0.0.0` build and the previous interim artifact.

### Task 3: Harden send ordering and payload validation

**Files:**
- Modify: `integrations/asf_plugin/TradeDelivery.cs`
- Modify: `integrations/asf_plugin/GameDeliveryPlugin.cs`
- Test: `tests/test_asf_plugin_contract.py`

- [x] Move send semaphore ahead of idempotency lookup, current-inventory fetch, asset validation, journal write, and the external trade call.
- [x] Check an existing order record before current inventory access so a repeated completed request returns its stored result even after the asset leaves inventory.
- [x] Validate asset IDs as canonical positive unsigned integers and reject numeric duplicates.
- [x] Verify pending-secret storage permissions and require successful secret deletion before continuing to any trade operation.
- [x] Preserve the pre-send failure vs unknown-after-send distinction and the no-automatic-retry policy.
- [x] Redact the trade token from error diagnostics and logs.

### Task 4: Verify build, packaging, and complete code contract

**Files:**
- Modify: `.github/workflows/asf-plugin.yml`
- Modify: `integrations/asf_plugin/README.md`
- Modify: `tests/test_asf_plugin_contract.py`
- Modify: `PROJECT_MASTER_LOG.md`

- [x] Add an executable .NET smoke test for command argument normalization and runtime plugin interface compatibility against pinned ASF 6.3.11.7 assemblies.
- [x] Build the plugin against the official pinned ASF binaries.
- [x] Verify only intended plugin package files are present and no host-shared assemblies or secrets are included.
- [x] Run the full Python unittest suite and every GitHub Actions workflow triggered by changed paths; wait for terminal conclusions.
- [x] Record commit, run IDs, results, artifact ID, and SHA-256 in the master journal.
- [x] Do not claim live Steam delivery is verified. A user-side installation is only required after the corrected artifact and CI are ready.

## Audit outcome — 2026-11-06

**Confirmed legacy failure chain:** the user's ASF log showed `GameDeliveryPlugin V1.0.0.0`; the previously supplied stack trace named `Newtonsoft.Json, Version=13.0.0.0` at `GameDeliveryPlugin.OnBotCommand`. The old plugin referenced Newtonsoft at runtime. ASF 6.3.11.7 catches exceptions from plugin callbacks in `PluginsCore.OnBotCommand`, returns null, and `Commands.Response` then emits its generic unknown-command response. The apparent unknown command therefore masked a missing runtime assembly inside the old callback. A successful plugin `OnLoaded` log alone did not prove that callback dependencies were available. The user-side log predated installation of the fixed artifact; the user had not yet demonstrated runtime execution of the new version.

**Implemented in the corrected build:**
- System.Text.Json only; no Newtonsoft references in plugin source/project/package.
- `OnASFInit` checks and logs the active plugin count, `IBotCommand2` handler count, and exact GameDelivery instance registration.
- Command token parser follows ASF's actual `Commands.Response` first-token dispatch; it accepts the configured prefix if present and falls back to the raw message. It does not match a GameDelivery command hidden later in an unrelated command's arguments.
- The `OnBotCommand` boundary catches unhandled errors and returns structured diagnostics rather than allowing ASF to turn them into the generic unknown response; callback logs never include encoded payloads.
- Preview/send require canonical positive asset IDs.
- Send serialization lock covers idempotency lookup, live inventory revalidation, journal write and ASF send; duplicate completed orders are handled before inventory access.
- One-time trade secret file must be deleted successfully before the send proceeds; token is redacted from exception diagnostics.
- Version is `1.0.2.0`.
- Added a .NET smoke test using ASF's actual MEF `ContainerConfiguration` + convention and `GetExports<IPlugin>()`; it verifies the plugin is discovered as the exact `IBotCommand2` implementation.

**CI evidence on commit `a8dee8dbe6715da99e951c6856dcf2c933913e9b`:**
- ASF plugin workflow run `42226474922` — success; build against official ASF 6.3.11.7, exact interface smoke, MEF composition, no Newtonsoft references, no host-shared assemblies in package, artifact upload.
- Duplicate PR plugin run `42226466962` — success.
- Python test run `42226474824` — success.
- Duplicate PR test run `42226467254` — success.
- The Windows executable workflow `42226474896` was still running at the time of this journal update; it is not a proof of live ASF plugin runtime and is not required to run the plugin.
- ASF artifact `GameDelivery-ASFPlugin`, ID `13505487431`, SHA-256 `8747f724a1b65f1293a12cef8e1b007ab345a5c163f32083f8852c19f3550477`, built from branch `feature/asf-bulk-account-import`, SHA `a8dee8dbe6715da99e951c6856dcf2c933913e9b`; expires 2026-12-06.
- No real trade offer was created in tests.

**Remaining live verification:** user must replace the previous plugin DLL with the new `1.0.2.0` artifact while ASF is fully stopped, then start ASF and provide the new startup registration line and one response from `gamedelivery-diagnose Ewaylariell`. This is a new verification against a different binary, not a repeat of the old test. Until that happens, live command dispatch and actual Steam trade delivery must remain marked unverified.