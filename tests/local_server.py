"""Локальный HTTP-сервер для тестов: никаких обращений во внешний интернет."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILE_SIZE = 1_234_567  # «некруглый» размер, чтобы ошибка на чанк не спряталась
SLOW_DELAY = 1.0
CHUNK = 64 * 1024


@dataclass(frozen=True)
class SeenRequest:
    path: str
    client_port: int
    headers: dict[str, str]


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[SeenRequest] = []

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def url(self, path: str) -> str:
        return self.base_url + path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # иначе keep-alive невозможен
    server: LocalServer

    def do_GET(self) -> None:
        self.server.requests.append(
            SeenRequest(self.path, self.client_address[1], dict(self.headers.items()))
        )
        route = self.path.split("?", 1)[0]
        match route:
            case "/file":
                self._send_body(FILE_SIZE, content_length=True)
            case "/no-length":
                # Без Content-Length конец тела — закрытие соединения.
                self._send_body(FILE_SIZE, content_length=False)
            case "/redirect":
                self.send_response(302)
                self.send_header("Location", "/file")
                self.send_header("Content-Length", "0")
                self.end_headers()
            case "/slow":
                time.sleep(SLOW_DELAY)
                self._send_body(10, content_length=True)
            case "/truncated":
                self.send_response(200)
                self.send_header("Content-Length", str(FILE_SIZE))
                self.end_headers()
                self.wfile.write(b"x" * (FILE_SIZE // 2))
                self.close_connection = True
            case "/echo":
                body = json.dumps({"path": self.path, "headers": dict(self.headers.items())})
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            case _:
                self.send_error(404, "Not Found")

    def _send_body(self, size: int, *, content_length: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if content_length:
            self.send_header("Content-Length", str(size))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        remaining = size
        while remaining > 0:
            n = min(CHUNK, remaining)
            self.wfile.write(b"\0" * n)
            remaining -= n

    def log_message(self, format: str, *args: object) -> None:
        pass
