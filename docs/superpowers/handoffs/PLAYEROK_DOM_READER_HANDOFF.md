# Playerok DOM sales parser handoff

**Date:** 2026-11-17
**Repository:** `D:\GameDelivery`
**Branch / HEAD:** `work/local-sync` / `64b16f918ed33d27d0679e8cc1ea327e2a62e101`
**Commit / push:** none.

## Scope and result

Implemented the isolated parser proposed by the read-only reconnaissance. It is not connected to the existing browser UI, `BrowserSalesReader`, sync service, SQLite, order models, baseline, eligibility, or delivery.

Files added for this step:

- `integrations/playerok/dom_sales.py` — pure standard-library parser for exact `/deal/{UUID}` anchors.
- `tests/fixtures/playerok_sales_observed_two_cards.html` — two anonymized observed-shape cards with synthetic UUID examples and redacted display text.
- `tests/test_playerok_dom_sales_parser.py` — isolated unit tests.
- `docs/superpowers/handoffs/PLAYEROK_DOM_READER_HANDOFF.md` — this handoff.

Updated existing local documents:

- `docs/superpowers/handoffs/PLAYEROK_DOM_CONTRACT.md` — records parser behavior and validation limits.
- `PROJECT_MASTER_LOG.md` — factual implementation/test entry appended; history preserved.

At task start, the worktree had 25 modified tracked files and 9 untracked files. `PROJECT_MASTER_LOG.md` was already modified and `PLAYEROK_DOM_CONTRACT.md` was already untracked. This step added four untracked files (parser, fixture, parser test, and this handoff), bringing the current expected totals to 25 modified tracked files and 13 untracked files; all earlier entries remain present.

## Parser contract

- Input is an HTML string; parsing uses Python's built-in `html.parser` and URL parsing only.
- The parser collects only exact `/deal/{8-4-4-4-12 hex}` anchor paths, either relative or HTTPS on `playerok.com`/`www.playerok.com`.
- Each result is named `candidate_deal_id`; it is **not** a canonical `deal_id`. Repeated UUID-shaped links are de-duplicated while preserving first-seen order.
- `listing_id=None`, `chat_id=None`, and `snapshot_complete=False` are invariants. Card count, scrolling, unknown attributes, or hypothetical page markers cannot change completeness.
- Unknown text/attributes are ignored. The parser exposes no buyer, item, status, price, date, or message text.
- No production module imports or calls this parser.

The fixture uses synthetic identifiers and redacted text. Only the observed card wrapper/link/span shape and the action label are retained. Additional malformed/unknown/duplicate cases are synthetic test inputs, not live observations.

## Verification

Command run with the installed Python 3.14 interpreter:

`python -m unittest tests.test_playerok_dom_sales_parser -v`

Result: **7 tests passed**. `git diff --check` passed. No full suite or unrelated tests ran.

The QSettings key `delivery/auto_send_enabled` was read-only checked and returned `str 'false'`. No settings were written. MainWindow was not launched. No Playerok request or page navigation was performed during implementation; no SQLite writes, order imports, reservations, sync, delivery, dispatcher, worker, ASF, or Steam actions occurred.

## Remaining evidence gaps

The observed deal route UUID has not been independently matched to a trusted `deal.id`. The sales-list page had no confirmed total count or end-of-list marker. The DOM still does not expose confirmed `listing_id` or `chat_id` in the sampled cards. Thus this parser cannot produce a canonical deal record or a complete baseline snapshot.

## Exactly one next step

After review, conduct one separately approved read-only contract-validation investigation that checks whether one `/deal/{UUID}` route candidate matches a trusted canonical `deal.id` and whether the sales DOM exposes a positive end-of-list signal. Keep the parser disconnected from sync, baseline, SQLite, and delivery until that investigation supplies evidence.

## Read-only validation attempt (2026-11-17)

- Branch/HEAD at the check: `work/local-sync` / `64b16f918ed33d27d0679e8cc1ea327e2a62e101`. The existing dirty worktree was preserved; 25 tracked files remained modified and 13 files remained untracked. No commit or push was made.
- The already-running GameDelivery page was inspected in place. It showed `https://playerok.com/500` and Playerok’s generic temporary-error message. No navigation, reload, or retry was performed. This attempt read zero sale cards and did not expose a deal object.
- Canonical relation remains unproven: the two route UUID candidates recorded in the prior inspection were not cross-checked against `deal.id`. Pagination/completeness also remains unproven: the earlier two-card view had no observed explicit next-page/load-more/total-count marker, and reaching the scroll bottom was not a completeness signal. No `hasNextPage`/`endCursor` DOM contract is known.
- Read-only QSettings check returned `delivery/auto_send_enabled` as `str 'false'`; it was not changed. No orders, reservations, SQLite, sync, baseline, dispatcher, worker, scheduler, ASF, or Steam were touched. No tests or compileall ran.
- `git diff --check` was run after these documentation additions. GitHub branch HEAD still matched local HEAD, but GitHub’s Master Log did not yet contain local 2026-11-17 entries. This task explicitly updated local documentation only; no GitHub write occurred.

## Exactly one next step

Wait for an existing Playerok sales page to be available in the same already-open session, then perform one approved read-only comparison of a route candidate with a directly available canonical `deal.id` and inspect only a positive end-of-list/pagination signal. Until both are evidenced, keep the parser disconnected and incomplete.

## Read-only retry attempt (2026-11-17)

- Control point: `work/local-sync` / `64b16f918ed33d27d0679e8cc1ea327e2a62e101`. The existing worktree remained at 25 modified tracked files and 13 untracked files; no prior changes were removed or overwritten. The remote branch SHA matched local HEAD.
- GameDelivery’s existing process was running, but the window was minimized. The UI bridge would not expose the current page state unless the window was activated; the available documented interface did not provide a non-invasive activation/read sequence. The window was not activated. No page navigation, reload, Playerok request, CDP attachment, or retry occurred. The prior `/500` observation was not revalidated.
- No cards or deal objects were read in this attempt. Canonical URL-to-`deal.id` mapping and a positive end-of-list/pagination contract remain unknown. Previous two route candidates and the prior two-card no-paginator observation are not stronger evidence.
- Read-only QSettings returned `delivery/auto_send_enabled` as `str 'false'`; it was not changed. No SQLite, order, reserve, sync, baseline, import, delivery, dispatcher, worker, scheduler, ASF, or Steam operation ran. Tests and compileall were not run.
- Updated local documentation only; no commit or push.

## Exactly one next step

When the same existing GameDelivery window is already accessible without launching or navigating the app, inspect its current sales DOM once for a direct `deal.id` comparison and a positive pagination/end marker. If it is unavailable or shows an error, stop without retrying; keep the parser standalone and incomplete.
