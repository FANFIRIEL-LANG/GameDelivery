# Live acceptance record template

Use this file only while a human operator performs the controlled scenarios in `LIVE_ACCEPTANCE_PLAN.md`. Leave fields blank until the operator has explicit approval and evidence.

## Run metadata

- Date/time (local):
- Operator:
- Frontend commit:
- ASF runtime/plugin version:
- Test order ID:
- Environment/profile backup reference:

## Steam / ASF

- Approval reference:
- Bot:
- Product context (`753/6` or `730/2`):
- Preview evidence:
- Trade Offer ID:
- Signature absent evidence (screenshot/page reference):
- Confirmation evidence:
- Result: `NOT RUN` / `PASS` / `FAIL` / `UNKNOWN`
- Recovery actions:

## Playerok

- Approval reference:
- Browser profile/session reference:
- Listing ID:
- Deal ID:
- Chat ID:
- Baseline/sync evidence:
- Mutation performed: `NO` / `YES (describe and authorize)`
- Result: `NOT RUN` / `PASS` / `FAIL` / `UNKNOWN`
- Recovery actions:

## Telegram

- Approval reference:
- Isolated bot/chat reference (do not record token):
- Binding/runtime evidence:
- Message ID (only if explicitly approved):
- Token exposure observed: `NO` / `YES`
- Result: `NOT RUN` / `PASS` / `FAIL` / `UNKNOWN`
- Recovery actions:

## File delivery

- Approval reference:
- Test file catalog ID:
- Expected SHA-256:
- Integrity verification evidence:
- Test order ID:
- Delivery evidence/receipt:
- Result: `NOT RUN` / `PASS` / `FAIL` / `UNKNOWN`
- Recovery actions:

## Stop conditions

If evidence is incomplete, record `UNKNOWN`, stop retries, preserve logs, and use the existing reconciliation/recovery path. Never record a live acceptance as PASS based only on UI state or an HTTP timeout.
