# C.2.7 Pure Multi-Game Allocation Planner Implementation Plan

> **For agentic workers:** Implement this plan task-by-task. Keep the planner disconnected from the production preflight and trade paths.

**Goal:** Implement and test the already-approved C.2.7 deterministic allocation rule as a pure, isolated planner.

**Architecture:** A small standard-library module accepts per-game `AppID`, remaining badge sets `R_g`, available sets `A_g`, and exact order quantity `Q`. It returns a complete deterministic allocation or raises without partial output; it is not imported by UI, order preflight, or trade code.

**Tech Stack:** Python 3.10+, dataclasses, unittest.

**Spec:** `docs/superpowers/plans/2026-11-06-delivery-system-stage-c.md`, sections C.2.2 and C.2.7.

## Global Constraints

- Preserve the approved priority: maximize completed badges; then minimize the sum of `R_g` for completed games; break ties by Steam AppID; allocate remainder by ascending `R_g`, then AppID.
- Allocate exactly `Q`, with `0 <= x_g <= min(A_g, R_g)`; fail closed if exact allocation is impossible.
- Do not change business rules, database, migrations, UI, `order_preflight`, preview, or send behavior.
- Do not contact Steam or use any account/profile data.
- Work only on the user-directed `work/local-sync` branch; do not touch `main` or PR #4.

## Review Focus

- Invalid/non-integer values, including `bool`, are rejected.
- Duplicate or non-positive AppIDs are rejected.
- Games at `R_g=0` or `A_g=0` receive no allocation.
- Infeasible `Q` raises rather than returning a partial plan.
- Equal completion costs and remainder choices are deterministic by AppID.

## Task 1: Isolated pure allocation

**Files:**
- Create: `games/steam_cards/badge_allocation.py`
- Test: `tests/test_badge_allocation.py`

- [x] Write tests for completion priority, minimum completed `R_g`, AppID tie-break, residual allocation order, exact-quantity failure, invalid input, and zero-capacity games.
- [x] Run the focused test file and verify it fails because the planner module/API is missing.
- [x] Implement only the pure input/result dataclasses and deterministic planner required by those tests.
- [x] Run the focused tests and the full existing Python test suite; record environment failures without changing unrelated tests.
- [x] Confirm no operational module imports the planner.
- [x] Update `PROJECT_MASTER_LOG.md` and the C.2 handoff, commit, push, verify local/remote SHA, and record CI for the code/test SHA.

**Completed 2026-11-08:** planner commit `27c36b311fb577887cc93d218a35154068dbb49a`; zero-capacity coverage commit `66f0d6df930078bfc92c27506463575a8a074fe2`. Seven focused tests passed locally. Full-suite and Actions results are recorded in `PROJECT_MASTER_LOG.md` and the C.2 handoff.
