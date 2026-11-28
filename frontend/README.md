# GameDelivery Desktop Frontend

## Build and package on Windows

From `frontend/`:

```powershell
npm ci
npm run production-smoke
npm run package:win
```

The NSIS installer is written to `dist/GameDelivery-0.1.0-setup.exe`. The unpacked application is under `dist/win-unpacked/`.

## Runtime prerequisites

The current package contains the Electron frontend, compiled Main/Preload and `backend_bridge.py`, but does not bundle Python or the existing GameDelivery Python environment. Install/use the same Python environment required by the repository and set `PYTHON` if `python` is not on `PATH`.

The bridge uses the existing user database location unless `GAMEDELIVERY_DATABASE_PATH` is set. It performs read-only snapshots by default. ASF status/inventory use the existing loopback ASF IPC client; set `ASF_IPC_BASE_URL` only to an approved local ASF endpoint and keep credentials out of renderer configuration.

## Preserve existing data during replacement

1. Close the old and new GameDelivery UI before replacement.
2. Do not delete the existing GameDelivery user data directory, SQLite database, keyring entries, ASF config, ASF data, or Playerok browser profile.
3. Install the new frontend to a separate directory first, or use the NSIS installer with a new/explicit destination.
4. Keep the existing Python/backend environment and configuration unchanged.
5. Set only environment variables required for the local bridge (`PYTHON`, optional database path, optional loopback ASF settings).
6. Start the packaged application and verify the read-only Dashboard/Orders snapshot before any live integration work.

The new Electron frontend does not migrate or overwrite PySide6 state. The PySide6 frontend remains available as the operational fallback.

## Current integration boundary

Connected read-only slices: local bridge health, order snapshots, operator stock counts, ASF status and ASF inventory contract. Playerok browser/session, Telegram binding/sending, code reveal, file delivery, service transitions and all Steam mutations remain explicitly disabled until their backend contracts are verified.

The installer is currently unsigned and requires normal Windows trust/allow-list handling appropriate for a locally built artifact.
