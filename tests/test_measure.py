from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from slowstart.measure import (
    CACHE_BUST_PARAM,
    USER_AGENT,
    RequestResult,
    Summary,
    add_cache_buster,
    make_client,
    measure_once,
    validate_url,
)
from tests.local_server import FILE_SIZE, LocalServer

# --- сетевые замеры на локальном сервере ---


def test_counts_exact_bytes(server: LocalServer) -> None:
    with make_client(5) as client:
        result = measure_once(client, server.url("/file"))

    assert result.ok
    assert result.status == 200
    assert result.bytes_received == FILE_SIZE
    assert result.elapsed > 0


def test_callbacks_report_size_and_progress(server: LocalServer) -> None:
    starts: list[int | None] = []
    progress: list[int] = []
    with make_client(5) as client:
        measure_once(
            client, server.url("/file"), on_start=starts.append, on_progress=progress.append
        )

    assert starts == [FILE_SIZE]
    assert progress == sorted(progress)
    assert progress[-1] == FILE_SIZE


def test_response_without_content_length(server: LocalServer) -> None:
    starts: list[int | None] = []
    with make_client(5) as client:
        result = measure_once(client, server.url("/no-length"), on_start=starts.append)

    assert result.ok
    assert result.bytes_received == FILE_SIZE
    assert starts == [None]  # прогресс-бар будет без процента


def test_http_error_is_reported_not_raised(server: LocalServer) -> None:
    with make_client(5) as client:
        result = measure_once(client, server.url("/missing"))

    assert not result.ok
    assert result.status == 404
    assert result.bytes_received == 0
    assert result.error is not None
    assert "404" in result.error


def test_timeout(server: LocalServer) -> None:
    with make_client(0.2) as client:
        result = measure_once(client, server.url("/slow"))

    assert not result.ok
    assert result.error is not None
    assert "timeout" in result.error
    assert "0.2 s" in result.error


def test_truncated_body_is_an_error(server: LocalServer) -> None:
    with make_client(5) as client:
        result = measure_once(client, server.url("/truncated"))

    assert not result.ok
    assert result.error is not None
    assert "обрыв соединения" in result.error


def test_follows_redirects(server: LocalServer) -> None:
    with make_client(5) as client:
        result = measure_once(client, server.url("/redirect"))

    assert result.ok
    assert result.bytes_received == FILE_SIZE


def test_request_headers(server: LocalServer) -> None:
    with make_client(5) as client:
        headers = client.get(server.url("/echo")).json()["headers"]

    headers = {k.lower(): v for k, v in headers.items()}
    assert headers["accept-encoding"] == "identity"
    assert headers["cache-control"] == "no-cache"
    assert headers["user-agent"] == USER_AGENT


@pytest.mark.parametrize(("reuse", "expected_connections"), [(False, 3), (True, 1)])
def test_connection_reuse(server: LocalServer, reuse: bool, expected_connections: int) -> None:
    with make_client(5, reuse_connection=reuse) as client:
        for _ in range(3):
            assert measure_once(client, server.url("/file")).ok

    ports = {r.client_port for r in server.requests}
    assert len(ports) == expected_connections


# --- cache busting ---


def test_cache_buster_without_query() -> None:
    url = add_cache_buster("https://example.com/big.jpg")
    parts = urlsplit(url)

    assert parts.path == "/big.jpg"
    assert list(parse_qs(parts.query)) == [CACHE_BUST_PARAM]


def test_cache_buster_keeps_existing_query() -> None:
    url = add_cache_buster("https://example.com/big.jpg?size=large&v=2")
    query = parse_qs(urlsplit(url).query)

    assert query["size"] == ["large"]
    assert query["v"] == ["2"]
    assert CACHE_BUST_PARAM in query


def test_cache_buster_is_unique() -> None:
    urls = {add_cache_buster("https://example.com/f") for _ in range(100)}
    assert len(urls) == 100


# --- сводка ---


def _ok(num_bytes: int, seconds: float) -> RequestResult:
    return RequestResult(status=200, bytes_received=num_bytes, elapsed=seconds)


def test_average_speed_is_total_bytes_over_total_time() -> None:
    # 10 MB за 1 s и 10 MB за 9 s: итого 20 MB за 10 s = 2 MB/s.
    # Среднее скоростей дало бы (10 + 1.11) / 2 ≈ 5.56 MB/s — это неверно.
    summary = Summary.from_results([_ok(10_000_000, 1.0), _ok(10_000_000, 9.0)])

    assert summary.mb_per_s == pytest.approx(2.0)
    assert summary.mbit_per_s == pytest.approx(16.0)
    assert summary.avg_time == pytest.approx(5.0)


def test_failed_requests_are_excluded() -> None:
    failed = RequestResult(status=None, bytes_received=500, elapsed=30.0, error="timeout")
    summary = Summary.from_results([_ok(4_000_000, 2.0), failed])

    assert summary.attempted == 2
    assert summary.succeeded == 1
    assert summary.total_bytes == 4_000_000
    assert summary.total_time == pytest.approx(2.0)
    assert summary.mb_per_s == pytest.approx(2.0)


def test_summary_without_successes() -> None:
    failed = RequestResult(status=404, bytes_received=0, elapsed=0.1, error="HTTP 404")
    summary = Summary.from_results([failed])

    assert summary.succeeded == 0
    assert summary.avg_time is None
    assert summary.mb_per_s is None
    assert summary.mbit_per_s is None


# --- валидация URL ---


@pytest.mark.parametrize("url", ["https://example.com/big.jpg", "http://127.0.0.1:8000/f?a=1"])
def test_valid_urls(url: str) -> None:
    assert validate_url(url) == url


@pytest.mark.parametrize("url", ["example.com/big.jpg", "ftp://example.com/f", "http://", ""])
def test_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_url(url)
