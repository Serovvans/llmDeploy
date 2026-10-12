"""Расшифровка частями (docs/portal-api.md §8.7): нарезка записи, текст и отметки части.

Потоки распознавания речи общие на воркер (`media.asr.threads`), поэтому у каждого набора
мест — своя очередь `SpeechPool` с одним потоком исполнения и своей долей потоков модели.
"""

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from portal.media.ports import SpeechRecognizer, SpeechSegment

# Отрезки речи в тексте части разделены пробелом: текст части — только речь (§8.7).
_SEPARATOR = " "


@dataclass(frozen=True)
class TranscriptPart:
    """Часть расшифровки: текст и отметки `(смещение в тексте, время начала отрезка, мс)`."""

    text: str
    marks: tuple[tuple[int, int], ...]


def whole_seconds(duration_ms: int) -> int:
    """Длительность в целых секундах, с округлением вверх: ход расшифровки её не превысит."""
    return -(-duration_ms // 1000)


def part_bounds(duration_ms: int, part_ms: int) -> list[tuple[int, int]]:
    """Границы частей записи по порядку: `(начало, конец)` в мс; номер части — с единицы."""
    return [(start, min(start + part_ms, duration_ms)) for start in range(0, duration_ms, part_ms)]


def transcript_part(segments: Sequence[SpeechSegment]) -> TranscriptPart:
    """Собрать часть из отрезков речи; первая отметка — со смещением 0."""
    marks: list[tuple[int, int]] = []
    pieces: list[str] = []
    offset = 0
    for segment in segments:
        marks.append((offset, segment.start_ms))
        pieces.append(segment.text)
        offset += len(segment.text) + len(_SEPARATOR)
    return TranscriptPart(_SEPARATOR.join(pieces), tuple(marks))


def mark_time(marks: Sequence[Sequence[int]], offset: int) -> int | None:
    """Время последней отметки, стоящей не позже `offset`; отметок нет — `None`."""
    reached = [time for position, time in marks if position <= offset]
    return reached[-1] if reached else None


class SpeechPool:
    """Очередь распознавания одного набора мест: по одному вызову, со своей долей потоков.

    Исполнитель с одним потоком: вызов, от результата которого отказались (задание снято),
    ещё занимает свои потоки модели, и следующий ждёт его конца — общий предел потоков
    воркера не превышается.
    """

    def __init__(self, recognizer: SpeechRecognizer, threads: int) -> None:
        """Запомнить распознаватель и число потоков модели этого набора мест."""
        self._recognizer = recognizer
        self._threads = threads
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech")

    async def transcribe(self, wav: Path, start_ms: int, end_ms: int) -> list[SpeechSegment]:
        """Отрезки речи участка записи. Сбой — `SpeechRecognitionError`."""
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._recognizer.transcribe, wav, start_ms, end_ms, self._threads
        )

    def close(self) -> None:
        """Остановить исполнитель; идущее распознавание не ждём."""
        self._executor.shutdown(wait=False, cancel_futures=True)
