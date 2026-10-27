import sys

from app.main import MainWindow, main


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        from PySide6.QtWidgets import QApplication

        application = QApplication([])
        window = MainWindow()
        window.close()
        window.deleteLater()
        application.processEvents()
        sys.exit(0)

    main()
