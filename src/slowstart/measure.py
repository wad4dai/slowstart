"""Логика замеров.

Модуль ничего не печатает и не знает про Rich: он делает запросы и возвращает
dataclass'ы. О ходе скачивания сообщает через необязательные callback'и.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx

from slowstart import __version__

USER_AGENT = f"slowstart/{__version__} (+https://github.com/wad4dai/slowstart)"
CACHE_BUST_PARAM = "_slowstart"
CHUNK_SIZE = 64 * 1024

BYTES_PER_MB = 1_000_000  # десятичные единицы: 1 MB = 10^6 байт
BITS_PER_BYTE = 8

StartCallback = Callable[[int | None], None]
ProgressCallback = Callable[[int], None]


@dataclass(frozen=True, slots=True)
class RequestResult:
    """Итог одного запроса."""

    status: int | None
    bytes_received: int
    elapsed: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class Summary:
    """Сводка по серии запросов. В статистику входят только успешные запросы."""

    attempted: int
    succeeded: int
    total_bytes: int
    total_time: float

    @classmethod
    def from_results(cls, results: Iterable[RequestResult]) -> Summary:
        results = list(results)
        ok = [r for r in results if r.ok]
        return cls(
            attempted=len(results),
            succeeded=len(ok),
            total_bytes=sum(r.bytes_received for r in ok),
            total_time=sum(r.elapsed for r in ok),
        )

    @property
    def avg_time(self) -> float | None:
        return self.total_time / self.succeeded if self.succeeded else None

    @property
    def bytes_per_second(self) -> float | None:
        # Сумма байт / сумма времени, а не среднее скоростей отдельных запросов:
        # так каждый запрос весит пропорционально своему вкладу в общий объём.
        return self.total_bytes / self.total_time if self.total_time > 0 else None

    @property
    def mb_per_s(self) -> float | None:
        bps = self.bytes_per_second
        return bps / BYTES_PER_MB if bps is not None else None

    @property
    def mbit_per_s(self) -> float | None:
        bps = self.bytes_per_second
        return bps * BITS_PER_BYTE / BYTES_PER_MB if bps is not None else None


def validate_url(url: str) -> str:
    """Проверяет, что URL абсолютный, со схемой http(s) и хостом. Иначе — ValueError."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ValueError(f"некорректный URL: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL должен начинаться с http:// или https://")
    if not parsed.host:
        raise ValueError("в URL нет хоста")
    return url


def add_cache_buster(url: str) -> str:
    """Добавляет к URL уникальный query-параметр, сохраняя существующие."""
    return str(httpx.URL(url).copy_add_param(CACHE_BUST_PARAM, uuid.uuid4().hex))


def make_client(timeout: float, *, reuse_connection: bool = False) -> httpx.Client:
    """Создаёт HTTP-клиент для замеров.

    По умолчанию keep-alive выключен: после каждого ответа соединение закрывается,
    и следующий запрос заново проходит DNS, TCP и TLS. Клиент при этом один,
    чтобы не пересоздавать SSL-контекст (это время CPU, а не сети).
    """
    limits = httpx.Limits() if reuse_connection else httpx.Limits(max_keepalive_connections=0)
    return httpx.Client(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            # Без сжатия: считаем реальные байты по сети, а не «сколько бы весил файл».
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
    )


def measure_once(
    client: httpx.Client,
    url: str,
    *,
    on_start: StartCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> RequestResult:
    """Скачивает url целиком и замеряет время от отправки запроса до последнего байта.

    Тело читается потоком и не хранится в памяти. Считаются байты, реально
    пришедшие по сети (iter_raw, без распаковки), а не заявленный Content-Length.

    on_start(content_length) вызывается один раз, когда пришли заголовки успешного
    ответа; on_progress(received) — после каждого прочитанного куска.
    """
    received = 0
    start = time.perf_counter()
    try:
        with client.stream("GET", url) as response:
            if not response.is_success:
                return RequestResult(
                    status=response.status_code,
                    bytes_received=0,
                    elapsed=time.perf_counter() - start,
                    error=f"HTTP {response.status_code} {response.reason_phrase}".rstrip(),
                )
            if on_start is not None:
                on_start(_content_length(response))
            for chunk in response.iter_raw(CHUNK_SIZE):
                received += len(chunk)
                if on_progress is not None:
                    on_progress(received)
            elapsed = time.perf_counter() - start
    except httpx.HTTPError as exc:
        return RequestResult(
            status=None,
            bytes_received=received,
            elapsed=time.perf_counter() - start,
            error=describe_error(exc, client.timeout),
        )
    return RequestResult(status=response.status_code, bytes_received=received, elapsed=elapsed)


def describe_error(exc: httpx.HTTPError, timeout: httpx.Timeout) -> str:
    """Короткое человекочитаемое описание сетевой ошибки."""
    match exc:
        case httpx.ConnectTimeout():
            return f"timeout после {timeout.connect:g} s (не удалось установить соединение)"
        case httpx.TimeoutException():
            return f"timeout после {timeout.read:g} s (сервер перестал отдавать данные)"
        case httpx.ConnectError():
            return f"не удалось подключиться: {exc}"
        case httpx.RemoteProtocolError():
            return f"обрыв соединения: {exc}"
        case httpx.TooManyRedirects():
            return "слишком много редиректов"
        case _:
            return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _content_length(response: httpx.Response) -> int | None:
    """Content-Length используется только для прогресс-бара, не для подсчёта объёма."""
    try:
        return int(response.headers["Content-Length"])
    except (KeyError, ValueError):
        return None
