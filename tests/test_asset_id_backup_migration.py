from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.database.db as db_module
from core.database.connection import connect_sqlite
from core.database.db import Database
from core.database.ledger_migrations import (
    ASSET_ID_BLOCKING_STATE_BACKUP_SUFFIX,
    ASSET_ID_BLOCKING_STATE_MIGRATION_ID,
    ASSET_ID_KEY_BACKUP_SUFFIX,
    ASSET_ID_KEY_MIGRATION_ID,
    apply_delivery_ledger_migrations,
)


class AssetIDBackupMigrationTests(unittest.TestCase):
    @staticmethod
    def backup_path(database_path: Path, suffix: str) -> Path:
        return Path(str(database_path) + suffix)

    @staticmethod
    def marker(connection: sqlite3.Connection, migration_id: str) -> bool:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gd_schema_migrations'"
        ).fetchone() is None:
            return False
        return connection.execute(
            "SELECT 1 FROM gd_schema_migrations WHERE migration_id=?", (migration_id,)
        ).fetchone() is not None

    @staticmethod
    def drop_blocking_guards(connection: sqlite3.Connection) -> None:
        connection.execute("DROP TRIGGER IF EXISTS gd_asset_reservations_guard_insert")
        connection.execute("DROP TRIGGER IF EXISTS gd_asset_reservations_guard_update")

    def test_clean_old_database_is_backed_up_before_full_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            with connect_sqlite(path) as old:
                old.execute("CREATE TABLE old_probe(value TEXT NOT NULL)")
                old.execute("INSERT INTO old_probe VALUES('kept')")

            Database(path).initialize()
            backup_path = self.backup_path(path, ASSET_ID_BLOCKING_STATE_BACKUP_SUFFIX)
            self.assertTrue(backup_path.is_file())
            with connect_sqlite(backup_path) as backup:
                self.assertEqual(backup.execute("SELECT value FROM old_probe").fetchone()[0], "kept")
                self.assertFalse(self.marker(backup, ASSET_ID_KEY_MIGRATION_ID))
                self.assertFalse(self.marker(backup, ASSET_ID_BLOCKING_STATE_MIGRATION_ID))
            with connect_sqlite(path) as migrated:
                self.assertTrue(self.marker(migrated, ASSET_ID_KEY_MIGRATION_ID))
                self.assertTrue(self.marker(migrated, ASSET_ID_BLOCKING_STATE_MIGRATION_ID))
                columns = {row[1] for row in migrated.execute("PRAGMA table_info(gd_resource_reservations)")}
                self.assertIn("asset_id_key", columns)

    def test_key_enabled_database_gets_distinct_pre_migration_backup_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "key-enabled.sqlite3"
            database = Database(path)
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('preserved','legacy','manual:preserved','cs2',730,'2','Bot-A','000123',1,'active')"
                )
                connection.execute(
                    "DELETE FROM gd_schema_migrations WHERE migration_id=?",
                    (ASSET_ID_BLOCKING_STATE_MIGRATION_ID,),
                )
                self.drop_blocking_guards(connection)

            backup_path = self.backup_path(path, ASSET_ID_BLOCKING_STATE_BACKUP_SUFFIX)
            key_backup_path = self.backup_path(path, ASSET_ID_KEY_BACKUP_SUFFIX)
            key_backup_path.write_bytes(b"previous asset-key backup marker")
            self.assertFalse(backup_path.exists())
            database.initialize()
            self.assertTrue(backup_path.is_file())
            self.assertEqual(key_backup_path.read_bytes(), b"previous asset-key backup marker")
            self.assertNotEqual(backup_path, key_backup_path)
            first_backup = backup_path.read_bytes()
            with connect_sqlite(backup_path) as backup:
                self.assertTrue(self.marker(backup, ASSET_ID_KEY_MIGRATION_ID))
                self.assertFalse(self.marker(backup, ASSET_ID_BLOCKING_STATE_MIGRATION_ID))
                self.assertEqual(backup.execute(
                    "SELECT asset_id FROM gd_resource_reservations WHERE reservation_id='preserved'"
                ).fetchone()[0], "000123")
            with database.connect() as connection:
                self.assertTrue(self.marker(connection, ASSET_ID_BLOCKING_STATE_MIGRATION_ID))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                    "AND name IN ('gd_asset_reservations_guard_insert','gd_asset_reservations_guard_update')"
                ).fetchone()[0], 2)

            database.initialize()
            self.assertEqual(backup_path.read_bytes(), first_backup)
            with database.connect() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM gd_schema_migrations WHERE migration_id=?",
                    (ASSET_ID_BLOCKING_STATE_MIGRATION_ID,),
                ).fetchone()[0], 1)

    def test_backup_failure_stops_before_blocking_state_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "backup-failure.sqlite3"
            database = Database(path)
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "DELETE FROM gd_schema_migrations WHERE migration_id=?",
                    (ASSET_ID_BLOCKING_STATE_MIGRATION_ID,),
                )
                self.drop_blocking_guards(connection)

            original_backup = db_module.ensure_pre_migration_backup

            def fail_for_guard_migration(
                database_path, *, migration_id="2026-11-delivery-ledger-v1",
                backup_suffix=".pre-gamedelivery-ledger-v1.bak",
            ):
                if migration_id == ASSET_ID_BLOCKING_STATE_MIGRATION_ID:
                    raise OSError("synthetic backup failure")
                return original_backup(
                    database_path, migration_id=migration_id, backup_suffix=backup_suffix
                )

            with patch.object(db_module, "ensure_pre_migration_backup", side_effect=fail_for_guard_migration):
                with self.assertRaisesRegex(OSError, "synthetic backup failure"):
                    database.initialize()
            with database.connect() as connection:
                self.assertFalse(self.marker(connection, ASSET_ID_BLOCKING_STATE_MIGRATION_ID))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                    "AND name IN ('gd_asset_reservations_guard_insert','gd_asset_reservations_guard_update')"
                ).fetchone()[0], 0)

    def test_trigger_migration_rolls_back_marker_and_ddl_then_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "rollback.sqlite3")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('preserved','legacy','manual:preserved','cs2',730,'2','Bot-A','000123',1,'active')"
                )
                connection.execute(
                    "DELETE FROM gd_schema_migrations WHERE migration_id=?",
                    (ASSET_ID_BLOCKING_STATE_MIGRATION_ID,),
                )
                self.drop_blocking_guards(connection)
                connection.execute(f"""
                    CREATE TRIGGER fail_guard_migration_marker
                    BEFORE INSERT ON gd_schema_migrations
                    WHEN NEW.migration_id='{ASSET_ID_BLOCKING_STATE_MIGRATION_ID}'
                    BEGIN SELECT RAISE(ABORT,'synthetic marker failure'); END
                """)
                connection.commit()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic marker failure"):
                    apply_delivery_ledger_migrations(connection)
                self.assertFalse(self.marker(connection, ASSET_ID_BLOCKING_STATE_MIGRATION_ID))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                    "AND name IN ('gd_asset_reservations_guard_insert','gd_asset_reservations_guard_update')"
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT asset_id FROM gd_resource_reservations WHERE reservation_id='preserved'"
                ).fetchone()[0], "000123")
                connection.execute("DROP TRIGGER fail_guard_migration_marker")
                connection.commit()
                self.assertTrue(apply_delivery_ledger_migrations(connection))
                self.assertTrue(self.marker(connection, ASSET_ID_BLOCKING_STATE_MIGRATION_ID))
                self.assertFalse(apply_delivery_ledger_migrations(connection))

    def test_alias_conflict_keeps_rows_and_both_backups_before_refusing_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "alias-conflict.sqlite3"
            database = Database(path)
            database.initialize()
            with database.connect() as connection:
                connection.execute("DROP INDEX idx_gd_resource_active_asset_key")
                connection.execute("DELETE FROM gd_schema_migrations WHERE migration_id IN (?,?)", (
                    ASSET_ID_KEY_MIGRATION_ID, ASSET_ID_BLOCKING_STATE_MIGRATION_ID,
                ))
                self.drop_blocking_guards(connection)
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('alias-a','legacy','manual:a','cs2',730,'2','Bot-A','123',1,'active')"
                )
                connection.execute(
                    "INSERT INTO gd_resource_reservations "
                    "(reservation_id,owner_type,legacy_owner_id,game_id,app_id,context_id,bot_name,asset_id,units,state) "
                    "VALUES('alias-b','legacy','manual:b','cs2',730,'2','Bot-A','00123',1,'active')"
                )

            with self.assertRaisesRegex(sqlite3.IntegrityError, "alias conflicts"):
                database.initialize()
            for suffix in (ASSET_ID_KEY_BACKUP_SUFFIX, ASSET_ID_BLOCKING_STATE_BACKUP_SUFFIX):
                backup_path = self.backup_path(path, suffix)
                self.assertTrue(backup_path.is_file())
                with connect_sqlite(backup_path) as backup:
                    rows = [tuple(row) for row in backup.execute(
                        "SELECT reservation_id,legacy_owner_id,asset_id,state "
                        "FROM gd_resource_reservations WHERE reservation_id LIKE 'alias-%' ORDER BY reservation_id"
                    )]
                    self.assertEqual(rows, [
                        ("alias-a", "manual:a", "123", "active"),
                        ("alias-b", "manual:b", "00123", "active"),
                    ])
            with database.connect() as connection:
                self.assertFalse(self.marker(connection, ASSET_ID_KEY_MIGRATION_ID))
                self.assertFalse(self.marker(connection, ASSET_ID_BLOCKING_STATE_MIGRATION_ID))
                self.assertEqual([tuple(row) for row in connection.execute(
                    "SELECT reservation_id,legacy_owner_id,asset_id,state "
                    "FROM gd_resource_reservations WHERE reservation_id LIKE 'alias-%' ORDER BY reservation_id"
                )], [
                    ("alias-a", "manual:a", "123", "active"),
                    ("alias-b", "manual:b", "00123", "active"),
                ])


if __name__ == "__main__":
    unittest.main()
