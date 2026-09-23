"""Вывод результатов: Rich (по умолчанию) и простой текст (--plain)."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from rich import box
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

from slowstart.measure import (
    BYTES_PER_MB,
    ProgressCallback,
    RequestResult,
    StartCallback,
    Summary,
)


class Reporter(Protocol):
    """Интерфейс вывода, который использует CLI.

    on_start / on_progress передаются в measure_once как есть; None означает,
    что прогресс не нужен и цикл чтения не делает лишних вызовов.
    """

    on_start: StartCallback | None
    on_progress: ProgressCallback | None

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def header(self, url: str, count: int) -> None: ...

    def request_started(self, index: int, count: int) -> None: ...

    def request_finished(self, index: int, count: int, result: RequestResult) -> None: ...

    def summary(self, summary: Summary, *, planned: int, interrupted: bool) -> None: ...


def format_mb(num_bytes: float) -> str:
    return f"{num_bytes / BYTES_PER_MB:.2f} MB"


def format_seconds(seconds: float) -> str:
    return f"{seconds:.2f} s"


def request_label(index: int, count: int) -> str:
    return f"Запрос {index:>{len(str(count))}}/{count}"


def interrupted_message(summary: Summary, planned: int) -> str:
    return f"Прервано: завершено {summary.attempted} из {planned} запросов"


NO_SUCCESS_MESSAGE = "Ни один запрос не завершился успешно — скорость посчитать не из чего."


class PlainReporter:
    """Простой построчный вывод без цветов и прогресс-баров: для логов, cron и CI."""

    on_start: StartCallback | None = None
    on_progress: ProgressCallback | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass

    def header(self, url: str, count: int) -> None:
        print(f"URL: {url}", flush=True)

    def request_started(self, index: int, count: int) -> None:
        pass

    def request_finished(self, index: int, count: int, result: RequestResult) -> None:
        label = request_label(index, count)
        if result.ok:
            line = (
                f"{label}: {result.status}, {format_mb(result.bytes_received)} "
                f"за {format_seconds(result.elapsed)}"
            )
        else:
            line = f"{label}: ошибка — {result.error}"
        print(line, flush=True)

    def summary(self, summary: Summary, *, planned: int, interrupted: bool) -> None:
        print()
        if interrupted:
            print(interrupted_message(summary, planned))
        if not summary.attempted:
            return
        print(f"Успешно: {summary.succeeded}/{summary.attempted}")
        if summary.avg_time is None or summary.mb_per_s is None or summary.mbit_per_s is None:
            print(NO_SUCCESS_MESSAGE)
            return
        print(f"Среднее время запроса: {format_seconds(summary.avg_time)}")
        print(f"Скачано всего: {format_mb(summary.total_bytes)}")
        print(f"Средняя скорость: {summary.mb_per_s:.2f} MB/s ({summary.mbit_per_s:.1f} Mbit/s)")


class RichReporter:
    """Прогресс-бар на каждый запрос, строка с результатом и итоговая таблица.

    Прогресс обновляется из callback'ов measure_once, а перерисовка идёт в фоновом
    потоке Rich (~10 раз в секунду), поэтому update() в цикле чтения дешёвый.
    Если вывод не в терминал, Rich сам отключает анимацию и цвета.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),  # при неизвестном размере процент не показывается
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            console=self.console,
            transient=True,
        )
        self._task: TaskID | None = None

    def __enter__(self) -> Self:
        self._progress.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._progress.stop()

    def header(self, url: str, count: int) -> None:
        self.console.print(Text.assemble(("slowstart", "bold cyan"), " → ", (url, "underline")))
        self.console.print(Text(f"Запросов: {count}", style="dim"))

    def request_started(self, index: int, count: int) -> None:
        # total=None — пока не пришли заголовки, размер неизвестен: бар без процента.
        self._task = self._progress.add_task(request_label(index, count), total=None)

    def on_start(self, content_length: int | None) -> None:
        if self._task is not None:
            self._progress.update(self._task, total=content_length)

    def on_progress(self, received: int) -> None:
        if self._task is not None:
            self._progress.update(self._task, completed=received)

    def request_finished(self, index: int, count: int, result: RequestResult) -> None:
        if self._task is not None:
            self._progress.remove_task(self._task)
            self._task = None
        label = request_label(index, count)
        if result.ok:
            speed = result.bytes_received / result.elapsed if result.elapsed > 0 else 0.0
            line = Text.assemble(
                (label, "dim"),
                ("  ✓ ", "green"),
                f"{result.status}, {format_mb(result.bytes_received)} "
                f"за {format_seconds(result.elapsed)}",
                (f"  ({format_mb(speed)}/s)", "dim"),
            )
        else:
            line = Text.assemble((label, "dim"), ("  ✗ ", "red"), (result.error or "", "red"))
        self._progress.console.print(line)

    def summary(self, summary: Summary, *, planned: int, interrupted: bool) -> None:
        self.console.print()
        if interrupted:
            self.console.print(Text(interrupted_message(summary, planned), style="yellow"))
        if not summary.attempted:
            return

        table = Table(title="Итог", box=box.ROUNDED, show_header=False, title_style="bold")
        table.add_column(style="bold")
        table.add_column(justify="right")
        all_ok = summary.succeeded == summary.attempted
        table.add_row(
            "Успешные запросы",
            Text(f"{summary.succeeded}/{summary.attempted}", style="green" if all_ok else "yellow"),
        )
        if summary.avg_time is not None and summary.mb_per_s is not None:
            table.add_row("Среднее время запроса", format_seconds(summary.avg_time))
            table.add_row("Скачано всего", format_mb(summary.total_bytes))
            table.add_row("Средняя скорость", Text(f"{summary.mb_per_s:.2f} MB/s", style="bold"))
            table.add_row("", Text(f"{summary.mbit_per_s:.1f} Mbit/s", style="bold"))
        self.console.print(table)
        if summary.succeeded == 0:
            self.console.print(Text(NO_SUCCESS_MESSAGE, style="red"))
