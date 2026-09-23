"""Фикстуры: один локальный сервер на сессию, журнал запросов чистится перед каждым тестом."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from tests.local_server import LocalServer


@pytest.fixture(scope="session")
def _server() -> Iterator[LocalServer]:
    server = LocalServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def server(_server: LocalServer) -> LocalServer:
    _server.requests.clear()
    return _server
