from __future__ import annotations
import tempfile, unittest
from pathlib import Path
from core.database.db import Database
from core.delivery.file_catalog import FileCatalog, FileCatalogError
from core.delivery.service_tasks import ServiceTaskStore, ServiceTaskError

class FileServiceTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.db=Database(Path(self.t.name)/"x.sqlite3")
 def tearDown(self): self.t.cleanup()
 def test_file_containment_hash_and_manual_gate(self):
  root=Path(self.t.name)/"files"; root.mkdir(); f=root/"a.bin"; f.write_bytes(b"abc")
  c=FileCatalog(self.db,root); rec=c.register(product_id="p",path=f); self.assertTrue(c.verify(rec["file_id"])["valid"]); self.assertEqual(c.reserve(file_id=rec["file_id"],order_id="o")["status"],"MANUAL_REQUIRED")
  with self.assertRaises(FileCatalogError): c.register(product_id="p",path=Path(self.t.name)/"../x")
 def test_service_partial_and_completion_requirements(self):
  s=ServiceTaskStore(self.db); uid=s.reserve(product_id="p",order_id="o",quantity=2,instructions="do",criteria="proof")
  s.transition(unit_id=uid,state="in_progress",actor="op",completed_units=1)
  with self.assertRaises(ServiceTaskError): s.transition(unit_id=uid,state="completed",actor="op",completed_units=1)
  s.transition(unit_id=uid,state="awaiting_confirmation",actor="op",completed_units=2)
  s.transition(unit_id=uid,state="completed",actor="op",completed_units=2,evidence={"proof":"local-ref"})
  with self.db.connect() as db: self.assertGreaterEqual(db.execute("select count(*) from gd_service_audit where unit_id=?", (uid,)).fetchone()[0], 4)
  with self.db.connect() as db: self.assertEqual(db.execute("select state from gd_service_units").fetchone()[0],"completed")
if __name__ == "__main__": unittest.main()
