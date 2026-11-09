from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from integrations.openskin.client import (
    OpenSkinClient, OpenSkinError, PricePosition, probe_proxy_connection,
)
from integrations.openskin.proxy import OpenSkinProxy, OpenSkinProxySettingsStore


class _CheckSignals(QObject):
    finished = Signal(bool, str)


class _ProxyCheckTask(QRunnable):
    def __init__(self, proxy: OpenSkinProxy):
        super().__init__()
        self.proxy = proxy
        self.signals = _CheckSignals()

    def run(self):
        try:
            status = probe_proxy_connection(self.proxy)
            self.signals.finished.emit(
                True,
                "Прокси, HTTPS CONNECT/SOCKS5 и TLS работают; "
                f"контрольная точка ответила HTTP {status}.",
            )
        except OpenSkinError as exc:
            self.signals.finished.emit(False, exc.user_message)
        except Exception:
            self.signals.finished.emit(False, "Не удалось выполнить проверку proxy.")


class _OpenSkinCheckTask(QRunnable):
    def __init__(self, proxy: OpenSkinProxy):
        super().__init__()
        self.proxy = proxy
        self.signals = _CheckSignals()

    def run(self):
        try:
            OpenSkinClient(proxy=self.proxy).fetch_prices(
                [PricePosition(730, "AK-47 | Redline (Field-Tested)")]
            )
            self.signals.finished.emit(True, "OpenSkin API ответил на ценовой запрос.")
        except OpenSkinError as exc:
            self.signals.finished.emit(False, exc.user_message)
        except Exception:
            self.signals.finished.emit(False, "Не удалось выполнить запрос к OpenSkin API.")


class OpenSkinProxySettingsWidget(QWidget):
    """Per-request OpenSkin proxy settings; Frankfurter and other clients are separate."""

    def __init__(self, parent=None, *, store: OpenSkinProxySettingsStore | None = None):
        super().__init__(parent)
        self.store = store or OpenSkinProxySettingsStore()
        self._proxy_check_task = None
        self._openskin_check_task = None
        self._build_ui()
        self._load_profile()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Эти настройки применяются только к OpenSkin Pricing API. "
            "HTTP CONNECT туннелирует HTTPS API; также поддерживается SOCKS5. "
            "Frankfurter, Steam, ASF IPC и загрузка изображений используют отдельные соединения. "
            "Пароль хранится в системном хранилище учётных данных."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        layout.addWidget(intro)

        self.enabled = QCheckBox("Использовать прокси для OpenSkin")
        layout.addWidget(self.enabled)
        form = QFormLayout()
        self.scheme = QComboBox()
        self.scheme.addItem("HTTP CONNECT", "http")
        self.scheme.addItem("SOCKS5", "socks5")
        self.host = QLineEdit()
        self.host.setPlaceholderText("proxy.example или IP-адрес")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(8080)
        self.username = QLineEdit()
        self.username.setPlaceholderText("Необязательно")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Пустое поле оставляет пароль из хранилища")
        form.addRow("Тип прокси", self.scheme)
        form.addRow("IP-адрес или домен", self.host)
        form.addRow("Порт", self.port)
        form.addRow("Логин", self.username)
        form.addRow("Пароль", self.password)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("Сохранить настройки")
        self.proxy_check_button = QPushButton("Проверить прокси")
        self.openskin_check_button = QPushButton("Проверить OpenSkin API")
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.proxy_check_button)
        buttons.addWidget(self.openskin_check_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.proxy_status = QLabel("Проверка прокси не выполнялась.")
        self.proxy_status.setWordWrap(True)
        self.proxy_status.setObjectName("muted")
        layout.addWidget(self.proxy_status)
        self.openskin_status = QLabel("Проверка OpenSkin API не выполнялась.")
        self.openskin_status.setWordWrap(True)
        self.openskin_status.setObjectName("muted")
        layout.addWidget(self.openskin_status)
        # Retain the old attribute for existing callers; it now means the API check.
        self.check_button = self.openskin_check_button
        self.status = self.openskin_status
        layout.addStretch(1)

        self.save_button.clicked.connect(self.save_settings)
        self.proxy_check_button.clicked.connect(self.check_proxy_connection)
        self.openskin_check_button.clicked.connect(self.check_openskin_connection)

    def _load_profile(self):
        profile = self.store.load_profile()
        self.enabled.setChecked(bool(profile["enabled"]))
        self.host.setText(str(profile["host"]))
        self.port.setValue(int(profile["port"]))
        self.username.setText(str(profile["username"]))
        index = self.scheme.findData(profile["scheme"])
        self.scheme.setCurrentIndex(index if index >= 0 else 0)
        if self.enabled.isChecked():
            self.proxy_status.setText(
                "Настройки прокси загружены; новые запросы перечитают их автоматически."
            )

    def _form_proxy(self) -> OpenSkinProxy:
        username = self.username.text().strip() or None
        password = self.password.text() or None
        if username and not password:
            password = self.store.saved_password()
        return OpenSkinProxy(
            str(self.scheme.currentData()), self.host.text(), self.port.value(),
            username, password,
        )

    def save_settings(self):
        try:
            username = self.username.text().strip() or None
            self.store.save(
                enabled=self.enabled.isChecked(),
                scheme=str(self.scheme.currentData()), host=self.host.text(),
                port=self.port.value(), username=username,
                password=self.password.text() or None,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            self.proxy_status.setText(str(exc))
            return
        self.password.clear()
        self.proxy_status.setText(
            "Настройки OpenSkin сохранены; новые запросы применят их без перезапуска ASF."
            if self.enabled.isChecked() else "Прокси OpenSkin выключен; применяется прямое соединение."
        )

    def check_proxy_connection(self):
        if self._proxy_check_task is not None:
            return
        try:
            proxy = self._form_proxy()
        except (ValueError, RuntimeError, OSError) as exc:
            self.proxy_status.setText(str(exc))
            return
        task = _ProxyCheckTask(proxy)
        task.signals.finished.connect(self._proxy_check_finished)
        self._proxy_check_task = task
        self.proxy_check_button.setEnabled(False)
        self.proxy_status.setText("Проверяю proxy-аутентификацию и HTTPS-туннель…")
        QThreadPool.globalInstance().start(task)

    def check_openskin_connection(self):
        if self._openskin_check_task is not None:
            return
        try:
            proxy = self._form_proxy()
        except (ValueError, RuntimeError, OSError) as exc:
            self.openskin_status.setText(str(exc))
            return
        task = _OpenSkinCheckTask(proxy)
        task.signals.finished.connect(self._openskin_check_finished)
        self._openskin_check_task = task
        self.openskin_check_button.setEnabled(False)
        self.openskin_status.setText("Проверяю read-only запрос OpenSkin API…")
        QThreadPool.globalInstance().start(task)

    def check_connection(self):
        """Backward-compatible alias for the explicit OpenSkin API check."""
        self.check_openskin_connection()

    def _proxy_check_finished(self, ok: bool, message: str):
        self._proxy_check_task = None
        self.proxy_check_button.setEnabled(True)
        self.proxy_status.setText(message)

    def _openskin_check_finished(self, ok: bool, message: str):
        self._openskin_check_task = None
        self.openskin_check_button.setEnabled(True)
        self.openskin_status.setText(message)
