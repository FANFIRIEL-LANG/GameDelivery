from __future__ import annotations

import json
from pathlib import Path
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from core.delivery.digital_codes import DigitalCodeStore, preview_import
from core.delivery.file_catalog import FileCatalog, FileCatalogError
from core.delivery.service_tasks import ServiceTaskStore, ServiceTaskError


class OperatorFulfillmentPanel(QWidget):
    status_changed = Signal(str)

    def __init__(self, *, database, managed_file_root: str | Path, keyring_backend=None, parent=None):
        super().__init__(parent); self.database = database; self.file_root = Path(managed_file_root)
        self.keyring_backend = keyring_backend
        self.digital = None
        self.files = FileCatalog(database, self.file_root)
        self.services = ServiceTaskStore(database)
        self.tabs = QTabWidget(); self.code_product = QLineEdit(); self.code_batch = QLineEdit(); self.code_order = QLineEdit()
        code = QWidget(); cl = QVBoxLayout(code); form = QFormLayout(); form.addRow("Товар кодов", self.code_product); form.addRow("Партия", self.code_batch); form.addRow("Order ID", self.code_order); cl.addLayout(form)
        buttons = QHBoxLayout(); b_preview = QPushButton("Preview импорта"); b_preview.clicked.connect(self.preview_codes); b_import = QPushButton("Импортировать коды"); b_import.clicked.connect(self.import_codes); b_reserve = QPushButton("Зарезервировать код"); b_reserve.clicked.connect(self.reserve_code); b_refresh = QPushButton("Обновить stock"); b_refresh.clicked.connect(self.refresh_codes); b_reveal = QPushButton("Раскрыть выбранный"); b_reveal.clicked.connect(self.reveal_code); b_release = QPushButton("Reconcile: release"); b_release.clicked.connect(lambda: self.reconcile_code("released")); b_consume = QPushButton("Reconcile: consume"); b_consume.clicked.connect(lambda: self.reconcile_code("consumed"));
        for button in (b_preview, b_import, b_reserve, b_refresh, b_reveal, b_release, b_consume): buttons.addWidget(button)
        cl.addLayout(buttons)
        self.code_status = QLabel("Коды: действие не выполнялось"); self.code_status.setWordWrap(True); cl.addWidget(self.code_status); self.code_table = QTableWidget(0,5); self.code_table.setHorizontalHeaderLabels(["Code ID","Product","Batch","State","Order ID"]); cl.addWidget(self.code_table)
        files = QWidget(); fl = QVBoxLayout(files); self.file_product = QLineEdit(); self.file_id = QLineEdit(); self.file_order = QLineEdit(); fb = QPushButton("Зарегистрировать файл"); fb.clicked.connect(self.register_file); fbr = QPushButton("Зарезервировать файл"); fbr.clicked.connect(self.reserve_file); fl.addWidget(self.file_product); fl.addWidget(self.file_id); fl.addWidget(self.file_order); fl.addWidget(fb); fl.addWidget(fbr); self.file_status=QLabel("Файлы: передача требует подтверждённого adapter"); fl.addWidget(self.file_status)
        service = QWidget(); sl = QVBoxLayout(service); self.service_product=QLineEdit(); self.service_order=QLineEdit(); self.service_id=QLineEdit(); self.service_state=QLineEdit("in_progress"); self.service_done=QSpinBox(); self.service_done.setMinimum(0); self.service_instructions=QLineEdit(); self.service_criteria=QLineEdit(); self.service_evidence=QLineEdit(); self.service_quantity=QSpinBox(); self.service_quantity.setMinimum(1); sf=QFormLayout(); sf.addRow("Unit ID",self.service_id); sf.addRow("Состояние",self.service_state); sf.addRow("Выполнено",self.service_done); sf.addRow("Товар услуги",self.service_product); sf.addRow("Order ID",self.service_order); sf.addRow("Количество",self.service_quantity); sf.addRow("Инструкции",self.service_instructions); sf.addRow("Критерии",self.service_criteria); sf.addRow("Доказательство / заметка",self.service_evidence); sl.addLayout(sf); sb=QPushButton("Зарезервировать единицы услуги"); sb.clicked.connect(self.reserve_service); st=QPushButton("Обновить состояние услуги"); st.clicked.connect(self.transition_service); sl.addWidget(sb); sl.addWidget(st); self.service_status=QLabel("Услуги: действие не выполнялось"); sl.addWidget(self.service_status)
        self.tabs.addTab(code,"Цифровые коды"); self.tabs.addTab(files,"Файлы"); self.tabs.addTab(service,"Услуги"); layout=QVBoxLayout(self); layout.addWidget(self.tabs)

    def _store(self):
        if self.digital is None:
            if self.keyring_backend is None: raise RuntimeError("OS keyring backend is not configured")
            self.digital=DigitalCodeStore(self.database,self.keyring_backend)
        return self.digital

    def preview_codes(self):
        path,_=QFileDialog.getOpenFileName(self,"Preview импорта кодов","","Text files (*.txt);;All files (*)")
        if not path: return
        try:
            report=preview_import(Path(path)); self.code_status.setText("Preview: " + json.dumps(report, ensure_ascii=False, sort_keys=True)); self.status_changed.emit("digital_code_preview")
        except Exception as exc: self._error(self.code_status,"Preview не выполнен",exc)

    def import_codes(self):
        path,_=QFileDialog.getOpenFileName(self,"Импорт кодов","","Text files (*.txt);;All files (*)")
        if not path: return
        if QMessageBox.question(self,"Подтверждение импорта","Импортировать только после успешного preview?") != QMessageBox.StandardButton.Yes: return
        try:
            result=self._store().import_codes(product_id=self.code_product.text(),batch_id=self.code_batch.text(),source=Path(path)); self.code_status.setText(f"Импортировано: {result['imported']}; секреты не показываются"); self.status_changed.emit("digital_code_imported")
        except Exception as exc: self._error(self.code_status,"Импорт не выполнен",exc)

    def reserve_code(self):
        try:
            product_id = self.code_product.text().strip()
            order_id = self.code_order.text().strip()
            if not product_id or not order_id:
                raise ValueError("Товар кодов и Order ID обязательны")
            with self.database.connect() as db:
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(gd_orders)")}
                if "order_id" not in columns:
                    raise ValueError("Канонический order store недоступен")
                if db.execute("SELECT 1 FROM gd_orders WHERE order_id=?", (order_id,)).fetchone() is None:
                    raise ValueError("Order ID не найден в канонической очереди")
            result=self._store().reserve(product_id=product_id,order_id=order_id)
            self.code_status.setText("Зарезервирован code ID: " + result["code_id"]); self.status_changed.emit("digital_code_reserved")
        except Exception as exc: self._error(self.code_status,"Резервация не выполнена",exc)

    def refresh_codes(self):
        try:
            rows = self._store().list_codes(product_id=self.code_product.text())
            self.code_table.setRowCount(0)
            for row in rows:
                index = self.code_table.rowCount(); self.code_table.insertRow(index)
                for column, key in enumerate(("code_id", "product_id", "batch_id", "state", "order_id")):
                    self.code_table.setItem(index, column, QTableWidgetItem(str(row.get(key) or "")))
            self.code_status.setText(f"Stock строк: {len(rows)}; plaintext не отображается")
        except Exception as exc:
            self._error(self.code_status, "Stock не загружен", exc)

    def _selected_code(self):
        row = self.code_table.currentRow()
        if row < 0:
            raise ValueError("Выбери строку кода")
        code_id = self.code_table.item(row, 0).text().strip()
        order_id = (self.code_table.item(row, 4).text().strip() or self.code_order.text().strip())
        if not code_id or not order_id:
            raise ValueError("Для строки нужен code ID и Order ID")
        return code_id, order_id

    def reveal_code(self):
        try:
            code_id, order_id = self._selected_code()
            if QMessageBox.question(self, "Подтверждение раскрытия", "Раскрыть код оператору один раз?" ) != QMessageBox.StandardButton.Yes:
                return
            value = self._store().reveal(code_id=code_id, order_id=order_id, actor="operator-ui")
            QMessageBox.information(self, "Код", value)
            self.code_status.setText("Код раскрыт и записан в аудит; значение не записывается в журнал")
            self.refresh_codes()
        except Exception as exc:
            self._error(self.code_status, "Раскрытие не выполнено", exc)

    def reconcile_code(self, outcome: str):
        try:
            code_id, order_id = self._selected_code()
            if QMessageBox.question(self, "Подтверждение reconcile", f"Зафиксировать исход {outcome}?") != QMessageBox.StandardButton.Yes:
                return
            self._store().reconcile(code_id=code_id, order_id=order_id, outcome=outcome, actor="operator-ui", evidence={"source": "operator-ui"})
            self.code_status.setText("UNKNOWN сверён оператором: " + outcome)
            self.refresh_codes()
        except Exception as exc:
            self._error(self.code_status, "Reconcile не выполнен", exc)

    def register_file(self):
        path,_=QFileDialog.getOpenFileName(self,"Файл товара",str(self.file_root),"All files (*)")
        if not path: return
        try:
            result=self.files.register(product_id=self.file_product.text(),path=path); self.file_status.setText(f"Файл зарегистрирован: {result['file_id']}; выдача manual-required"); self.status_changed.emit("file_registered")
        except Exception as exc: self._error(self.file_status,"Регистрация файла не выполнена",exc)

    def reserve_file(self):
        try:
            file_id = self.file_id.text().strip(); order_id = self.file_order.text().strip()
            if not file_id or not order_id: raise ValueError("File ID и Order ID обязательны")
            with self.database.connect() as db:
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(gd_orders)")}
                if "order_id" not in columns:
                    raise ValueError("Канонический order store недоступен")
                if db.execute("SELECT 1 FROM gd_orders WHERE order_id=?", (order_id,)).fetchone() is None:
                    raise ValueError("Order ID не найден в канонической очереди")
            if QMessageBox.question(self, "Подтверждение резерва файла", "Зарезервировать файл для ручной передачи?") != QMessageBox.StandardButton.Yes: return
            result = self.files.reserve(file_id=file_id, order_id=order_id)
            self.file_status.setText("Файл зарезервирован: " + result["status"] + "; передача вручную")
            self.status_changed.emit("file_reserved_manual")
        except Exception as exc: self._error(self.file_status, "Резервация файла не выполнена", exc)

    def reserve_service(self):
        try:
            product_id = self.service_product.text().strip()
            order_id = self.service_order.text().strip()
            if not product_id or not order_id:
                raise ValueError("Товар услуги и Order ID обязательны")
            with self.database.connect() as db:
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(gd_orders)")}
                if "order_id" not in columns:
                    raise ValueError("Канонический order store недоступен")
                if db.execute("SELECT 1 FROM gd_orders WHERE order_id=?", (order_id,)).fetchone() is None:
                    raise ValueError("Order ID не найден в канонической очереди")
            uid = self.services.reserve(
                product_id=product_id, order_id=order_id,
                quantity=self.service_quantity.value(),
                instructions=self.service_instructions.text(), criteria=self.service_criteria.text(),
            )
            self.service_status.setText("Единица услуги зарезервирована: " + uid); self.status_changed.emit("service_reserved")
        except Exception as exc: self._error(self.service_status,"Резервация услуги не выполнена",exc)

    def transition_service(self):
        try:
            unit_id = self.service_id.text().strip(); state = self.service_state.text().strip()
            if not unit_id or not state: raise ValueError("Unit ID и состояние обязательны")
            if QMessageBox.question(self, "Подтверждение состояния услуги", "Записать переход и audit?") != QMessageBox.StandardButton.Yes: return
            evidence_text = self.service_evidence.text().strip()
            if state in {"awaiting_confirmation", "completed"} and not evidence_text:
                raise ValueError("Для подтверждения услуги укажи evidence или заметку.")
            self.services.transition(
                unit_id=unit_id, state=state, actor="operator-ui",
                completed_units=self.service_done.value(),
                evidence={"source": "operator-ui", "note": evidence_text},
            )
            self.service_status.setText("Состояние услуги обновлено: " + state); self.status_changed.emit("service_state_changed")
        except Exception as exc: self._error(self.service_status, "Состояние услуги не обновлено", exc)

    @staticmethod
    def _error(label, title, exc): label.setText(title + ": " + type(exc).__name__)

