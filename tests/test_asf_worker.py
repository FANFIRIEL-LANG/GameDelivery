import threading
import unittest

from PySide6.QtCore import QThreadPool

from app.main import _AsfCallWorker


class AsfWorkerThreadTests(unittest.TestCase):
    def test_blocking_operation_runs_outside_calling_thread(self):
        caller_thread = threading.get_ident()
        completed = threading.Event()
        observed = {}

        def operation():
            observed["thread_id"] = threading.get_ident()
            completed.set()
            return "ok"

        worker = _AsfCallWorker(operation, lambda _result, _error: None)
        QThreadPool.globalInstance().start(worker)
        self.assertTrue(completed.wait(3), "ASF worker did not execute in time")
        self.assertNotEqual(observed["thread_id"], caller_thread)
        self.assertTrue(QThreadPool.globalInstance().waitForDone(3000))


if __name__ == "__main__":
    unittest.main()
