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

- ASF sends a command with a configured prefix, unusual token order, or raw-message/args mismatch — normalize without losing bot/payload arguments.
- ASF callback throws before its inner diagnostic try — return a structured error rather than silently turning it into "unknown command".
- Plugin loads but does not implement/register the exact runtime `IBotCommand2` interface — expose registration evidence at startup.
- Two concurrent sends target the same asset before inventory refresh — serialize idempotency lookup, live preflight, and send.
- Asset IDs are numerically equal but textually different (for example `001` and `1`) — reject noncanonical duplicates.
- One-time secret deletion fails — abort before the external trade call.

---

### Task 1: Add failing regression contracts

**Files:**
- Modify: `tests/test_asf_plugin_contract.py`

- [ ] Add regression checks for command normalization from both ASF `args` and raw `message`.
- [ ] Add checks that the callback logs entry, catches unhandled exceptions, and verifies its runtime `IBotCommand2` registration after ASF initialization.
- [ ] Add checks that send idempotency lookup and live inventory preflight happen under the same semaphore, with duplicate lookup before inventory access.
- [ ] Add checks for canonical numeric asset IDs and fail-closed secret deletion.
- [ ] Run the focused test module and confirm the new assertions fail for the current implementation.

### Task 2: Repair custom-command dispatch and observability

**Files:**
- Create: `integrations/asf_plugin/CommandArgumentParser.cs`
- Modify: `integrations/asf_plugin/GameDeliveryPlugin.cs`
- Test: command normalization contracts in `tests/test_asf_plugin_contract.py`

- [ ] Normalize the command name from pre-parsed arguments first, then fall back to the raw message.
- [ ] Recognize a command token even if ASF supplies a bot selector before it; move the command token to index zero while preserving the remaining arguments.
- [ ] Strip only the configured ASF command prefix, not arbitrary characters.
- [ ] Implement `IASF.OnASFInit` and inspect ASF active plugins to log the number of command handlers and whether this instance is registered as `IBotCommand2`.
- [ ] Log each recognized callback command name and argument count; never log payload values.
- [ ] Catch unexpected callback exceptions and return a structured failure response; for send exceptions, keep `resultUnknown=true`.
- [ ] Bump plugin assembly version so the installed artifact can be distinguished from previous `1.0.0.0`.

### Task 3: Harden send ordering and payload validation

**Files:**
- Modify: `integrations/asf_plugin/TradeDelivery.cs`
- Modify: `integrations/asf_plugin/GameDeliveryPlugin.cs`
- Test: `tests/test_asf_plugin_contract.py`

- [ ] Move send semaphore ahead of idempotency lookup, current-inventory fetch, asset validation, journal write, and the external trade call.
- [ ] Check an existing order record before current inventory access so a repeated completed request returns its stored result even after the asset leaves inventory.
- [ ] Validate asset IDs as canonical positive unsigned integers and reject numeric duplicates.
- [ ] Verify pending-secret storage permissions and require successful secret deletion before continuing to any trade operation.
- [ ] Preserve the pre-send failure vs unknown-after-send distinction and the no-automatic-retry policy.
- [ ] Redact the trade token from error diagnostics and logs.

### Task 4: Verify build, packaging, and complete code contract

**Files:**
- Modify: `.github/workflows/asf-plugin.yml`
- Modify: `integrations/asf_plugin/README.md`
- Modify: `tests/test_asf_plugin_contract.py`
- Modify: `PROJECT_MASTER_LOG.md`

- [ ] Add an executable .NET smoke test for command argument normalization and runtime plugin interface compatibility against pinned ASF 6.3.11.7 assemblies.
- [ ] Build the plugin against the official pinned ASF binaries.
- [ ] Verify only intended plugin package files are present and no host-shared assemblies or secrets are included.
- [ ] Run the full Python unittest suite and every GitHub Actions workflow triggered by changed paths; wait for terminal conclusions.
- [ ] Record commit, run IDs, results, artifact ID, and SHA-256 in the master journal.
- [ ] Do not claim live Steam delivery is verified. A user-side installation is only required after the corrected artifact and CI are ready.