import sys
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QFrame,
)
from app.inventory_panel import InventoryPanel
from app.proxy_settings import ProxySettingsWidget
from app.asf_management import ASFManagementWidget


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GameDelivery — локальная панель")
        self.resize(1120, 720)
        self.setMinimumSize(900, 600)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)
        header = QHBoxLayout()
        title = QLabel("GameDelivery")
        title.setObjectName("title")
        subtitle = QLabel("Автоматизация доставки игровых предметов")
        subtitle.setObjectName("muted")
        status = QLabel("Playerok: не подключён")
        status.setObjectName("status")
        header.addWidget(title)
        header.addSpacing(12)
        header.addWidget(subtitle)
        header.addStretch()
        header.addWidget(status)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._orders_tab(), "Заказы")
        self.tabs.addTab(self._inventory_tab(), "Инвентарь")
        self.tabs.addTab(self._games_tab(), "Модули игр")
        self.tabs.addTab(self._logs_tab(), "Журнал")
        self.tabs.addTab(self._settings_tab(), "Настройки")
        self.tabs.addTab(ASFManagementWidget(), "ASF-боты")
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #171b22; color: #e8edf5; font-size: 13px; }
            QLabel#title { font-size: 25px; font-weight: 700; color: #f3f6fb; }
            QLabel#muted { color: #9aa6b7; }
            QLabel#status { color: #f2c879; padding: 8px 12px; background: #29271f; border-radius: 8px; }
            QTabWidget::pane { border: 1px solid #303846; border-radius: 10px; padding: 8px; }
            QTabBar::tab { background: #222833; padding: 10px 16px; margin-right: 4px; border-radius: 6px; }
            QTabBar::tab:selected { background: #304a3d; color: #a8f0c6; }
            QPushButton { background: #2e654b; border: 0; border-radius: 7px; padding: 9px 13px; font-weight: 600; }
            QPushButton:hover { background: #397b5b; }
            QTableWidget, QTextEdit { background: #1e242d; border: 1px solid #303846; border-radius: 7px; gridline-color: #303846; }
            QLineEdit, QComboBox, QSpinBox, QListWidget { background: #1e242d; border: 1px solid #303846; border-radius: 6px; padding: 6px; }
            QHeaderView::section { background: #252d38; color: #cbd5e1; border: 0; padding: 8px; }
            QFrame#card { background: #202731; border: 1px solid #303846; border-radius: 10px; }
        """)

    def _orders_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("Демонстрационный режим: реальные обмены отключены.")
        note.setObjectName("muted")
        layout.addWidget(note)
        self.orders = QTableWidget(0, 5)
        self.orders.setHorizontalHeaderLabels(["ID заказа", "Источник", "Товар", "Статус", "Обновлено"])
        self.orders.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.orders)
        buttons = QHBoxLayout()
        add_demo = QPushButton("Добавить тестовый заказ")
        add_demo.clicked.connect(self._add_demo_order)
        buttons.addWidget(add_demo)
        buttons.addStretch()
        layout.addLayout(buttons)
        return page

    def _add_demo_order(self):
        row = self.orders.rowCount()
        self.orders.insertRow(row)
        values = [f"DEMO-{row+1:04d}", "Playerok (демо)", "Steam-карточки", "Тест: не отправлено", "Локально"]
        for col, value in enumerate(values):
            self.orders.setItem(row, col, QTableWidgetItem(value))

    def _inventory_tab(self):
        return InventoryPanel()

    def _games_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        for name, state in [
            ("Steam-карточки", "Первый модуль · планируется"),
            ("Counter-Strike 2", "Скины и предметы · планируется"),
            ("Другие игры", "Подключаемые модули · будущий этап"),
        ]:
            card = QFrame()
            card.setObjectName("card")
            row = QHBoxLayout(card)
            name_label = QLabel(name)
            name_label.setStyleSheet("font-weight: 600;")
            state_label = QLabel(state)
            state_label.setObjectName("muted")
            row.addWidget(name_label)
            row.addStretch()
            row.addWidget(state_label)
            layout.addWidget(card)
        layout.addStretch()
        return page

    def _logs_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        log = QTextEdit()
        log.setReadOnly(True)
        log.setPlainText("GameDelivery: тестовый режим.\nPlayerok не подключён.\nSteam API не используется.\nРеальная отправка предметов отключена.")
        layout.addWidget(log)
        return page

    def _settings_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Steam-аккаунты и прокси"))
        self.proxy_settings = ProxySettingsWidget()
        layout.addWidget(self.proxy_settings, 1)
        telegram = QLabel("Telegram: только уведомления и статистика.")
        telegram.setObjectName("muted")
        layout.addWidget(telegram)
        warning = QLabel(
            "Прокси можно применить через локальный ASF IPC. Для Steam CM приложение "
            "запросит отдельное подтверждение глобального SteamProtocols=4. Фактическая "
            "маршрутизация трафика и реальная отправка предметов пока не подтверждены."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #f2c879;")
        layout.addWidget(warning)
        return page


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
