# C.2.8 Read-Only ASF Diagnostic Implementation Plan

> **For agentic workers:** Execute inline in the existing `work/local-sync` checkout. Use TDD for each behavior and do not change branches.

**Goal:** Extend `gamedelivery-diagnose` to inspect ASF-session badge metadata and a public recipient's Steam inventory without performing any trade or changing GameDelivery state.

**Architecture:** Keep ASF requests in the existing owner-only command path. Put strict JSON/XML parsing, card aggregation, and exact-hash comparison in small pure helpers that the existing smoke executable can exercise. Consume ASF inventory sequentially through `GetInventoryAsync`; never call the delivery sender from the diagnostic path.

**Tech Stack:** C# / .NET 10.0, ASF Generic 6.3.11.7 host assemblies, System.Text.Json, ASF-provided AngleSharp DOM, existing Python contract tests and ASF smoke executable.

**Spec:** C.2.8 in `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md` and the user's autonomous C.2.8 instruction.

## Global Constraints

- Work only in `work/local-sync`; preserve `main`, both feature branches, and PR #4.
- ASF target is the verified ASF Generic 6.3.11.7 release and .NET 10.0.
- Diagnostics are read-only: no offers, confirmations, inventory mutation, delivery attempts, orders, or database access.
- Return `mode=diagnostic-read-only`, `sendEnabled=false`, and `tradeOfferCreated=false` on success and failure.
- Missing, private, malformed, mismatched, or ambiguous data stays unknown; never coerce it to badge level zero.
- Match only exact `markethash` / `MarketHashName` strings; include non-tradable recipient cards and exclude foil from ordinary-card counts.
- Do not add runtime packages or alter `TradeDelivery.cs`.

## Approaches

- Parse and aggregate in `GameDeliveryPlugin.cs`: fewer files, but expands the existing command class and leaves the data rules difficult to test.
- Add a small pure diagnostic helper and keep ASF I/O in the command: one focused production file, reusable smoke coverage, and a clear read-only call boundary. **Chosen** for testability and containment.
- Add a new unit-test framework: broader test affordances, but it adds dependencies when the existing smoke executable can run the behavior tests against the built plugin. Not chosen.

## Review Focus

- Recipient XML is unavailable or names a different SteamID64: stop recipient badge requests and report identity as unverified.
- Badge response has an unexpected `eresult`, `appid`, `border`, shape, level, or duplicate hashes: report the exact diagnostic error and do not infer a usable set.
- ASF inventory enumeration fails partway through: mark the inventory incomplete and do not report matching as complete.
- Non-tradable normal cards count toward recipient ownership; foil, other apps, and unknown types remain separate.
- Hash comparison is ordinal and exact; report missing, extra, and duplicate values without fuzzy fallback.

## Rulings

- **Ruling:** The badge JSON schema does not include a SteamID64, so verify the recipient first with Steam's read-only profile XML endpoint through ASF and compare its `steamID64` exactly before querying the badge URL. Steam marks the XML format deprecated, so an unavailable or changed XML response leaves the recipient unknown and blocks matching; the cost if this ruling is wrong is a diagnostic that needs a replacement identity source, never data attributed to the wrong account.

---

### Task 1: Pure badge, identity, inventory, and match rules

**Files:**
- Create: `integrations/asf_plugin/ReadOnlyDiagnostic.cs`
- Modify: `tests/asf_plugin_smoke/Program.cs`
- Modify: `tests/test_asf_plugin_contract.py` only where a static boundary assertion is needed

**Interfaces:**
- Produces strict parsing for badge JSON and Steam profile XML identity, an inventory summary over all asynchronously yielded ASF assets, and exact hash-match results.
- No helper in this task performs network, filesystem, database, or trade operations.

- [ ] Add smoke cases for successful and malformed badge payloads, `eresult`, app ID, identity, level, border, empty and duplicate card hashes.
- [ ] Add smoke cases for multiple async inventory pages, normal/foil/unknown types, non-tradable items, amounts, missing/extra hashes, and exact matching.
- [ ] Run the smoke executable against the current plugin and verify the new cases fail because the helper is absent.
- [ ] Implement the smallest pure helper that satisfies the tests; keep raw Steam payloads out of results and logs.
- [ ] Rebuild the plugin against the verified Generic assemblies and run the smoke executable; require all new cases to pass.
- [ ] Commit and push the completed data-rule slice to `origin/work/local-sync`.

### Task 2: ASF-session command orchestration and handoff

**Files:**
- Modify: `integrations/asf_plugin/GameDeliveryPlugin.cs`
- Modify: `integrations/asf_plugin/README.md`
- Modify: `tests/test_asf_plugin_contract.py`
- Modify: `tests/asf_plugin_smoke/Program.cs`
- Modify: `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`

**Interfaces:**
- Preserve legacy `gamedelivery-diagnose <BotName>` behavior.
- Add optional AppID checks and optional `--recipient <SteamID64>` input; make requests sequential through the selected bot's ASF session.
- Verify a recipient's public SteamID64 via the read-only profile XML endpoint before using the numeric profile badge URL. If profile identity cannot be confirmed, skip recipient badge/inventory matching and return unknown.
- Consume `GetInventoryAsync(recipientSteamId64, 753, 6)` fully, including every yielded page, and return aggregates only.

- [ ] Add failing contract/smoke tests for command validation, ASF request choice, error results, and the no-send diagnostic boundary.
- [ ] Wire validated inputs to ASF profile, badge, and inventory session calls; do not touch `TradeDelivery.cs` or application persistence.
- [ ] Run the complete Python unittest suite, compile/build the plugin against ASF Generic 6.3.11.7, and run the ASF smoke executable.
- [ ] Update the Stage C handoff with only verified status; commit and push the completed command slice.
- [ ] Confirm local and remote SHA equality and a clean working tree. Do not claim GitHub Actions success unless a run exists for that exact SHA.
