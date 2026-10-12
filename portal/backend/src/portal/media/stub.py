"""Подмена распознавания речи для стенда (docs/portal-api.md §8.7): `media.asr.engine: stub`."""

from pathlib import Path

from portal.media.ports import SpeechSegment

_SLOT_MS = 30_000


class StubSpeechRecognizer:
    """Детерминированная подмена порта `SpeechRecognizer`: файл записи не читается.

    На каждые 30 секунд записи — отрезок с текстом `Расшифровка-заглушка, отрезок <N>.`;
    `N` — с единицы от начала записи.
    """

    def transcribe(
        self, wav: Path, start_ms: int, end_ms: int, threads: int
    ) -> list[SpeechSegment]:
        """Отрезки, начало которых попадает в участок записи."""
        first = -(-start_ms // _SLOT_MS)
        return [
            SpeechSegment(
                start_ms=slot * _SLOT_MS,
                end_ms=min((slot + 1) * _SLOT_MS, end_ms),
                text=f"Расшифровка-заглушка, отрезок {slot + 1}.",
            )
            for slot in range(first, -(-end_ms // _SLOT_MS))
        ]
