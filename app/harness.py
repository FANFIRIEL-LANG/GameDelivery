"""Isolated Qt shell for packaging/startup diagnostics.

This module deliberately does not import MainWindow, QSettings, database
migrations, browser code, or any network/Steam/ASF integration.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class HarnessIsolationError(RuntimeError):
    """Raised when a harness path would escape its private temporary root."""


@dataclass(frozen=True)
class HarnessRuntime:
    """Explicit paths owned by one harness process; no home-directory fallback."""

    root: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "HarnessRuntime":
        resolved = Path(root).resolve()
        if not resolved.is_absolute():
            raise HarnessIsolationError("Harness root must be absolute")
        resolved.mkdir(parents=True, exist_ok=True)
        return cls(root=resolved)

    def path(self, *parts: str) -> Path:
        if not parts or any(not isinstance(part, str) or not part for part in parts):
            raise HarnessIsolationError("Harness path requires non-empty relative components")
        candidate = (self.root.joinpath(*parts)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise HarnessIsolationError("Harness path escapes its isolated root") from exc
        return candidate

    @property
    def database_path(self) -> Path:
        return self.path("data", "harness.sqlite3")

    def initialize_synthetic_database(self) -> None:
        database_path = self.database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again after creating parents to reject a redirected path.
        if self.path("data", "harness.sqlite3") != database_path:
            raise HarnessIsolationError("Harness database path changed during setup")
        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS harness_probe "
                "(probe_key TEXT PRIMARY KEY, probe_value TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()


_DIAGNOSTIC_PAGES = (
    ("Заказы", "Хранилище изолировано. Пользовательские заказы не загружались."),
    ("Площадки", "Playerok отключён: WebEngine и сетевые клиенты не создаются."),
    ("Инвентарь", "Интеграции Steam/ASF отключены. Данные пользователя не загружались."),
    ("Игровые предметы", "Сетевые цены и фоновые обновления отключены."),
    ("Модули игр", "Диагностическая заглушка; игровые действия недоступны."),
    ("Журнал", "Harness Mode не подключает пользовательские файлы журналов."),
    ("Настройки", "Пользовательский QSettings/HKCU не читается и не изменяется."),
    ("ASF-боты", "ASF IPC и автоматический запуск отключены."),
)


class _HarnessPage(QWidget):
    def __init__(self, runtime: HarnessRuntime, message: str):
        super().__init__()
        self.runtime = runtime
        layout = QVBoxLayout(self)
        label = QLabel(message)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)


class HarnessWindow(QMainWindow):
    """Small diagnostic shell; it intentionally does not construct MainWindow."""

    def __init__(self, runtime: HarnessRuntime):
        super().__init__()
        self.runtime = runtime
        self.setWindowTitle("GameDelivery — изолированный Harness Mode")
        self.resize(1000, 680)
        root = QWidget(self)
        layout = QVBoxLayout(root)
        status = QLabel(
            "Диагностический режим: пользовательские данные, настройки и интеграции отключены.\n"
            f"Временная область: {runtime.root}"
        )
        status.setWordWrap(True)
        layout.addWidget(status)
        tabs = QTabWidget(root)
        for title, message in _DIAGNOSTIC_PAGES:
            tabs.addTab(_HarnessPage(runtime, message), title)
        layout.addWidget(tabs)
        self.setCentralWidget(root)


def run_harness() -> int:
    """Run the isolated shell and remove only its own temporary directory on exit."""
    with tempfile.TemporaryDirectory(prefix="GameDelivery-Harness-") as temporary_root:
        try:
            runtime = HarnessRuntime.from_root(temporary_root)
            runtime.initialize_synthetic_database()
            application = QApplication(["GameDelivery-Harness"])
            window = HarnessWindow(runtime)
            window.show()
            exit_code = application.exec()
            window.close()
            window.deleteLater()
            application.processEvents()
            return int(exit_code)
        except (HarnessIsolationError, OSError, sqlite3.Error, RuntimeError) as exc:
            print(f"Harness Mode stopped safely: {type(exc).__name__}", file=sys.stderr)
            return 2
