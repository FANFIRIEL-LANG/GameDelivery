from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from integrations.openskin.client import (
    OpenSkinClient, OpenSkinError, PricePosition, probe_proxy_connection,
)
from integrations.openskin.proxy import (
    OpenSkinProxy, OpenSkinProxySettingsStore, parse_openskin_proxy_text,
)


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
    def __init__(self, proxy: OpenSkinProxy | None):
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
        layout.addWidget(QLabel("Прокси — вставь всю строку целиком"))
        self.proxy_input = QLineEdit()
        self.proxy_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_input.setPlaceholderText(
            "http://login:password@host:port | socks5://login:password@host:port | host:port:login:password"
        )
        self.proxy_input.setClearButtonEnabled(True)
        layout.addWidget(self.proxy_input)

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
        self.proxy_input.clear()
        host = str(profile.get("host", "") or "")
        if host:
            scheme = str(profile.get("scheme", "http") or "http")
            port = int(profile.get("port", 8080))
            auth = " (авторизация сохранена)" if profile.get("username") else ""
            self.proxy_status.setText(
                f"Сохранён {scheme}://{host}:{port}{auth}. Строка с паролем не отображается."
            )
        elif self.enabled.isChecked():
            self.proxy_status.setText(
                "Прокси включён, но профиль не найден. Вставь строку прокси."
            )

    def _form_proxy(self) -> OpenSkinProxy | None:
        value = self.proxy_input.text().strip()
        if value:
            return parse_openskin_proxy_text(value)
        if not self.enabled.isChecked():
            return None
        proxy = self.store.load_enabled_proxy()
        if proxy is None:
            raise ValueError("Вставь прокси целиком в одно поле или выключи прокси.")
        return proxy

    def save_settings(self):
        try:
            value = self.proxy_input.text().strip()
            profile = self.store.load_profile()
            if value:
                proxy = parse_openskin_proxy_text(value)
            elif self.enabled.isChecked():
                proxy = self.store.load_enabled_proxy()
                if proxy is None:
                    raise ValueError("Вставь прокси целиком в одно поле.")
            else:
                proxy = None

            if proxy is not None:
                self.store.save(
                    enabled=self.enabled.isChecked(), scheme=proxy.scheme,
                    host=proxy.host, port=proxy.port, username=proxy.username,
                    password=proxy.password,
                )
                summary = proxy.safe_summary()
                safe_route = f"{summary['scheme']}://{summary['host']}:{summary['port']}"
                auth = " (авторизация сохранена)" if summary["authentication"] else ""
            else:
                host = str(profile.get("host", "") or "")
                if host:
                    self.store.save(
                        enabled=False, scheme=str(profile.get("scheme", "http")),
                        host=host, port=int(profile.get("port", 8080)),
                        username=str(profile.get("username", "") or "") or None,
                        password=None,
                    )
                else:
                    self.store.save(
                        enabled=False, scheme="http", host="", port=8080,
                        username=None, password=None,
                    )
                safe_route = ""
                auth = ""
        except (ValueError, RuntimeError, OSError) as exc:
            self.proxy_status.setText(str(exc))
            return
        self.proxy_input.clear()
        self.proxy_status.setText(
            f"Настройки сохранены: {safe_route}{auth}. Пароль хранится в системном хранилище."
            if self.enabled.isChecked() and proxy is not None
            else "Прокси OpenSkin выключен; применяется прямое соединение."
        )

    def check_proxy_connection(self):
        if self._proxy_check_task is not None:
            return
        try:
            proxy = self._form_proxy()
            if proxy is None:
                raise ValueError("Вставь строку прокси или включи сохранённый профиль.")
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
