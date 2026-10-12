"""Порты обработки записей (docs/portal-api.md §8.7, §13.3); ими пользуются kb и worker."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from portal.files.ports import RecordingType

RejectionCode = Literal["media_invalid", "media_no_audio"]


@dataclass(frozen=True)
class AudioInfo:
    """Что показала проверка записи."""

    duration_ms: int


class MediaRejectedError(Exception):
    """Причина в самом файле: формат не подтвердился, кодек не из разрешённых, нет звука."""

    def __init__(self, code: RejectionCode) -> None:
        """Запомнить код причины."""
        super().__init__(code)
        self.code: RejectionCode = code


class AudioToolsError(Exception):
    """Сбой запуска ffmpeg или ffprobe либо превышение пределов запуска."""


class SpeechRecognitionError(Exception):
    """Сбой распознавания речи."""


class AudioTools(Protocol):
    """ffprobe и ffmpeg отдельным процессом; требования к запуску — §8.7."""

    async def probe(self, source: Path, media_type: RecordingType) -> AudioInfo:
        """Проверить запись: формат, первая звуковая дорожка, её кодек, длительность.

        `MediaRejectedError` — причина в файле; `AudioToolsError` — сбой запуска.
        """
        ...

    async def to_wav(self, source: Path, media_type: RecordingType, target: Path) -> None:
        """Извлечь первую звуковую дорожку в WAV: 16 кГц, моно. `AudioToolsError`."""
        ...

    async def to_opus(self, wav: Path, target: Path) -> None:
        """Сжать звук для прослушивания: Opus в контейнере Ogg, моно. `AudioToolsError`."""
        ...


@dataclass(frozen=True)
class SpeechSegment:
    """Распознанный отрезок речи; время — от начала записи."""

    start_ms: int
    end_ms: int
    text: str


class SpeechRecognizer(Protocol):
    """Распознавание речи на CPU; вызов блокирующий — его место в пуле потоков."""

    def transcribe(
        self, wav: Path, start_ms: int, end_ms: int, threads: int
    ) -> list[SpeechSegment]:
        """Отрезки речи участка записи по порядку, с пунктуацией; пустой список — речи нет.

        Сбой — `SpeechRecognitionError`.
        """
        ...
