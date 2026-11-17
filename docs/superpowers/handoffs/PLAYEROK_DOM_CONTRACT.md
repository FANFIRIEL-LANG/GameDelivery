# Playerok DOM contract — read-only reconnaissance

**Observation date:** 2026-11-17
**Repository:** `D:\GameDelivery`
**Branch / HEAD:** `work/local-sync` / `64b16f918ed33d27d0679e8cc1ea327e2a62e101`
**Scope:** existing GameDelivery WebEngine page only; no implementation changes.

## 1. Method and safety

- Reused the already running GameDelivery window, its existing `QWebEngineView`/persistent profile, and the existing Playerok target. No new Playerok view, profile, browser process, login, or CDP target was created.
- Read the rendered DOM with read-only `Runtime.evaluate` calls over the already available loopback CDP endpoint. The evaluations inspected element names, classes, attribute names, route shapes, counts, and boolean text categories; raw buyer names, listing titles, identifiers, e-mail, cookies, tokens, message contents, and full URLs were not saved into this document or Master Log.
- Navigated the existing page to the user profile products route and then to the profile's observed sales link. Those were ordinary same-origin page navigations. No forms, order controls, listing controls, or status-changing actions were used. The visible «Выполните заказ» links were deliberately not clicked.
- The GameDelivery UI still displayed «Playerok: статус входа не проверен», while the existing site page rendered the account profile and its own products/sales sections. No separate auth check or BrowserSalesReader was run.
- Read-only `read-only QSettings lookup` returned `delivery/auto_send_enabled` as `str 'false'`. No setting was written. Dispatcher, worker, scheduler, queue consumer, sync service, SQLite writes, order creation, reservations, ASF, and Steam were not invoked.

## 2. Sales page observations

The existing profile sales page had route shape `/profile/[seller]/sales`. The UI showed tabs «Мои товары», «Покупки», «Продажи» and sales buckets «Выполнение», «Подтверждение», «Завершено», «Возврат средств». «Выполнение» was selected during this observation. This display bucket is not evidence of the exact backend `source_status` for each deal.

Two deal cards were present. Each card was an anchor to `/deal/[36-character UUID-shaped value]`; the two route values were different. The values themselves were not retained. This is a strong DOM candidate for `deal_id`, but no independent `deal.id` value was available for a canonical cross-check, so canonical equivalence remains unproven.

Observed structure, with current generated classes abbreviated:

```text
page: /profile/[seller]/sales
  list wrapper: div.MuiBox-root (generated class)
    deal wrapper: div.mb-10.md:mb-14.MuiBox-root (generated class)
      deal link/card: a.MuiLink-root (generated class), href=/deal/[uuid-shaped value]
        spans: amount-like text, item/offer summary, secondary text, action text
```

The two cards included a currency-bearing amount, item/offer summary, secondary text, a date-like fragment, and an action link labelled «Выполните заказ». The action is not a status field. Its presence must never be treated as permission to click or as proof of a canonical `PAID` value. Date-like text appeared in both cards, but the DOM label/meaning (created time vs another date) was not established; raw date values were omitted.

No `/products/{...}` link, `/chats/{...}` link, `chat_id` attribute, or separate canonical `listing_id` field was found inside these two deal cards. Buyer identity was visible on screen but was not recorded. Thus `chat_id` and `listing_id` are unresolved from the observed sales-card DOM.

At the bottom of the page the DOM still contained two action cards and two distinct `/deal/...` links. The page had a scrollable document (`scrollTop=923`, `scrollHeight=1346`, `clientHeight=423`) and scrolling reached the bottom without revealing additional cards. No «next», numeric page control, «load more», or total deal count was found. This establishes only the end of the currently rendered «Выполнение» view; it does **not** prove completeness of all account sales or other status buckets.

## 3. Listings page observations

The existing products page route shape was `/profile/[seller]/products`. The profile UI showed «Активные» and «Завершённые» tabs, with «Активные» selected, and visible listing cards whose displayed prices used the ruble symbol. Individual card links included slug routes of the forms `/products/[slug]` and `/profile/[seller]/products/[slug]` in the sampled DOM. The raw slugs and listing text were not retained.

Observed structure:

```text
page: /profile/[seller]/products
  listing link: a.MuiLink-root (generated class), href=.../products/[slug]
  card-like ancestor: div.MuiBox-root (generated class), with data-id on some ancestors
    descendants: currency-bearing span, secondary span, title/summary link
```

The sampled link anchors had `class` and `href`, but no semantic `data-*` attribute. Three sampled card ancestors carried `data-id`; each value was 39 characters, not a 24-hex string or UUID, and did not equal or prefix-match the corresponding sampled URL slug. The attribute’s semantics were not proven; do not map it to `listing_id`.

A profile heading displayed a total of 35 listings at the time of observation. The DOM query found 33 links matching `/products/` (including the profile products route itself) and 16 `[data-id]` card-like elements. These counts do not reconcile to a complete item set. The page had a scrollable document (height greater than viewport), but the listing page could not be safely reloaded after returning from sales: it rendered Playerok’s generic error page. No retry was made. Therefore the observed active listing cards are examples only; pagination, lazy loading, virtualization, the finished tab, and end-of-list behavior were not confirmed.

No canonical listing ID was independently confirmed. A URL slug remains a separate `source_listing_ref`; it must never be substituted for `listing_id`.

## 4. Evidence and reliability table

| Field | Found | DOM source | Canonical? | Reliability |
|---|---|---|---|---|
| Deal route identifier | Two distinct 36-character UUID-shaped values | `a[href^="/deal/"]` on two deal cards | Not independently matched to `deal.id` | Candidate only |
| Deal status | Current bucket label «Выполнение» | Selected sales bucket/tab | No per-deal source status confirmed | Bucket-level only |
| Deal amount | Currency-bearing text | Descendant `span` in deal card | No unit/total semantics confirmed | Display-only |
| Deal date | Date-like fragment on both cards | Descendant text in deal wrapper | Meaning not confirmed | Unresolved |
| `listing_id` in sale card | Not found | No product link or semantic listing field in sampled cards | No | Unavailable |
| `chat_id` in sale card | Not found | No chat link/attribute in sampled cards | No | Unavailable |
| Listing slug | Present in product links | `a[href*="/products/"]` | No | Reference only |
| Listing `data-id` | Present on some ancestors | `[data-id]` around sampled product links | Meaning not confirmed; not slug | Candidate only |
| Listing title | Visible as card text | Descendant link/text | Not canonical | Display-only; raw values omitted |
| Listing price/currency | Ruble currency visible | Currency-bearing descendant span | Display amount observed; selector is generated-class based | Display-only |
| Listing status | Active/completed tabs | Profile products page tabs | Per-listing status not separately confirmed | Bucket-level only |
| Full sales/listings completeness | Not proven | No total count/pagination marker; listing reload errored | No | Must fail closed |

The observed `Mui*` and `mui-*` CSS classes include generated Emotion-style names and varied between elements. Treat them as unstable presentation details, not a safe durable selector contract. `data-id` is also not usable as an ID contract until its meaning is independently established.

## 5. Future normalizer contract (not implemented)

A safe reader should distinguish page completeness from field resolution and should fail closed:

```text
SalesSnapshot {
  complete: bool,              # false unless a positive end-of-list proof exists
  status_bucket: str | null,   # observed UI bucket; not automatically source_status
  deals: [
    {
      deal_id: str,             # required; reject row until route value is proven canonical
      chat_id: str | null,      # optional; never infer from deal_id
      listing_id: str | null,   # null until canonical ID is observed
      source_listing_ref: str | null,  # slug kept separately
      source_status: str | null,       # unknown unless explicit per-deal value is observed
      created_at: str | null,   # only after date semantics are confirmed
    }
  ]
}

ListingsSnapshot {
  complete: bool,
  listings: [
    {
      listing_id: str | null,  # never fill from slug or unverified data-id
      source_listing_ref: str | null,
      title: str | null,
      price_amount: str | null,
      currency: str | null,
      status: str | null,
    }
  ]
}
```

The current DOM does not justify baseline completion, canonical deal matching, listing resolution, or creation of orders. An unresolved `listing_id` must remain unresolved. A partial snapshot must never complete baseline.

## 6. Original next step

The reconnaissance proposed a read-only fixture-driven parser for the observed sales list. That isolated parser has now been implemented; see Section 8. It extracts only provisional `/deal/{UUID}` route candidates, leaves `chat_id` and `listing_id` null, and cannot report a complete snapshot.
## 7. Stop condition and final state

Returning to the products page after the sales inspection produced Playerok’s generic error page. The inspection stopped there; no retry or additional request was made. No code, tests, database, configuration, listings, deals, messages, or delivery state was changed.

## 8. Isolated fixture-driven parser (2026-11-17)

- Added `integrations/playerok/dom_sales.py`, a pure standard-library parser. It imports no Qt/WebEngine, browser, network, GraphQL, or SQLite code and is not imported by the application, sync, or delivery modules.
- `parse_playerok_sales_dom(html)` inspects anchor `href` values only. It accepts the exact observed `/deal/{UUID}` path shape (relative or HTTPS on Playerok), rejects malformed IDs and external origins, and de-duplicates repeated UUID-shaped links in first-seen order.
- Results use `candidate_deal_id`; there is intentionally no `deal_id` property. `listing_id` and `chat_id` are always `None`. `snapshot_complete` has type `Literal[False]` and remains false even if HTML contains hypothetical counts, end markers, or only two cards. No status, title, buyer, amount, date, or other identifier is normalized.
- Added `tests/fixtures/playerok_sales_observed_two_cards.html`: two sanitized card structures based on the observed wrapper, link, and span elements. Route IDs are synthetic format examples; displayed text is redacted. Malformed/missing IDs, duplicate links, and unknown attributes are generated only in negative tests and are not claimed to have been observed live.
- Added `tests/test_playerok_dom_sales_parser.py`. The targeted Python 3.14 unittest run passed **7/7**: two candidates, unknown fields ignored, missing/malformed IDs rejected, duplicate links deduplicated, incomplete snapshot enforced, same-origin absolute URL accepted/external rejected, and non-string input rejected.
- `delivery/auto_send_enabled` was read without writing and remains the string `'false'`; no application setting changed. No MainWindow, live Playerok page, sync, SQLite write, order processing, reservation, dispatcher, worker, ASF, or Steam ran in this implementation step. No commit or push.
- The existing status before this task was 25 modified tracked files and 9 untracked files. This task adds 4 untracked files (parser, fixture, test, and this implementation handoff) and updates the already-dirty Master Log and DOM contract; all prior changes remain present.

### Current next step

Perform one separately approved read-only contract-validation investigation to establish whether a `/deal/{UUID}` route candidate equals a trusted canonical `deal.id` and whether Playerok exposes a positive end-of-list signal. Keep this parser standalone until that evidence exists; do not connect it to baseline or sync.

## 9. Follow-up validation attempt (2026-11-17)

The attempted read-only validation used only the already-running GameDelivery window and its current embedded page. The address field showed `https://playerok.com/500`; Playerok displayed its generic temporary-error page. No navigation, reload, retry, new browser/profile, or request was initiated. The current page therefore supplied **zero sales cards** and no deal object to inspect.

- **Route UUID versus canonical `deal.id`: unresolved.** The earlier two UUID-shaped `/deal/{...}` links remain candidates only. No independent `deal.id` was available in this attempt, so there is no direct match or mismatch to report.
- **End-of-list/pagination: unconfirmed.** The current error page exposes no useful sales-list evidence. The earlier sampled «Выполнение» DOM had two cards and no visible next/page/load-more control or total count; scrolling to the visual bottom was not proof of completeness. No DOM `hasNextPage`/`endCursor` contract has been confirmed.
- The panel’s separate status text said that browser login had not been checked. No request was made to verify it; this UI status and the Playerok error page do not prove whether the persistent session is currently authenticated.

Keep `candidate_deal_id` provisional and `snapshot_complete=false`. Resume only when an already-open page naturally provides the needed data under a separately authorized read-only check; do not infer either contract from card count or scrolling.

## 10. Follow-up attempt with minimized application (2026-11-17)

The existing GameDelivery process was still running, but its window was minimized. The available UI bridge reported that the window must be activated before its state can be read; no documented safe action was used to activate it. No DOM content was obtained in this attempt, and no Playerok request, navigation, reload, CDP connection, or retry was made. The previous `/500` observation is not asserted as the current page state because it was not re-read.

This attempt yielded zero inspected sales cards and no deal object. Thus there is still no direct comparison between a `/deal/{UUID}` route value and `deal.id`. The earlier two UUID-shaped links remain provisional candidates. No current list DOM was available to inspect for pagination; the earlier lack of visible page controls/count and scroll-to-bottom observation still does not prove completeness. No `hasNextPage`/`endCursor` or equivalent positive end marker is confirmed.

The parser contract remains unchanged: do not promote `candidate_deal_id` to canonical `deal_id`, and keep `snapshot_complete=false` until direct identity and positive completeness evidence exist.
