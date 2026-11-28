from __future__ import annotations
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile, unittest
from pathlib import Path
from core.database.db import Database
from core.delivery.file_catalog import FileCatalog, FileCatalogError
from core.delivery.service_tasks import ServiceTaskStore, ServiceTaskError
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from app.operator_fulfillment_panel import OperatorFulfillmentPanel

class FileServiceTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.db=Database(Path(self.t.name)/"x.sqlite3")
 def tearDown(self): self.t.cleanup()
 def test_file_containment_hash_and_manual_gate(self):
  root=Path(self.t.name)/"files"; root.mkdir(); f=root/"a.bin"; f.write_bytes(b"abc")
  c=FileCatalog(self.db,root); rec=c.register(product_id="p",path=f); self.assertTrue(c.verify(rec["file_id"])["valid"]); self.assertEqual(c.reserve(file_id=rec["file_id"],order_id="o")["status"],"MANUAL_REQUIRED")
  with self.db.connect() as db: self.assertEqual(db.execute("select order_id from gd_files where file_id=?", (rec["file_id"],)).fetchone()[0], "o")
  with self.assertRaises(FileCatalogError): c.register(product_id="p",path=Path(self.t.name)/"../x")
  with self.assertRaises(FileCatalogError): c.register(product_id="p",path=f)


 def test_operator_file_reservation_requires_canonical_order(self):
  app = QApplication.instance() or QApplication([])
  class Keyring:
   def set_password(self, *args): pass
   def get_password(self, *args): return None
   def delete_password(self, *args): pass
  root=Path(self.t.name)/"files"; root.mkdir(); path=root/"a.bin"; path.write_bytes(b"abc")
  panel = OperatorFulfillmentPanel(database=self.db, managed_file_root=root, keyring_backend=Keyring())
  file_id=panel.files.register(product_id="p", path=path)["file_id"]
  panel.file_id.setText(file_id); panel.file_order.setText("missing")
  panel.reserve_file()
  with self.db.connect() as db:
   self.assertEqual(db.execute("select state from gd_files").fetchone()[0], "available")
   db.execute("create table gd_orders(order_id text primary key)")
   db.execute("insert into gd_orders values('order-1')")
  panel.file_order.setText("order-1")
  from PySide6.QtWidgets import QMessageBox
  with patch("app.operator_fulfillment_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
   panel.reserve_file()
  with self.db.connect() as db:
   self.assertEqual(db.execute("select state from gd_files").fetchone()[0], "manual_required")
  panel.deleteLater(); app.processEvents()

 def test_operator_code_reservation_requires_canonical_order(self):
  app = QApplication.instance() or QApplication([])
  class Keyring:
   def __init__(self): self.values = {}
   def set_password(self, service, account, value): self.values[(service, account)] = value
   def get_password(self, service, account): return self.values.get((service, account))
   def delete_password(self, service, account): self.values.pop((service, account), None)
  keyring = Keyring(); panel = OperatorFulfillmentPanel(database=self.db, managed_file_root=Path(self.t.name)/"files", keyring_backend=keyring)
  panel._store().import_codes(product_id="p", batch_id="b", source="CODE-1\n")
  panel.code_product.setText("p"); panel.code_order.setText("missing"); panel.reserve_code()
  with self.db.connect() as db:
   self.assertEqual(db.execute("select state from gd_digital_codes").fetchone()[0], "available")
   db.execute("create table gd_orders(order_id text primary key)")
   db.execute("insert into gd_orders values('order-1')")
  panel.code_order.setText("order-1"); panel.reserve_code()
  with self.db.connect() as db:
   self.assertEqual(db.execute("select state from gd_digital_codes").fetchone()[0], "reserved")
  panel.deleteLater(); app.processEvents()

 def test_service_audit_keeps_operator_evidence(self):
  s=ServiceTaskStore(self.db); uid=s.reserve(product_id="p",order_id="o",quantity=1,instructions="do",criteria="proof")
  s.transition(unit_id=uid,state="in_progress",actor="op",completed_units=0)
  s.transition(unit_id=uid,state="awaiting_confirmation",actor="op",completed_units=1,evidence={"note":"operator proof"})
  with self.db.connect() as db:
   row=db.execute("select evidence_json from gd_service_audit where unit_id=? order by created_at desc limit 1", (uid,)).fetchone()
  self.assertIn("operator proof", row[0])

 def test_service_reservation_requires_order_id_at_store_boundary(self):
  s=ServiceTaskStore(self.db)
  with self.assertRaises(ServiceTaskError):
   s.reserve(product_id="p",order_id="",quantity=1,instructions="do",criteria="proof")

 def test_service_confirmation_requires_evidence_at_store_boundary(self):
  s=ServiceTaskStore(self.db); uid=s.reserve(product_id="p",order_id="o",quantity=1,instructions="do",criteria="proof")
  s.transition(unit_id=uid,state="in_progress",actor="op")
  with self.assertRaises(ServiceTaskError):
   s.transition(unit_id=uid,state="awaiting_confirmation",actor="op",completed_units=1)
  with self.db.connect() as db:
   self.assertEqual(db.execute("select state from gd_service_units where unit_id=?", (uid,)).fetchone()[0], "in_progress")

 def test_operator_service_reservation_requires_canonical_order(self):
  app = QApplication.instance() or QApplication([])
  panel = OperatorFulfillmentPanel(database=self.db, managed_file_root=Path(self.t.name)/"files", keyring_backend=None)
  panel.service_product.setText("service-product")
  panel.service_order.setText("missing")
  panel.service_instructions.setText("Perform task")
  panel.service_criteria.setText("Provide evidence")
  panel.reserve_service()
  with self.db.connect() as db:
   self.assertIsNone(db.execute("select 1 from gd_service_units").fetchone())
   db.execute("create table gd_orders(order_id text primary key)")
   db.execute("insert into gd_orders values('order-1')")
  panel.service_order.setText("order-1")
  panel.reserve_service()
  with self.db.connect() as db:
   self.assertEqual(db.execute("select order_id from gd_service_units").fetchone()[0], "order-1")
  panel.deleteLater(); app.processEvents()

 def test_service_partial_and_completion_requirements(self):
  s=ServiceTaskStore(self.db); uid=s.reserve(product_id="p",order_id="o",quantity=2,instructions="do",criteria="proof")
  s.transition(unit_id=uid,state="in_progress",actor="op",completed_units=1)
  with self.assertRaises(ServiceTaskError): s.transition(unit_id=uid,state="completed",actor="op",completed_units=1)
  s.transition(unit_id=uid,state="awaiting_confirmation",actor="op",completed_units=2,evidence={"proof":"ready"})
  s.transition(unit_id=uid,state="completed",actor="op",completed_units=2,evidence={"proof":"local-ref"})
  with self.db.connect() as db: self.assertGreaterEqual(db.execute("select count(*) from gd_service_audit where unit_id=?", (uid,)).fetchone()[0], 4)
  with self.db.connect() as db: self.assertEqual(db.execute("select state from gd_service_units").fetchone()[0],"completed")
if __name__ == "__main__": unittest.main()
