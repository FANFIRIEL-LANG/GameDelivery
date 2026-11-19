"""Non-overlapping orchestration for complete browser-backed Playerok snapshots."""
from __future__ import annotations

from typing import Any, Callable

from integrations.playerok.browser_sales import is_fresh_trusted_browser_sales_snapshot


class PlayerokSalesPoller:
    """Schedule full BrowserSalesReader snapshots and persist them through the sync service.

    Timer, reader, session checks and snapshot persistence are injected so this
    coordinator can be tested without Qt, WebEngine, or live Playerok access.
    ``sync_snapshot`` may complete asynchronously; the poller stays busy until
    its callback runs.
    """

    def __init__(
        self,
        *,
        timer: Any,
        reader_factory: Callable[[Callable[[Any | None, str | None], None]], Any],
        sync_snapshot: Callable[[Any, Callable[[Any | None, str | None], None]], None],
        session_available: Callable[[], bool],
        externally_busy: Callable[[], bool] | None = None,
        status_changed: Callable[[str, Any], None] | None = None,
        interval_ms: int = 300_000,
    ):
        self._validate_interval(interval_ms)
        self.timer = timer
        self._reader_factory = reader_factory
        self._sync_snapshot = sync_snapshot
        self._session_available = session_available
        self._externally_busy = externally_busy or (lambda: False)
        self._status_changed = status_changed or (lambda _state, _detail: None)
        self.interval_ms = interval_ms
        self.active = False
        self.busy = False
        self._reader = None
        self._generation = 0
        self._account_user_id: str | None = None
        self.last_successful_result: Any | None = None

        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.poll_now)

    @staticmethod
    def _validate_interval(value: int) -> None:
        if type(value) is not int or value < 1_000:
            raise ValueError("Интервал polling должен быть целым числом не меньше 1000 мс.")

    def set_interval(self, interval_ms: int) -> None:
        self._validate_interval(interval_ms)
        self.interval_ms = interval_ms
        if self.active and not self.busy:
            self._schedule_next()

    def start(self) -> bool:
        if self.active:
            return True
        if not self._available():
            self._notify("session_unavailable", None)
            return False
        self.active = True
        self._notify("started", self.interval_ms)
        self.poll_now()
        return True

    def stop(self, reason: str = "stopped") -> None:
        was_active = self.active or self.busy
        self.active = False
        self._generation += 1
        self.timer.stop()
        reader, self._reader = self._reader, None
        self.busy = False
        if reader is not None:
            try:
                reader.cancel(stage="cancelled")
            except (AttributeError, RuntimeError, TypeError):
                pass
        if was_active:
            self._notify(reason, None)

    def poll_now(self) -> bool:
        if not self.active or self.busy:
            return False
        if not self._available():
            self.stop("session_lost")
            return False
        if self._externally_busy():
            self._notify("busy", None)
            self._schedule_next()
            return False

        self.busy = True
        generation = self._generation
        reader_slot: dict[str, Any] = {"reader": None, "pending": None}

        def completed(snapshot: Any | None, error: str | None) -> None:
            reader = reader_slot["reader"]
            if reader is None:
                reader_slot["pending"] = (snapshot, error)
                return
            self._reader_completed(generation, reader, snapshot, error)

        try:
            reader = self._reader_factory(completed)
        except Exception as exc:
            self._read_failed(generation, None, f"Ошибка запуска чтения ({type(exc).__name__}).")
            return False

        reader_slot["reader"] = reader
        if generation == self._generation and self.active:
            self._reader = reader
        pending = reader_slot["pending"]
        if pending is not None:
            self._reader_completed(generation, reader, *pending)
        return True

    def _available(self) -> bool:
        try:
            return bool(self._session_available())
        except Exception:
            return False

    def _reader_completed(
        self, generation: int, reader: Any, snapshot: Any | None, error: str | None
    ) -> None:
        if generation != self._generation or not self.active:
            return
        if self._reader is reader:
            self._reader = None
        if error or not is_fresh_trusted_browser_sales_snapshot(snapshot):
            self._read_failed(
                generation,
                reader,
                error or "Reader не выдал доверенный полный snapshot.",
            )
            return

        account_id = str(snapshot.account_user_id or "").strip()
        if not account_id:
            self._read_failed(generation, reader, "Snapshot не содержит account ID.")
            return
        if self._account_user_id is not None and account_id != self._account_user_id:
            self.stop("account_changed")
            return
        self._account_user_id = account_id

        def persisted(result: Any | None, sync_error: str | None) -> None:
            self._sync_completed(generation, result, sync_error)

        try:
            self._sync_snapshot(snapshot, persisted)
        except Exception as exc:
            self._sync_completed(generation, None, type(exc).__name__)

    def _read_failed(self, generation: int, reader: Any | None, error: str) -> None:
        if generation != self._generation or not self.active:
            return
        session_lost = bool(getattr(reader, "session_lost", False))
        if session_lost or not self._available():
            self.stop("session_lost")
            return
        self.busy = False
        self._notify("read_error", error)
        self._schedule_next()

    def _sync_completed(self, generation: int, result: Any | None, error: str | None) -> None:
        if generation != self._generation or not self.active:
            return
        self.busy = False
        if error is not None:
            self._notify("sync_error", error)
        else:
            self.last_successful_result = result
            self._notify("success", result)
        self._schedule_next()

    def _schedule_next(self) -> None:
        if self.active and not self.busy:
            self.timer.start(self.interval_ms)

    def _notify(self, state: str, detail: Any) -> None:
        try:
            self._status_changed(state, detail)
        except Exception:
            # UI/reporting errors must not break the polling state machine.
            pass
