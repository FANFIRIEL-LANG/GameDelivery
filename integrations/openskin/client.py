"""Minimal client for the public OpenSkin v1 CS2 price batch endpoint."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import base64
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import time
import traceback
from urllib.parse import quote, quote_plus
import urllib.error
import urllib.request
from typing import Callable, Mapping

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl
from PySide6.QtNetwork import (
    QAuthenticator, QNetworkAccessManager, QNetworkProxy, QNetworkReply,
    QNetworkRequest,
)

from integrations.openskin.proxy import OpenSkinProxy


_ENDPOINT = "https://api.openskin.dev/v1/prices/batch"
_PROXY_PROBE_URL = "https://api.openskin.dev/health"
_MAX_BATCH = 500
_logger = logging.getLogger(__name__)


def configure_openskin_diagnostics(log_path: Path | str | None = None):
    """Persist bounded, credential-redacted OpenSkin diagnostics for the app log tab."""
    if any(getattr(handler, "_gamedelivery_openskin", False)
           for handler in _logger.handlers):
        return None
    path = Path(log_path) if log_path is not None else Path.home() / ".gamedelivery" / "openskin-proxy.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8",
        )
    except OSError:
        return None
    handler._gamedelivery_openskin = True
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    return handler


@dataclass(frozen=True, order=True)
class PricePosition:
    appid: int
    market_hash_name: str
    pattern: str | None = None

    def __post_init__(self):
        if type(self.appid) is not int or self.appid != 730:
            raise ValueError("OpenSkin pricing is limited to CS2 AppID 730")
        if not isinstance(self.market_hash_name, str) or not self.market_hash_name.strip():
            raise ValueError("market_hash_name must be a non-empty string")
        if self.pattern is not None and (
            not isinstance(self.pattern, str) or not self.pattern.strip()
        ):
            raise ValueError("pattern must be a non-empty string or None")


@dataclass(frozen=True)
class OpenSkinPrice:
    position: PricePosition
    steam_ask: Decimal | None
    currency: str | None
    source_updated_at: str | None


class OpenSkinError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None,
                 retry_after_seconds: int | None = None, category: str = "unknown",
                 diagnostic_detail: str = ""):
        super().__init__(message)
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.category = category
        self.diagnostic_detail = diagnostic_detail

    @property
    def user_message(self) -> str:
        return str(self)


def classify_qnetwork_error(error, status_code: int, proxy_scheme: str,
                            *, timed_out: bool = False,
                            request_sent: bool | None = None,
                            tls_completed: bool = False,
                            proxy_auth_requested: bool = False,
                            proxy_auth_answered: bool = False) -> tuple[str, str]:
    """Classify Qt transport using observed authentication, TLS and request milestones."""
    if status_code == 407 or error == QNetworkReply.NetworkError.ProxyAuthenticationRequiredError:
        return "proxy_auth", "Прокси отклонил авторизацию (HTTP 407). Проверь логин и пароль."
    if status_code == 403:
        return "http_403", "HTTPS-маршрут установлен, но OpenSkin вернул HTTP 403."
    if status_code == 429:
        return "http_429", "OpenSkin ограничил частоту запросов (HTTP 429)."
    if 500 <= status_code <= 599:
        return "http_5xx", f"OpenSkin временно недоступен (HTTP {status_code})."
    if status_code >= 400:
        return "http_error", f"Удалённая сторона вернула HTTP {status_code}."

    network_error = QNetworkReply.NetworkError
    if timed_out:
        if request_sent:
            return "response_timeout", "Запрос отправлен, но удалённый сервер не ответил вовремя."
        if proxy_auth_requested and not proxy_auth_answered:
            return "proxy_auth_timeout", "Прокси запросил авторизацию, но ответ на запрос авторизации не был отправлен вовремя."
        if tls_completed:
            return "pre_response_timeout", "TLS-соединение установлено, но HTTP-ответ не получен вовремя."
        if proxy_scheme == "socks5":
            return "socks5_or_tls_timeout", (
                "Не завершилось соединение через SOCKS5 или TLS. Qt не подтвердил завершение TLS; "
                "точный этап сбоя не установлен."
            )
        return "connect_or_tls_timeout", (
            "Не завершилось соединение через HTTP CONNECT или TLS. Qt не подтвердил завершение TLS; "
            "это не доказывает, что завис именно proxy-туннель."
        )
    if error == network_error.ProxyNotFoundError:
        return "proxy_dns", "Не удалось разрешить DNS-имя proxy-сервера."
    if error == network_error.HostNotFoundError:
        return "dns", "Не удалось разрешить DNS-имя удалённого сервера."
    if error == network_error.ProxyConnectionRefusedError:
        return "proxy_tcp", "Proxy-сервер отклонил TCP-соединение."
    if error == network_error.ConnectionRefusedError:
        return "tcp", "Удалённый сервер отклонил TCP-соединение через proxy."
    if error == network_error.ProxyTimeoutError:
        return "proxy_timeout", "Время ожидания ответа proxy-сервера истекло."
    if error == network_error.ProxyAuthenticationRequiredError:
        return "proxy_auth", "Proxy-сервер отклонил авторизацию."
    if error == network_error.SslHandshakeFailedError:
        return "tls", "Не удалось выполнить TLS handshake или проверить сертификат."
    if error == network_error.TimeoutError:
        return "response_timeout", "Истекло время ожидания ответа удалённого сервера."
    if error in {
        network_error.ProxyConnectionClosedError,
        network_error.UnknownProxyError,
    }:
        if proxy_scheme == "socks5":
            return "socks5_handshake", "Не завершился SOCKS5 handshake или подключение через SOCKS5."
        return "http_tunnel", "Не удалось установить HTTP CONNECT tunnel."
    if error == network_error.ProtocolFailure:
        return "invalid_response", "Получен некорректный сетевой ответ."
    if error in {network_error.InternalServerError, network_error.ServiceUnavailableError,
                 network_error.UnknownServerError}:
        return "http_5xx", "OpenSkin API вернул ошибку сервера."
    return "network_error", "Сетевой запрос не завершился успешно."


def redact_network_detail(detail: str, proxy: OpenSkinProxy | None = None) -> str:
    """Remove raw and URL-encoded proxy credentials before diagnostics are stored."""
    text = " ".join(str(detail or "").split())
    if proxy is not None:
        secrets = [proxy.username, proxy.password]
        if proxy.username is not None and proxy.password is not None:
            secrets.append(base64.b64encode(
                f"{proxy.username}:{proxy.password}".encode("utf-8")
            ).decode("ascii"))
        for secret in filter(None, secrets):
            variants = {secret, quote(secret, safe=""), quote_plus(secret)}
            for variant in variants:
                if variant:
                    text = text.replace(variant, "[redacted]")
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    return text[:400]


def _log_network_failure(category: str, qt_error: str, detail: str,
                         proxy: OpenSkinProxy | None) -> str:
    safe_detail = redact_network_detail(detail, proxy)
    _logger.warning(
        "OpenSkin transport diagnostic category=%s qt_error=%s detail=%s",
        category, qt_error, safe_detail,
    )
    return safe_detail

def _log_unexpected_exception(context: str, exc: Exception,
                               proxy: OpenSkinProxy | None) -> str:
    """Log safe traceback frame locations and redacted exception text only.

    Do not log locals or exception-info objects: either can expose credentials.
    """
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    frame_summary = " -> ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames[-10:]
    ) or "unavailable"
    detail = redact_network_detail(str(exc), proxy) or "<empty>"
    safe_detail = (
        f"context={context}; exception={type(exc).__name__}; "
        f"traceback={frame_summary}; detail={detail}"
    )
    _logger.warning("OpenSkin unexpected failure %s", safe_detail)
    return safe_detail


def _read_qnetwork_response_headers(reply: QNetworkReply) -> dict[str, str]:
    """Read response headers using the str overload exposed by PySide6."""
    headers: dict[str, str] = {}
    for raw_name in reply.rawHeaderList():
        name = bytes(raw_name).decode("latin-1")
        headers[name] = bytes(reply.rawHeader(name)).decode("latin-1")
    return headers

def _retry_after(headers: Mapping[str, str] | None) -> int | None:
    if not headers:
        return None
    value = next((v for k, v in headers.items() if k.casefold() == "retry-after"), None)
    if value is None:
        return None
    try:
        return max(0, int(value.strip()))
    except (AttributeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, int((date - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _url_request(url: str, body: bytes, headers: Mapping[str, str], timeout: float):
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        # Never read or include an API error body.
        category, message = classify_qnetwork_error(
            QNetworkReply.NetworkError.NoError, exc.code, "http"
        )
        raise OpenSkinError(
            message, http_status=exc.code, category=category,
            retry_after_seconds=_retry_after(exc.headers),
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise OpenSkinError(
            "Не удалось получить цены OpenSkin.", category="network_error"
        ) from None


def build_qnetwork_proxy(proxy: OpenSkinProxy) -> QNetworkProxy:
    """Create a request-manager-local HTTP CONNECT or SOCKS5 proxy."""
    proxy_type = (
        QNetworkProxy.ProxyType.Socks5Proxy
        if proxy.scheme == "socks5"
        else QNetworkProxy.ProxyType.HttpProxy
    )
    result = QNetworkProxy(proxy_type, proxy.host, proxy.port)
    if proxy.username is not None:
        result.setUser(proxy.username)
        result.setPassword(proxy.password or "")
    return result


def _set_qt_request_headers(request, headers: Mapping[str, str]):
    """Set request headers and avoid Qt's implicit gzip decompression path.

    Qt automatically advertises gzip when Accept-Encoding is absent and then
    transparently decompresses the reply. Explicit identity keeps the response
    bytes intact and avoids the zero-byte gzip response observed with the
    packaged proxy transport.
    """
    has_accept_encoding = False
    for name, value in headers.items():
        if name.casefold() == "accept-encoding":
            has_accept_encoding = True
        if name.casefold() == "content-type":
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, value)
        else:
            request.setRawHeader(name.encode("ascii"), str(value).encode("latin-1"))
    if not has_accept_encoding:
        request.setRawHeader(b"Accept-Encoding", b"identity")


def _qt_proxy_request(url: str, body: bytes, headers: Mapping[str, str],
                      timeout: float, proxy: OpenSkinProxy, *, method: str = "POST"):
    """Use one manager-local proxy for the whole request, including HTTPS CONNECT."""
    if QCoreApplication.instance() is None:
        raise OpenSkinError("Проверка прокси OpenSkin доступна только из приложения.")
    ssl_error_details: list[str] = []
    manager = QNetworkAccessManager()
    # This is intentionally manager-local and happens before get()/post().
    manager.setProxy(build_qnetwork_proxy(proxy))
    phase = {
        "proxy_auth_requested": False,
        "proxy_auth_answered": False,
        "tls_completed": False,
        "request_sent": False,
        "metadata_received": False,
        "download_received": 0,
        "download_total": -1,
    }

    def apply_auth(_proxy, authenticator: QAuthenticator):
        phase["proxy_auth_requested"] = True
        if proxy.username is not None:
            authenticator.setUser(proxy.username)
            authenticator.setPassword(proxy.password or "")
            phase["proxy_auth_answered"] = True

    def mark_tls_completed(*_args):
        phase["tls_completed"] = True

    def mark_ssl_errors(*args):
        errors = args[-1] if args else ()
        ssl_error_details.extend(error.errorString() for error in errors)

    manager.proxyAuthenticationRequired.connect(apply_auth)
    encrypted_signal = getattr(manager, "encrypted", None)
    if encrypted_signal is not None:
        encrypted_signal.connect(mark_tls_completed)
    manager_ssl_signal = getattr(manager, "sslErrors", None)
    if manager_ssl_signal is not None:
        manager_ssl_signal.connect(mark_ssl_errors)
    request = QNetworkRequest(QUrl(url))
    _set_qt_request_headers(request, headers)
    if method == "HEAD":
        reply = manager.head(request)
    elif method == "GET":
        reply = manager.get(request)
    else:
        reply = manager.post(request, body)
    reply.sslErrors.connect(
        lambda errors: ssl_error_details.extend(error.errorString() for error in errors)
    )
    event_loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timed_out = []

    def abort_request():
        timed_out.append(True)
        reply.abort()
        event_loop.quit()

    reply.finished.connect(event_loop.quit)
    reply.requestSent.connect(lambda: phase.__setitem__("request_sent", True))
    response_chunks = bytearray()

    def collect_reply_data():
        chunk = bytes(reply.readAll())
        if chunk:
            response_chunks.extend(chunk)

    ready_read_signal = getattr(reply, "readyRead", None)
    if ready_read_signal is not None:
        ready_read_signal.connect(collect_reply_data)
    metadata_signal = getattr(reply, "metaDataChanged", None)
    if metadata_signal is not None:
        metadata_signal.connect(lambda: phase.__setitem__("metadata_received", True))
    download_progress_signal = getattr(reply, "downloadProgress", None)
    if download_progress_signal is not None:
        download_progress_signal.connect(
            lambda received, total: (
                phase.__setitem__("download_received", int(received)),
                phase.__setitem__("download_total", int(total)),
            )
        )
    timer.timeout.connect(abort_request)
    # A reply may finish before its finished signal is observed; never wait on a missed signal.
    if not reply.isFinished():
        timer.start(max(1, int(timeout * 1000)))
        try:
            event_loop.exec()
        finally:
            timer.stop()
    try:
        raw_status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        status = int(raw_status) if raw_status is not None else 0
        response_headers = _read_qnetwork_response_headers(reply)
        qt_error = reply.error()
        bytes_available = getattr(reply, "bytesAvailable", None)
        bytes_available_before_read = (
            int(bytes_available()) if callable(bytes_available) else -1
        )
        response_body = b""
        if method == "POST" and status == 200:
            # Drain once more after finished/abort in case data arrived between
            # readyRead notifications and the final state inspection.
            collect_reply_data()
            response_body = bytes(response_chunks)
        response_complete = (
            phase["download_total"] > 0
            and phase["download_received"] >= phase["download_total"]
        )
        if method == "POST" and status == 200 and timed_out and not response_complete:
            _logger.warning(
                "OpenSkin response body timed out before completion "
                "status=200 qt_error=%s bytes_read=%s download_received=%s download_total=%s",
                getattr(qt_error, "name", str(qt_error)), len(response_body),
                phase["download_received"], phase["download_total"],
            )
            raise OpenSkinError(
                "OpenSkin прислал HTTP-заголовки, но тело ответа не загрузилось полностью за отведённое время. "
                "Проверь доступность API через прокси и повтори позже.",
                http_status=200, category="response_timeout",
                diagnostic_detail="HTTP 200 headers received; response body incomplete at timeout",
            )
        if method == "POST" and status == 200 and timed_out and response_complete:
            _logger.info(
                "OpenSkin response body recovered after Qt timeout "
                "bytes_read=%s download_received=%s download_total=%s",
                len(response_body), phase["download_received"], phase["download_total"],
            )
        if method == "POST" and status == 200 and not response_body:
            def response_header(name):
                value = next(
                    (value for key, value in response_headers.items()
                     if key.casefold() == name.casefold()),
                    "missing",
                )
                return redact_network_detail(value, proxy)
            _logger.warning(
                "OpenSkin proxy returned an empty HTTP response "
                "status=%s qt_error=%s bytes_available=%s download_received=%s "
                "download_total=%s request_sent=%s metadata_received=%s "
                "content_length=%s transfer_encoding=%s content_encoding=%s content_type=%s",
                status, getattr(qt_error, "name", str(qt_error)),
                bytes_available_before_read, phase["download_received"],
                phase["download_total"], phase["request_sent"],
                phase["metadata_received"], response_header("content-length"),
                response_header("transfer-encoding"), response_header("content-encoding"),
                response_header("content-type"),
            )
        if status == 0:
            category, message = classify_qnetwork_error(
                qt_error, status, proxy.scheme, timed_out=bool(timed_out),
                request_sent=phase["request_sent"] if timed_out else None,
                tls_completed=phase["tls_completed"],
                proxy_auth_requested=phase["proxy_auth_requested"],
                proxy_auth_answered=phase["proxy_auth_answered"],
            )
            detail = " | ".join(ssl_error_details) or reply.errorString()
            phase_detail = (
                f"tls_completed={phase['tls_completed']}; "
                f"proxy_auth_requested={phase['proxy_auth_requested']}; "
                f"proxy_auth_answered={phase['proxy_auth_answered']}; "
                f"request_sent={phase['request_sent']}; "
                f"metadata_received={phase['metadata_received']}"
            )
            safe_detail = _log_network_failure(
                category, getattr(qt_error, "name", str(qt_error)),
                f"{phase_detail}; {detail}", proxy
            )
            raise OpenSkinError(
                message, category=category, diagnostic_detail=safe_detail
            )
        return status, response_body, response_headers
    finally:
        reply.deleteLater()
        manager.deleteLater()


def _proxy_url_request(url: str, body: bytes, headers: Mapping[str, str],
                       timeout: float, proxy: OpenSkinProxy):
    return _qt_proxy_request(url, body, headers, timeout, proxy, method="POST")


def probe_proxy_connection(proxy: OpenSkinProxy, *, timeout: float = 15.0,
                           request_func: Callable | None = None) -> int:
    """Test proxy auth, HTTPS CONNECT/SOCKS and TLS before testing API behavior.

    Any HTTP response other than 407 means the proxy route completed DNS,
    proxy negotiation, HTTPS tunnel and TLS. The response body is discarded.
    """
    request_func = request_func or _qt_proxy_request
    try:
        status, _body, _headers = request_func(
            _PROXY_PROBE_URL, b"", {"Accept": "*/*", "Range": "bytes=0-0"},
            timeout, proxy, method="GET"
        )
    except OpenSkinError:
        raise
    except Exception as exc:
        _log_unexpected_exception("proxy_probe", exc, proxy)
        raise
    if status == 407:
        category, message = classify_qnetwork_error(
            QNetworkReply.NetworkError.ProxyAuthenticationRequiredError,
            status, proxy.scheme,
        )
        _logger.warning("OpenSkin proxy probe failed category=%s http_status=%s", category, status)
        raise OpenSkinError(message, http_status=status, category=category)
    if status == 0:
        raise OpenSkinError("Proxy-проверка не получила HTTP-ответа.", category="network_error")
    _logger.info("OpenSkin proxy probe received an HTTP response: status=%s", status)
    return status


class OpenSkinClient:
    """Sends sequential batches and reads only steam.ask from ordered results."""

    def __init__(self, *, timeout: float = 45.0,
                 request_func: Callable | None = None,
                 proxy_request_func: Callable | None = None,
                 proxy: OpenSkinProxy | None = None):
        self.timeout = timeout
        self._request_func = request_func or _url_request
        self._proxy_request_func = proxy_request_func or _proxy_url_request
        self.proxy = proxy

    def fetch_prices(self, positions) -> tuple[OpenSkinPrice, ...]:
        positions = tuple(positions)
        if any(not isinstance(position, PricePosition) for position in positions):
            raise TypeError("positions must contain PricePosition values")
        result = []
        for start in range(0, len(positions), _MAX_BATCH):
            batch = positions[start:start + _MAX_BATCH]
            result.extend(self._fetch_batch(batch))
        return tuple(result)

    def _fetch_batch(self, positions: tuple[PricePosition, ...]) -> tuple[OpenSkinPrice, ...]:
        items = [
            position.market_hash_name if position.pattern is None else {
                "item": position.market_hash_name, "pattern": position.pattern,
            }
            for position in positions
        ]
        body = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
        try:
            request_func = self._proxy_request_func if self.proxy else self._request_func
            args = (_ENDPOINT, body, {"Accept": "application/json",
                                     "Content-Type": "application/json"}, self.timeout)
            status, raw, headers = (
                request_func(*args, self.proxy) if self.proxy else request_func(*args)
            )
        except OpenSkinError:
            raise
        except Exception as exc:
            diagnostic = _log_unexpected_exception("price_request", exc, self.proxy)
            raise OpenSkinError("Не удалось получить цены OpenSkin.",
                                category="client_error",
                                diagnostic_detail=diagnostic) from None
        if status != 200:
            category, message = classify_qnetwork_error(
                QNetworkReply.NetworkError.NoError, status,
                self.proxy.scheme if self.proxy else "http",
            )
            _logger.warning("OpenSkin API response category=%s http_status=%s", category, status)
            raise OpenSkinError(
                message, http_status=status, category=category,
                retry_after_seconds=_retry_after(headers),
            )
        try:
            payload = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal)
        except UnicodeDecodeError as exc:
            content_type = next((v for k, v in headers.items() if k.casefold() == "content-type"), "unknown")
            content_encoding = next((v for k, v in headers.items() if k.casefold() == "content-encoding"), "identity")
            _logger.warning(
                "OpenSkin API returned non-UTF8 response content_type=%s content_encoding=%s "
                "body_bytes=%s decode_error_start=%s prefix_hex=%s suffix_hex=%s",
                redact_network_detail(content_type, self.proxy),
                redact_network_detail(content_encoding, self.proxy),
                len(raw), exc.start, raw[:16].hex(), raw[-16:].hex(),
            )
            raise OpenSkinError("OpenSkin вернул ответ в неожиданной кодировке.",
                                category="invalid_response") from None
        except json.JSONDecodeError as exc:
            content_type = next((v for k, v in headers.items() if k.casefold() == "content-type"), "unknown")
            content_encoding = next((v for k, v in headers.items() if k.casefold() == "content-encoding"), "identity")
            _logger.warning(
                "OpenSkin API returned invalid JSON content_type=%s content_encoding=%s "
                "body_bytes=%s json_error=%s json_error_pos=%s json_error_line=%s "
                "json_error_column=%s prefix_hex=%s suffix_hex=%s",
                redact_network_detail(content_type, self.proxy),
                redact_network_detail(content_encoding, self.proxy),
                len(raw), exc.msg, exc.pos, exc.lineno, exc.colno,
                raw[:16].hex(), raw[-16:].hex(),
            )
            raise OpenSkinError("OpenSkin вернул ответ в неожиданном формате.",
                                category="invalid_response") from None
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) != len(positions):
            _logger.warning("OpenSkin API returned an invalid results collection")
            raise OpenSkinError("OpenSkin вернул неполный список результатов.",
                                category="invalid_response")

        prices = []
        for position, row in zip(positions, rows):
            if not isinstance(row, dict) or row.get("item") != position.market_hash_name:
                raise OpenSkinError("OpenSkin вернул результат с несовпадающим предметом.")
            echoed_pattern = row.get("pattern")
            if position.pattern is not None and echoed_pattern != position.pattern:
                raise OpenSkinError("OpenSkin вернул результат с несовпадающим pattern.")
            if position.pattern is None and echoed_pattern not in (None, ""):
                raise OpenSkinError("OpenSkin вернул pattern без запроса точного варианта.")

            steam = row.get("steam")
            if not isinstance(steam, dict):
                prices.append(OpenSkinPrice(position, None, None, None))
                continue
            ask = steam.get("ask")
            if isinstance(ask, bool) or not isinstance(ask, (Decimal, int)):
                ask = None
            else:
                try:
                    ask = Decimal(ask) if isinstance(ask, int) else ask
                    if not ask.is_finite() or ask < 0:
                        ask = None
                except (InvalidOperation, ValueError):
                    ask = None
            currency = steam.get("currency")
            currency = currency.upper() if isinstance(currency, str) else None
            updated = steam.get("updated_at")
            if isinstance(updated, str):
                try:
                    parsed_updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    if parsed_updated.tzinfo is None or parsed_updated.utcoffset() is None:
                        updated = None
                except ValueError:
                    updated = None
            else:
                updated = None
            prices.append(OpenSkinPrice(position, ask, currency, updated))
        return tuple(prices)
