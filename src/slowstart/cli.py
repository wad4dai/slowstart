"""CLI: разбирает аргументы, запускает серию замеров и выбирает формат вывода."""

from __future__ import annotations

from typing import Annotated

import typer

from slowstart.measure import (
    RequestResult,
    Summary,
    add_cache_buster,
    make_client,
    measure_once,
    validate_url,
)
from slowstart.output import PlainReporter, Reporter, RichReporter

EXIT_NO_SUCCESS = 1
EXIT_INTERRUPTED = 130  # 128 + SIGINT, общепринятый код для Ctrl+C

app = typer.Typer(add_completion=False)


def _check_url(url: str) -> str:
    try:
        return validate_url(url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _check_timeout(timeout: float) -> float:
    if timeout <= 0:
        raise typer.BadParameter("таймаут должен быть больше нуля")
    return timeout


@app.command()
def run(
    url: Annotated[
        str,
        typer.Argument(
            help="Адрес файла для скачивания (лучше тяжёлого, от 10–50 MB).",
            callback=_check_url,
            metavar="URL",
            show_default=False,
        ),
    ],
    count: Annotated[
        int, typer.Option("-n", "--count", min=1, help="Сколько запросов сделать.")
    ] = 10,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            callback=_check_timeout,
            help="Таймаут в секундах на каждую сетевую операцию: установку соединения "
            "и ожидание очередной порции данных.",
        ),
    ] = 30.0,
    reuse_connection: Annotated[
        bool,
        typer.Option(
            "--reuse-connection",
            help="Переиспользовать соединение (keep-alive). По умолчанию каждый запрос "
            "открывает новое, и время включает DNS, TCP и TLS.",
        ),
    ] = False,
    no_cache_bust: Annotated[
        bool,
        typer.Option("--no-cache-bust", help="Не добавлять к URL уникальный query-параметр."),
    ] = False,
    plain: Annotated[
        bool,
        typer.Option("--plain", help="Простой текстовый вывод без прогресс-баров и цветов."),
    ] = False,
) -> None:
    """Скачивает файл по URL несколько раз подряд и печатает среднюю скорость."""
    reporter: Reporter = PlainReporter() if plain else RichReporter()
    reporter.header(url, count)

    results: list[RequestResult] = []
    interrupted = False
    try:
        with make_client(timeout, reuse_connection=reuse_connection) as client, reporter:
            for index in range(1, count + 1):
                target = url if no_cache_bust else add_cache_buster(url)
                reporter.request_started(index, count)
                result = measure_once(
                    client, target, on_start=reporter.on_start, on_progress=reporter.on_progress
                )
                reporter.request_finished(index, count, result)
                results.append(result)
    except KeyboardInterrupt:
        # Прогресс-бар уже закрыт выходом из with; незавершённый запрос не учитываем.
        interrupted = True

    summary = Summary.from_results(results)
    reporter.summary(summary, planned=count, interrupted=interrupted)

    if interrupted:
        raise typer.Exit(EXIT_INTERRUPTED)
    if summary.succeeded == 0:
        raise typer.Exit(EXIT_NO_SUCCESS)


def main() -> None:
    app()
