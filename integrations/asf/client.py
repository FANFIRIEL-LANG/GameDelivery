from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ASFError(RuntimeError):
    """Raised when ASF IPC cannot complete a request."""


@dataclass(frozen=True)
class ASFClient:
    """Minimal client for the local, official ASF IPC API.

    Keep IPC bound to localhost. Configure IPCPassword in ASF and pass it
    to this client from local settings; never commit it or write it to logs.
    """

    base_url: str = "http://127.0.0.1:1242"
    ipc_password: str | None = None
    timeout: float = 5.0

    def request(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None
    ) -> Any:
        if not path.startswith("/Api/"):
            raise ValueError("Разрешены только пути ASF API, начинающиеся с /Api/")
        if "://" in path or "\n" in path or "\r" in path:
            raise ValueError("Некорректный путь ASF API")

        url = self.base_url.rstrip("/") + path
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.ipc_password:
            headers["Authentication"] = self.ipc_password

        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ASFError("ASF вернул ответ не в формате JSON") from exc
        except HTTPError as exc:
            # Do not include request headers or URL query strings in the error.
            if exc.code == 401:
                raise ASFError("ASF IPC отклонил авторизацию; проверь IPCPassword") from None
            if exc.code == 403:
                raise ASFError("ASF IPC временно заблокировал запросы или запретил доступ") from None
            raise ASFError(f"ASF IPC вернул HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise ASFError("ASF IPC недоступен на локальном компьютере") from None

    def get_status(self) -> Any:
        """Read ASF process status using its documented API endpoint."""
        return self.request("/Api/ASF")

    def get_bot(self, bot_name: str) -> Any:
        """Read one bot's status; the name is URL-encoded as a single path segment."""
        from urllib.parse import quote

        if not bot_name.strip():
            raise ValueError("Имя бота не должно быть пустым")
        return self.request("/Api/Bot/" + quote(bot_name, safe=""))

    def send_command(self, command: str) -> Any:
        """Send a permitted ASF command through IPC.

        This is a low-level primitive, not an automatic trade sender. Callers
        must validate commands and require an explicit confirmation for actions
        that move items. Consult the running ASF /swagger for exact API schemas.
        """
        if not command.strip():
            raise ValueError("Команда ASF не должна быть пустой")
        return self.request("/Api/Command", method="POST", body={"Command": command})
