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
        import os
        import tempfile
        from pathlib import Path
        from PySide6.QtWidgets import QApplication

        previous = {
            key: os.environ.get(key)
            for key in ("GAMEDELIVERY_TEST_ASF_IPC_URL", "GAMEDELIVERY_SELF_TEST", "GAMEDELIVERY_DATABASE_PATH")
        }
        try:
            os.environ.setdefault("GAMEDELIVERY_TEST_ASF_IPC_URL", "http://127.0.0.1:1")
            os.environ["GAMEDELIVERY_SELF_TEST"] = "1"
            with tempfile.TemporaryDirectory(prefix="gamedelivery-self-test-") as temp_dir:
                os.environ["GAMEDELIVERY_DATABASE_PATH"] = str(Path(temp_dir) / "gamedelivery.sqlite3")
                application = QApplication([])
                window = MainWindow()
                window.close()
                window.deleteLater()
                application.processEvents()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return 0

    main()
    return 0


if __name__ == "__main__":
    raise SystemExit(_dispatch())
