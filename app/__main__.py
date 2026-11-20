"""Application entry point with a dependency-free isolated harness mode."""

import sys


def _dispatch(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--harness" in args:
        # Keep this branch before importing app.main: its import graph contains
        # the production UI, persistence, browser, and integration components.
        from app.harness import run_harness

        return run_harness()

    from app.main import MainWindow, main

    if "--self-test" in args:
        from PySide6.QtWidgets import QApplication

        application = QApplication([])
        window = MainWindow()
        window.close()
        window.deleteLater()
        application.processEvents()
        return 0

    main()
    return 0


if __name__ == "__main__":
    raise SystemExit(_dispatch())
