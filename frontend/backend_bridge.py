"""Read-only JSONL bridge for capability and order snapshots."""
from __future__ import annotations
import importlib.util, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None

def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            method = request.get("method")
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "error": "invalid_request"}), flush=True)
            continue
        if method == "health":
            result = {"ok": True, "service": "python_bridge"}
        elif method == "capabilities":
            result = {"ok": True, "capabilities": [
                {"id":"orders","state":"available" if available("core.orders.service") else "unavailable"},
                {"id":"playerok","state":"available" if available("integrations.playerok.client") else "unavailable"},
                {"id":"asf","state":"available" if available("integrations.asf.client") else "unavailable"},
                {"id":"operator.digital_codes","state":"available" if available("core.delivery.digital_codes") else "unavailable"},
                {"id":"operator.files","state":"available" if available("core.delivery.file_catalog") else "unavailable"},
                {"id":"operator.services","state":"available" if available("core.delivery.service_tasks") else "unavailable"},
                {"id":"telegram","state":"available" if available("integrations.telegram.outbox") else "unavailable"},
                {"id":"proxies.openskin","state":"available" if available("integrations.openskin.client") else "unavailable"},
            ]}
        elif method == "asf.inventory":
            try:
                from integrations.asf.client import ASFClient
                bot = request.get("bot")
                app_id = request.get("app_id")
                context_id = request.get("context_id")
                if not isinstance(bot, str) or not bot.strip() or (app_id, context_id) not in {(753, 6), (730, 2)}:
                    result = {"ok": False, "error": "invalid_request"}
                else:
                    result = {"ok": True, "inventory": ASFClient(base_url=os.environ.get("ASF_IPC_BASE_URL", "http://127.0.0.1:1242"), ipc_password=os.environ.get("ASF_IPC_PASSWORD") or None).get_bot_inventory(bot, app_id=app_id, context_id=context_id)}
            except Exception as exc:
                result = {"ok": False, "error": "asf_unavailable", "type": type(exc).__name__}
        elif method == "asf.status":
            try:
                from integrations.asf.client import ASFClient
                result = {"ok": True, "status": ASFClient(base_url=os.environ.get("ASF_IPC_BASE_URL", "http://127.0.0.1:1242"), ipc_password=os.environ.get("ASF_IPC_PASSWORD") or None).get_status()}
            except Exception as exc:
                result = {"ok": False, "error": "asf_unavailable", "type": type(exc).__name__}
        elif method == "operator.summary":
            try:
                from core.database.db import Database
                db_path = os.environ.get("GAMEDELIVERY_DATABASE_PATH", str(Path.home() / ".gamedelivery" / "gamedelivery.sqlite3"))
                with Database(db_path).connect() as db:
                    tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type=\"table\"").fetchall()}
                    def count(table: str) -> int:
                        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if table in tables else 0
                    result = {"ok": True, "digital_codes": count("gd_digital_codes"), "files": count("gd_files"), "service_units": count("gd_service_units")}
            except Exception as exc:
                result = {"ok": False, "error": "backend_error", "type": type(exc).__name__}
        elif method == "orders.list":
            try:
                from core.database.db import Database
                from core.orders.service import OrderService
                db_path = os.environ.get("GAMEDELIVERY_DATABASE_PATH", str(Path.home() / ".gamedelivery" / "gamedelivery.sqlite3"))
                result = {"ok": True, "orders": OrderService(Database(db_path)).list_orders()}
            except Exception as exc:
                result = {"ok": False, "error": "backend_error", "type": type(exc).__name__}
        else:
            result = {"ok": False, "error": "not_implemented"}
        print(json.dumps(result, ensure_ascii=False), flush=True)
if __name__ == "__main__": main()
