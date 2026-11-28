# GameDelivery controlled live acceptance plan

This plan is prepared only. No live operation may be executed from the frontend development task without a separate explicit approval.

## Steam / ASF

- Preconditions: patched ASF Generic runtime is installed; GameDelivery.ASFPlugin version matches; ASF IPC is loopback-only and healthy; test bot has only approved test inventory; recipient SteamID and test order are known; confirmations are enabled according to existing backend policy.
- Scenario: load ASF status and read-only inventory for one bot/context; prepare the existing backend preview for a reserved test order; inspect exact AppID/ContextID/assets; only after separate approval send one controlled Trade Offer through existing ASF plugin path; verify patched trade-offer page contains no ASF signature.
- Expected result: status/inventory are confirmed; preview matches reserved assets; one offer is sent through existing plugin; confirmation lifecycle remains intact; signature text is absent.
- Verification: Steam offer page, ASF logs, GameDelivery order/ledger state and confirmation state; compare offer recipient, assets, order and bot identities.
- Risks: duplicate offer, wrong assets, unknown Steam outcome, stale inventory, unpatched runtime.
- Recovery: stop further dispatch; query existing trade status/reconciliation; do not retry UNKNOWN automatically; manually resolve only after Steam evidence and preserve ledger evidence.

## Playerok

- Preconditions: existing Qt Playerok browser profile/session is backed up and authenticated; Safe Sync/baseline state is known; isolated test listing/deal/chat IDs are recorded; no production listing is selected.
- Scenario: open existing Playerok browser/session using the legacy operational UI; perform read-only refresh and inspect listing/deal/chat linkage; verify Electron frontend continues to show explicit unavailable state until a verified transport is implemented.
- Expected result: session remains intact; deal_id, chat_id and listing_id remain distinct; no message or listing mutation occurs.
- Verification: compare baseline/sync logs and browser-visible IDs; check no outgoing message or listing change.
- Risks: stale baseline, session expiry, accidental message/listing mutation.
- Recovery: stop sync, preserve evidence, restore the backed-up profile/baseline only through existing recovery procedures; do not re-run UNKNOWN as a normal retry.

## Telegram

- Preconditions: test bot and isolated operator account are available; token is stored through existing secure settings; no production chat is selected; separate permission exists for sending a test message.
- Scenario: inspect current settings/runtime state only; verify Electron frontend reports unavailable/not implemented for binding and sending; if separately approved, perform one existing-backend test-message flow, not an invented Electron operation.
- Expected result: no token appears in UI/logs; without approval no message leaves the system; with approval exactly one test message is traceable.
- Verification: backend/outbox evidence and target test chat; inspect logs for redaction.
- Risks: message sent to wrong chat, token disclosure, duplicate notification.
- Recovery: disable notification dispatch, revoke/rotate token if exposed, preserve outbox evidence, do not repeat automatically.

## File delivery

- Preconditions: isolated test file and test order; file catalog integrity hash verified; destination/test recipient approved; backup of catalog/database available; separate permission for actual delivery.
- Scenario: inspect read-only operator summary and file integrity only; verify no delivery button/IPC exists in current frontend; after approval, use existing backend/manual-required flow for one test file.
- Expected result: catalog hash matches; no file is delivered before approval; after approval exactly one controlled delivery is recorded with evidence.
- Verification: catalog row, hash, order ledger, delivery evidence and recipient-side receipt.
- Risks: wrong file, duplicate delivery, corrupted file, irreversible external transfer.
- Recovery: stop dispatch, mark outcome UNKNOWN when evidence is insufficient, reconcile through existing recovery path; never resend solely because a response timed out.

## General stop gate

Stop and ask for explicit approval immediately before any real Trade Offer, Playerok message/listing change, Telegram message, or file delivery. The current frontend remains read-only for these actions.
