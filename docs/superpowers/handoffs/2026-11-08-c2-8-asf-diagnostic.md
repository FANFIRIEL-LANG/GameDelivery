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

- ASF Generic 6.3.11.7 Release build on .NET 10.0.403: passed, 0 warnings and 0 errors.
- ASF smoke executable against the pinned Generic assemblies: passed.
- `py -3.10 -m unittest discover -s tests -p test_asf_plugin_contract.py`: passed, 38 tests.
- `git diff --check`: passed.
- ASF `GetInventoryAsync` pagination, limiter, and response-error behavior were checked against the [pinned 6.3.11.7 source](https://github.com/JustArchiNET/ArchiSteamFarm/blob/6.3.11.7/ArchiSteamFarm/Steam/Integration/ArchiWebHandler.cs#L2566-L2802).
- Final read-only review found and fixed zero-amount inventory items being counted as unknown types; the smoke test now verifies a separate invalid amount counter.
- Full Python suite on Python 3.10: 223 tests ran, 13 errors. Nine test modules could not import because PySide6 is not installed for Python 3.10; four tests hit Windows temporary SQLite file cleanup locks. The Python 3.14 full discovery emitted repeated SQLite `ResourceWarning`s and hung after test progress, so it was stopped without a final count.
- No Steam session requests were made. No GitHub Actions run exists for this code SHA; the ASF plugin workflow does not auto-trigger on push to `work/local-sync`.

## Known limitations

- Steam profile XML is deprecated. If it is missing, malformed, unavailable, or its `steamID64` is absent or mismatched, recipient badge and inventory requests are skipped. Private badge/inventory responses remain errors; they are never converted into badge level zero.
- The local build and smoke tests verify the code path and host API, not live Steam response availability or privacy behavior.
- Full Python regression results remain constrained by this Windows environment's missing Python 3.10 PySide6 dependency and SQLite temp-file locks.

## Remaining C.2.8 work

Live-check the Steam endpoints on ASF Generic 6.3.11.7 with a connected non-production bot and an approved public test profile. Confirm HTTP/result behavior, profile identity, badge fields, full inventory counts, and exact match/missing/extra/duplicate output. Do not treat endpoint behavior as a production contract until that check succeeds. Do not create or send trade offers.

## Exact next action

When a public test SteamID64 and AppID are selected, run this command in the connected ASF instance:

```text
gamedelivery-diagnose <BotName> <AppID> --recipient <SteamID64>
```

Check that the response contains all three read-only flags, `recipientIdentity.verified=true`, and `responseIdentityVerified=true` for each recipient badge result. Review the exact match, missing, extra, duplicate, foil, and non-tradable counts. If profile verification fails, stop there; the command must not request recipient badge or inventory data. Share only redacted diagnostic JSON.

For local continuation, stay on `work/local-sync`, run the Generic build and smoke commands from `integrations/asf_plugin/README.md`, then rerun the Python contract test. Do not change workflow triggers or protected branches just to obtain CI for this checkpoint.
