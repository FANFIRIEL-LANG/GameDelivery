import logging
import tempfile
import unittest
from pathlib import Path

from app.diagnostics import configure_logging, get_logger, log_exception


class DiagnosticsLoggingTests(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger("gamedelivery")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def test_configure_logging_writes_utf8_rotating_file_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "logs"
            first = configure_logging(log_directory=directory)
            second = configure_logging(log_directory=directory)
            logger = get_logger("tests")
            logger.info("Проверка диагностического журнала")

            root = logging.getLogger("gamedelivery")
            self.assertEqual(first, directory)
            self.assertEqual(second, directory)
            self.assertEqual(len(root.handlers), 1)
            log_file = directory / "gamedelivery.log"
            self.assertTrue(log_file.exists())
            self.assertIn("Проверка диагностического журнала", log_file.read_text(encoding="utf-8"))

    def test_sensitive_values_are_redacted_from_messages_and_tracebacks(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "logs"
            configure_logging(log_directory=directory)
            logger = get_logger("tests")
            logger.info("IPCPassword=%s trade_url=%s", "ipc-secret", "https://steamcommunity.com/tradeoffer/new/?partner=123&token=trade-secret")
            try:
                raise RuntimeError("request failed: password=account-secret")
            except RuntimeError as exc:
                log_exception(logger, "test operation", exc)

            content = (directory / "gamedelivery.log").read_text(encoding="utf-8")
            for secret in ("ipc-secret", "trade-secret", "account-secret"):
                self.assertNotIn(secret, content)
            self.assertIn("[REDACTED]", content)
            self.assertIn("exception_type=RuntimeError", content)
            self.assertIn("Sanitized traceback", content)


if __name__ == "__main__":
    unittest.main()
