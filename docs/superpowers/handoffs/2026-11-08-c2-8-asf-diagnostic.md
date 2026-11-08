# C.2.8 ASF diagnostic handoff

## Last completed substep

Implemented the read-only ASF command path and its pure parsing, identity, inventory aggregation, and exact-match rules. Code checkpoint: `36b1b3ef213d6b6f3d2a333b6b2feef340f361bd` (`feat(asf): add read-only recipient badge diagnostics`), pushed to `origin/work/local-sync`.

The one-argument `gamedelivery-diagnose <BotName>` path remains available. The extended form accepts one or more AppIDs and optional `--recipient <SteamID64>`. Recipient badge and inventory reads are gated on exact identity verification through profile XML; the final badge response URI is also checked for the requested ID and AppID. All diagnostic outputs state `mode=diagnostic-read-only`, `sendEnabled=false`, and `tradeOfferCreated=false`. No change was made to `TradeDelivery.cs`.

## Changed files

- `integrations/asf_plugin/GameDeliveryPlugin.cs`
- `integrations/asf_plugin/ReadOnlyDiagnostic.cs`
- `integrations/asf_plugin/GameDelivery.ASFPlugin.csproj`
- `integrations/asf_plugin/README.md`
- `.gitignore` (ignores only the ASF plugin and smoke project build output directories)
- `tests/asf_plugin_smoke/Program.cs`
- `tests/test_asf_plugin_contract.py`
- `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`
- `docs/superpowers/plans/2026-11-08-c2-8-asf-diagnostic-implementation.md`
- `docs/superpowers/handoffs/2026-11-08-c2-8-asf-diagnostic.md`

## Verification results

- ASF Generic 6.3.11.7 Release build against `D:\ASF-generic` on .NET SDK 10.0.403: passed, 0 warnings and 0 errors. The running ASF process was using .NET runtime 10.0.14.
- ASF smoke executable against `D:\ASF-generic`: passed.
- `py -3.10 -m unittest discover -s tests -p test_asf_plugin_contract.py`: passed, 39 tests, including the new diagnostic-exception fail-closed contract.
- The smoke suite verifies that an async ASF inventory enumeration error propagates instead of being converted into an empty inventory; smoke passed.
- `git diff --check`: passed.
- ASF `GetInventoryAsync` pagination, limiter, and response-error behavior were checked against the [pinned 6.3.11.7 source](https://github.com/JustArchiNET/ArchiSteamFarm/blob/6.3.11.7/ArchiSteamFarm/Steam/Integration/ArchiWebHandler.cs#L2566-L2802).
- Final read-only review found and fixed zero-amount inventory items being counted as unknown types; the smoke test now verifies a separate invalid amount counter.
- Full Python suite on Python 3.10: 223 tests ran, 13 errors. Nine test modules could not import because PySide6 is not installed for Python 3.10; four tests hit Windows temporary SQLite file cleanup locks. The Python 3.14 full discovery emitted repeated SQLite `ResourceWarning`s and hung after test progress, so it was stopped without a final count.
- No Steam session requests were made. GitHub Actions `Tests` run [42437231666](https://github.com/FANFIRIEL-LANG/GameDelivery/actions/runs/42437231666) completed successfully for checkpoint `101a26bec4ac4e39940fb9b6831f2fd85648497d` (then-current branch HEAD; event: `push`; full unit-test job passed on Ubuntu/Python 3.12).
- No ASF plugin workflow run exists for that SHA. `.github/workflows/asf-plugin.yml` has `workflow_dispatch`, but the connected GitHub tools expose only GET/read operations and no dispatch operation. No browser session was created and no workflow trigger was changed.

## Installed ASF read-only inventory

- `D:\ASF-generic\ArchiSteamFarm.dll` is ASF Generic `6.3.11.7`, SHA-256 `3E5F7BB683557F46EF7EC5721CE7402535062F7DB102AA171A4A15AF7CFBC0A8`.
- Old plugin: `D:\ASF-generic\plugins\GameDelivery.ASFPlugin\GameDelivery.ASFPlugin.dll`, version `1.0.2.0`, product commit `a1e23ada74a7437c667f9ab41b693afa0081e3f9`, SHA-256 `108B101D5E9C573C1D4B2C3A70229E1F1393D81FFA306023AA2650D5AE845481`.
- A running ASF Generic process loaded a byte-identical plugin from `%USERPROFILE%\Downloads\ASF-generic\plugins\GameDelivery.ASFPlugin\GameDelivery.ASFPlugin.dll`; its loaded CoreCLR was `.NET 10.0.14`. No ASF process was stopped or restarted, and neither old plugin file was changed.
- New workspace build: version `1.0.2.0`, product commit `101a26bec4ac4e39940fb9b6831f2fd85648497d`, SHA-256 `A3A3068069D1183ABB0F025A5BCF25575946958FACE7FF131B44FE695EC29998`.
- Prepared an isolated Generic 6.3.11.7 runtime under `%LOCALAPPDATA%\Temp\GameDelivery-C28\asf-generic-6.3.11.7-c2-8-test-5abe7f17d8da4e5aae65031658ab0445`. It excludes `config`, `logs`, and prior `plugins`; only the new plugin DLL is staged. It has not been started.

## Known limitations

- Steam profile XML is deprecated. If it is missing, malformed, unavailable, or its `steamID64` is absent or mismatched, recipient badge and inventory requests are skipped. Private badge/inventory responses remain errors; they are never converted into badge level zero.
- The local build and smoke tests verify the code path and host API, not live Steam response availability or privacy behavior. The isolated ASF runtime has not been started; approval is required before that test.
- Full Python regression results remain constrained by this Windows environment's missing Python 3.10 PySide6 dependency and SQLite temp-file locks.
- The successful GitHub `Tests` workflow does not run the ASF Generic plugin build or .NET smoke executable.

## Remaining C.2.8 work

Start the prepared isolated ASF Generic 6.3.11.7 environment only after the user's approval and confirm it loads the staged plugin. Separately, live-check Steam endpoints only after the user provides a connected BotName, AppID, and approved public test SteamID64. Confirm HTTP/result behavior, profile identity, badge fields, full inventory counts, and exact match/missing/extra/duplicate output. Do not treat endpoint behavior as a production contract until that check succeeds. Do not create or send trade offers.

## Exact next action

After approval, launch ASF only from the prepared isolated test directory; do not touch the active instance. For a later live profile check, use the supplied BotName, AppID, and approved public SteamID64:

```text
gamedelivery-diagnose <BotName> <AppID> --recipient <SteamID64>
```

Check that the response contains all three read-only flags, `recipientIdentity.verified=true`, and `responseIdentityVerified=true` for each recipient badge result. Review the exact match, missing, extra, duplicate, foil, and non-tradable counts. If profile verification fails, stop there; the command must not request recipient badge or inventory data. Share only redacted diagnostic JSON.

The GitHub connector cannot dispatch `.github/workflows/asf-plugin.yml`; report that limitation rather than changing triggers or creating a browser session. Do not change protected branches or the active ASF installation.
