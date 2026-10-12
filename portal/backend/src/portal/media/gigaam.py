"""Порт `SpeechRecognizer`: GigaAM-v3 на CPU через onnx-asr и ONNX Runtime (§8.7, ПД §3.3).

Веса лежат в каталоге `media.asr.model_dir`; в интернет за ними модуль не обращается:
каталога нет — это ошибка, а не повод для загрузки.
"""

import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from portal.media.ports import SpeechRecognitionError, SpeechSegment

# Вариант e2e — с пунктуацией (ПД §3.3). Имена — как их знает onnx-asr; они же — имена
# подкаталогов весов в `media.asr.model_dir`.
ASR_MODEL = "gigaam-v3-e2e-rnnt"
VAD_MODEL = "silero"
_SAMPLE_RATE_HZ = 16_000
_SAMPLE_BYTES = 2
_PCM16_SCALE = 32768.0


class SpeechWeightsMissingError(Exception):
    """В каталоге весов нет модели речи или модели пауз: воркер не стартует (§8.7)."""


def require_weights(model_dir: Path) -> None:
    """Убедиться, что веса на месте; иначе — `SpeechWeightsMissingError`."""
    for name in (ASR_MODEL, VAD_MODEL):
        if not any((model_dir / name).glob("*.onnx")):
            raise SpeechWeightsMissingError(name)


class GigaamSpeechRecognizer:
    """Реализация порта `SpeechRecognizer`.

    Число потоков у сеанса ONNX Runtime задаётся при создании, поэтому на каждое значение
    `threads` — свой экземпляр модели: один у заданий базы знаний, один у места под
    вложения чата. Экземпляр создаётся при первом обращении.
    """

    def __init__(self, model_dir: Path) -> None:
        """Запомнить каталог весов."""
        self._model_dir = model_dir
        self._models: dict[int, Any] = {}
        self._loading = threading.Lock()

    def transcribe(
        self, wav: Path, start_ms: int, end_ms: int, threads: int
    ) -> list[SpeechSegment]:
        """Распознать участок записи: речь выделяет модель пауз, текст — GigaAM."""
        try:
            model = self._model(threads)
            samples = _read(wav, start_ms, end_ms)
            if samples.size == 0:
                return []
            found = [
                SpeechSegment(
                    start_ms=start_ms + round(result.start * 1000),
                    end_ms=start_ms + round(result.end * 1000),
                    text=result.text.strip(),
                )
                for result in model.recognize(samples, sample_rate=_SAMPLE_RATE_HZ)
            ]
        except SpeechRecognitionError:
            raise
        except Exception as error:
            raise SpeechRecognitionError from error
        return [segment for segment in found if segment.text]

    def _model(self, threads: int) -> Any:
        with self._loading:
            if threads not in self._models:
                self._models[threads] = self._load(threads)
            return self._models[threads]

    def _load(self, threads: int) -> Any:
        # Импорт здесь: ONNX Runtime нужен только воркеру, а сборка зависимостей у него
        # общая с `portal-api`.
        import onnx_asr
        import onnxruntime

        require_weights(self._model_dir)
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        vad = onnx_asr.load_vad(
            VAD_MODEL, self._model_dir / VAD_MODEL, sess_options=options, providers=providers
        )
        asr = onnx_asr.load_model(
            ASR_MODEL, self._model_dir / ASR_MODEL, sess_options=options, providers=providers
        )
        return asr.with_vad(vad)


def _read(wav: Path, start_ms: int, end_ms: int) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Участок WAV (16 кГц, моно, 16 бит — то, что пишет `AudioTools.to_wav`) в виде отсчётов."""
    with wave.open(str(wav), "rb") as source:
        if (
            source.getframerate() != _SAMPLE_RATE_HZ
            or source.getnchannels() != 1
            or source.getsampwidth() != _SAMPLE_BYTES
        ):
            raise SpeechRecognitionError("wav")
        first = start_ms * _SAMPLE_RATE_HZ // 1000
        source.setpos(min(first, source.getnframes()))
        frames = source.readframes((end_ms - start_ms) * _SAMPLE_RATE_HZ // 1000)
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / _PCM16_SCALE
