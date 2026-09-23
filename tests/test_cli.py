from __future__ import annotations

import pytest
from typer.testing import CliRunner

from slowstart import cli
from slowstart.measure import CACHE_BUST_PARAM, RequestResult
from tests.local_server import FILE_SIZE, LocalServer

runner = CliRunner()


def test_plain_success(server: LocalServer) -> None:
    result = runner.invoke(cli.app, [server.url("/file"), "-n", "3", "--plain"])

    assert result.exit_code == 0, result.output
    assert f"URL: {server.url('/file')}" in result.output
    assert "Запрос 1/3: 200, 1.23 MB за" in result.output
    assert "Успешно: 3/3" in result.output
    assert f"Скачано всего: {3 * FILE_SIZE / 1e6:.2f} MB" in result.output
    assert "MB/s (" in result.output
    assert "Mbit/s)" in result.output
    assert "\x1b[" not in result.output  # без ANSI-кодов


def test_rich_output_degrades_without_terminal(server: LocalServer) -> None:
    result = runner.invoke(cli.app, [server.url("/file"), "-n", "2"])

    assert result.exit_code == 0, result.output
    assert "Итог" in result.output
    assert "Mbit/s" in result.output
    assert "\x1b[" not in result.output


def test_partial_failures_do_not_stop_the_run(
    server: LocalServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_measure = cli.measure_once
    calls = 0

    def flaky(*args: object, **kwargs: object) -> RequestResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            return RequestResult(status=None, bytes_received=0, elapsed=30.0, error="timeout")
        return real_measure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "measure_once", flaky)
    result = runner.invoke(cli.app, [server.url("/file"), "-n", "3", "--plain"])

    assert result.exit_code == 0, result.output
    assert "Запрос 2/3: ошибка — timeout" in result.output
    assert "Успешно: 2/3" in result.output


@pytest.mark.parametrize("mode", [["--plain"], []])
def test_all_failed_exits_with_1(server: LocalServer, mode: list[str]) -> None:
    result = runner.invoke(cli.app, [server.url("/missing"), "-n", "2", *mode])

    assert result.exit_code == 1
    assert "404" in result.output
    assert "Ни один запрос не завершился успешно" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["not-a-url"],
        ["ftp://example.com/file"],
        ["https://example.com/f", "-n", "0"],
        ["https://example.com/f", "--timeout", "0"],
    ],
)
def test_invalid_arguments_exit_with_2(args: list[str]) -> None:
    assert runner.invoke(cli.app, [*args, "--plain"]).exit_code == 2


@pytest.mark.parametrize("mode", [["--plain"], []])
def test_ctrl_c_prints_partial_stats_and_exits_130(
    server: LocalServer, monkeypatch: pytest.MonkeyPatch, mode: list[str]
) -> None:
    real_measure = cli.measure_once
    calls = 0

    def interrupted_on_third(*args: object, **kwargs: object) -> RequestResult:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return real_measure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "measure_once", interrupted_on_third)
    result = runner.invoke(cli.app, [server.url("/file"), "-n", "5", *mode])

    assert result.exit_code == 130
    assert "Прервано: завершено 2 из 5 запросов" in result.output
    assert "Traceback" not in result.output
    assert "2/2" in result.output


def test_cache_busting_is_on_by_default(server: LocalServer) -> None:
    runner.invoke(cli.app, [server.url("/file?v=1"), "-n", "2", "--plain"])

    paths = [r.path for r in server.requests]
    assert len(paths) == 2
    assert all(CACHE_BUST_PARAM in p and "v=1" in p for p in paths)
    assert len(set(paths)) == 2


def test_no_cache_bust(server: LocalServer) -> None:
    runner.invoke(cli.app, [server.url("/file"), "-n", "2", "--plain", "--no-cache-bust"])

    assert [r.path for r in server.requests] == ["/file", "/file"]
