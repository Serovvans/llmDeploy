"""Порт `AudioTools`: ffprobe и ffmpeg отдельным процессом (docs/portal-api.md §8.7).

Файл записи — недоверенный ввод. Формат, определённый порталом по сигнатуре, задаётся
явно, демультиплексор и звуковые декодеры ограничены белыми списками, из протоколов
разрешён только локальный файл. Процесс получает пустое окружение и пределы времени,
памяти, потоков и размера результата. Его stdout и stderr в журнал не пишутся и на диск
не сохраняются: в них метаданные файла и путь.
"""

import asyncio
import json
import logging
import math
import resource
import shutil
import subprocess
import sys
import wave
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any

from portal.core.settings import MediaSettings
from portal.files.ports import RecordingType
from portal.media.ports import AudioInfo, AudioToolsError, MediaRejectedError, RejectionCode

logger = logging.getLogger(__name__)

# Демультиплексор ffmpeg для каждого типа записи (§1.5): он задаётся явно (`-f`) и он же —
# единственный в белом списке форматов, так что вложенный формат (плейлист, concat) не
# откроется.
DEMUXERS: dict[RecordingType, str] = {
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/flac": "flac",
    "audio/ogg": "ogg",
    "audio/mp4": "mov",
    "video/mp4": "mov",
    "video/quicktime": "mov",
    "video/webm": "matroska",
    "video/x-matroska": "matroska",
}
# Имя декодера ffmpeg не всегда совпадает с именем кодека: MP3 по умолчанию декодирует
# `mp3float`. Белый список декодеров строится из `media.audio_codecs` с этими именами.
_DECODER_ALIASES: dict[str, tuple[str, ...]] = {"mp3": ("mp3float",)}
_WAV_CODEC = "pcm_s16le"
_WAV_RATE_HZ = 16_000
# Ответ ffprobe — формат и строка дорожки; больше этого — не ответ на наш запрос.
_PROBE_OUTPUT_MAX_BYTES = 256 * 1024
_READ_BYTES = 64 * 1024
_SHOW_ENTRIES = "format=format_name,duration:stream=codec_type,codec_name"
# Скопированная звуковая дорожка: её, а не исходный файл, читает ffprobe.
_TRACK = "track.mka"
_TRACK_FORMAT = "matroska"
# Копия дорожки бывает немного больше исходника (служебные данные Matroska на каждый
# пакет), а исходник — до `media.max_bytes`: предел размера копии берётся с запасом.
_TRACK_SIZE_FACTOR = 2
# Образец для самопроверки программ: заведомо исправная запись — десятая доля секунды тишины.
_REFERENCE = "reference.wav"
_REFERENCE_FRAMES = _WAV_RATE_HZ // 10
_NOWHERE = Path("/dev/null")


def _input(path: Path) -> str:
    """Путь как адрес локального файла: имя не будет прочитано как протокол или параметр."""
    return f"file:{path}"


def _input_options(demuxer: str, decoders: Sequence[str], source: Path) -> list[str]:
    """Общие параметры и вход ffmpeg: явный формат, белые списки протоколов и декодеров."""
    return [
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        demuxer,
        "-codec_whitelist",
        ",".join(decoders),
        "-f",
        demuxer,
        "-i",
        _input(source),
    ]


# Только первая звуковая дорожка, без метаданных: видео, субтитры и данные не декодируются.
_FIRST_AUDIO_TRACK = ["-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1"]
_NO_CHAPTERS = ["-map_chapters", "-1"]


def track_command(source: Path, demuxer: str, decoders: Sequence[str], target: Path) -> list[str]:
    """Параметры ffmpeg: первая звуковая дорожка копируется без декодирования в Matroska.

    Так запись проверяется, не открывая декодеров остальных дорожек: видеодекодера в
    белом списке нет, а копированию он не нужен. Matroska пишет длительность в заголовок
    результата, даже если в исходном контейнере её нет (WebM из браузера).
    """
    output = [*_FIRST_AUDIO_TRACK, *_NO_CHAPTERS, "-c:a", "copy", "-f", _TRACK_FORMAT]
    return [*_input_options(demuxer, decoders, source), *output, "-y", _input(target)]


def track_fit_command(source: Path, demuxer: str, decoders: Sequence[str]) -> list[str]:
    """Параметры ffmpeg: ложится ли дорожка в Matroska — её начало копируется мимо диска.

    Та же копия, что в `track_command`, но первой секунды и без записи на том: её исход
    от свободного места и прав на каталог задания не зависит.
    """
    command = track_command(source, demuxer, decoders, _NOWHERE)
    return [*command[:-2], "-t", "1", *command[-2:]]


def container_command(source: Path, demuxer: str, decoders: Sequence[str]) -> list[str]:
    """Параметры ffmpeg: только разобрать контейнер — результат без дорожек и метаданных."""
    output = ["-map_metadata", "-1", *_NO_CHAPTERS, "-f", "ffmetadata", "-"]
    return [*_input_options(demuxer, decoders, source), *output]


def audio_presence_command(source: Path, demuxer: str, decoders: Sequence[str]) -> list[str]:
    """Параметры ffmpeg: есть ли звуковая дорожка — её начало копируется в никуда."""
    output = [*_FIRST_AUDIO_TRACK, "-c:a", "copy", "-t", "1", "-f", "null", "-"]
    return [*_input_options(demuxer, decoders, source), *output]


def probe_command(track: Path, decoders: Sequence[str]) -> list[str]:
    """Параметры ffprobe для скопированной дорожки: белые списки, явный перечень полей.

    Исходный файл ffprobe не получает: он открывает декодер каждой дорожки, и с белым
    списком декодеров завершается с кодом 1 на любой записи с видео или обложкой
    (проверено на 8.0.1). В скопированной дорожке — только звук.
    """
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        _TRACK_FORMAT,
        "-codec_whitelist",
        ",".join(decoders),
        "-f",
        _TRACK_FORMAT,
        "-show_entries",
        _SHOW_ENTRIES,
        "-of",
        "json",
        "-i",
        _input(track),
    ]


def _transcode_command(
    demuxer: str, decoders: Sequence[str], source: Path, threads: int, output: Sequence[str]
) -> list[str]:
    """Параметры ffmpeg: один вход с белыми списками, первая звуковая дорожка, один выход."""
    return [
        *_input_options(demuxer, decoders, source),
        *_FIRST_AUDIO_TRACK,
        "-threads",
        str(threads),
        "-filter_threads",
        "1",
        "-ac",
        "1",
        *output,
    ]


def wav_command(
    source: Path, demuxer: str, decoders: Sequence[str], target: Path, threads: int
) -> list[str]:
    """Параметры ffmpeg для извлечения звука: WAV, 16 кГц, моно."""
    output = ["-ar", str(_WAV_RATE_HZ), "-c:a", _WAV_CODEC, "-f", "wav", "-y", _input(target)]
    return _transcode_command(demuxer, decoders, source, threads, output)


def opus_command(wav: Path, target: Path, bitrate_kbps: int, threads: int) -> list[str]:
    """Параметры ffmpeg для сжатого звука: Opus в Ogg, моно."""
    output = ["-c:a", "libopus", "-b:a", f"{bitrate_kbps}k", "-f", "ogg", "-y", _input(target)]
    return _transcode_command("wav", [_WAV_CODEC], wav, threads, output)


def audio_info(report: Any, codecs: Sequence[str]) -> AudioInfo:
    """Разобрать ответ ffprobe как данные; причина отказа в файле — `MediaRejectedError`."""
    if not isinstance(report, dict):
        raise MediaRejectedError("media_invalid")
    container = report.get("format")
    streams = report.get("streams")
    if not isinstance(container, dict) or not isinstance(streams, list):
        raise MediaRejectedError("media_invalid")
    if _TRACK_FORMAT not in str(container.get("format_name", "")).split(","):
        raise MediaRejectedError("media_invalid")
    audio = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not audio:
        raise MediaRejectedError("media_no_audio")
    if audio[0].get("codec_name") not in codecs:
        raise MediaRejectedError("media_invalid")
    try:
        seconds = float(container.get("duration", ""))
    except (TypeError, ValueError):
        raise MediaRejectedError("media_invalid") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise MediaRejectedError("media_invalid")
    return AudioInfo(duration_ms=round(seconds * 1000))


def _write_reference(path: Path) -> None:
    """Записать образец для самопроверки: тишина в том виде, что пишет `wav_command`."""
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(_WAV_RATE_HZ)
        file.writeframes(bytes(2 * _REFERENCE_FRAMES))


class FfmpegAudioTools:
    """Реализация порта `AudioTools`."""

    def __init__(self, settings: MediaSettings) -> None:
        """Запомнить разрешённые кодеки, битрейт и пределы запуска."""
        self._settings = settings
        self._decoders = [
            name
            for codec in settings.audio_codecs
            for name in (codec, *_DECODER_ALIASES.get(codec, ()))
        ]

    async def probe(self, source: Path, media_type: RecordingType) -> AudioInfo:
        """Проверить запись: скопировать звуковую дорожку и прочитать её ffprobe.

        Декодеры видео и прочих дорожек не открываются ни на одном шаге: исходный файл
        читает только ffmpeg с белым списком декодеров. Отказ по вине файла выносится,
        только если те же программы принимают заведомо исправную запись: иначе это сбой
        среды, и исходник остаётся для повтора.
        """
        try:
            return await self._checked(
                source, DEMUXERS[media_type], self._decoders, self._settings.audio_codecs
            )
        except MediaRejectedError:
            await self._self_check(source.parent)
            raise

    async def _checked(
        self, source: Path, demuxer: str, decoders: Sequence[str], codecs: Sequence[str]
    ) -> AudioInfo:
        """Копия дорожки и её разбор ffprobe; отказ — `MediaRejectedError`."""
        track = source.parent / _TRACK
        size_limit = max(
            self._settings.ffmpeg.output_max_bytes, self._settings.max_bytes * _TRACK_SIZE_FACTOR
        )
        try:
            command = track_command(source, demuxer, decoders, track)
            copied, _ = await self._run("ffmpeg", command, source.parent, size_limit)
            if copied != 0:
                raise MediaRejectedError(await self._refusal(source, demuxer, decoders))
            command = probe_command(track, decoders)
            code, output = await self._run("ffprobe", command, source.parent)
        finally:
            track.unlink(missing_ok=True)
        if code != 0:
            # Декодера дорожки нет в белом списке: кодек не из разрешённых.
            raise MediaRejectedError("media_invalid")
        try:
            report = json.loads(output)
        except ValueError:
            raise MediaRejectedError("media_invalid") from None
        return audio_info(report, codecs)

    async def _refusal(self, source: Path, demuxer: str, decoders: Sequence[str]) -> RejectionCode:
        """Почему дорожка не скопировалась: «нет звука» — только у разобранного контейнера.

        Копия не удалась, хотя дорожка в Matroska ложится, — причина не в файле (нет места
        или прав на каталог задания): это `AudioToolsError`, временный сбой.
        """
        directory = source.parent
        command = container_command(source, demuxer, decoders)
        if (await self._run("ffmpeg", command, directory))[0] != 0:
            return "media_invalid"
        command = audio_presence_command(source, demuxer, decoders)
        if (await self._run("ffmpeg", command, directory))[0] != 0:
            return "media_no_audio"
        command = track_fit_command(source, demuxer, decoders)
        if (await self._run("ffmpeg", command, directory))[0] != 0:
            # Дорожка есть, но в Matroska не ложится: такого кодека среди разрешённых нет.
            return "media_invalid"
        raise AudioToolsError("ffmpeg")

    async def _self_check(self, directory: Path) -> None:
        """Убедиться, что программы принимают исправную запись; нет — `AudioToolsError`.

        Так сбой среды (несовместимые параметры запуска, тесный предел памяти, каталог
        без записи) не выдаётся за негодный файл и не стоит записи её исходника.
        """
        reference = directory / _REFERENCE
        try:
            await asyncio.to_thread(_write_reference, reference)
            await self._checked(reference, "wav", [_WAV_CODEC], [_WAV_CODEC])
        except (OSError, MediaRejectedError) as error:
            logger.warning("media tools self-check failed")
            raise AudioToolsError("ffmpeg") from error
        finally:
            reference.unlink(missing_ok=True)

    async def to_wav(self, source: Path, media_type: RecordingType, target: Path) -> None:
        """Извлечь первую звуковую дорожку в WAV: 16 кГц, моно."""
        threads = self._settings.ffmpeg.threads
        command = wav_command(source, DEMUXERS[media_type], self._decoders, target, threads)
        await self._convert(command, source.parent)

    async def to_opus(self, wav: Path, target: Path) -> None:
        """Сжать звук для прослушивания: Opus в контейнере Ogg, моно."""
        settings = self._settings
        command = opus_command(wav, target, settings.audio_bitrate_kbps, settings.ffmpeg.threads)
        await self._convert(command, wav.parent)

    async def _convert(self, command: Sequence[str], directory: Path) -> None:
        code, _ = await self._run("ffmpeg", command, directory)
        if code != 0:
            raise AudioToolsError("ffmpeg")

    def _limit(self, output_max_bytes: int) -> None:
        """Пределы процесса; выполняется в нём самом перед запуском программы."""
        limits = self._settings.ffmpeg
        resource.setrlimit(resource.RLIMIT_FSIZE, (output_max_bytes, output_max_bytes))
        # Предел адресного пространства действует в Linux — в образе воркера; macOS (машина
        # разработчика) его выставить не даёт.
        if sys.platform == "linux":
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))

    async def _run(
        self,
        program: str,
        arguments: Sequence[str],
        directory: Path,
        output_max_bytes: int | None = None,
    ) -> tuple[int, bytes]:
        """Запустить программу и дождаться её; результат — код возврата и её stdout.

        `output_max_bytes` — предел размера результата, если он не `media.ffmpeg`.

        Сбой запуска, превышение срока, завершение по сигналу (в том числе по пределу
        размера результата) и слишком длинный ответ — `AudioToolsError`. В журнал идут
        только имя программы и код возврата.
        """
        # Окружение процесса пустое, поэтому путь к программе ищется здесь, по PATH воркера.
        binary = shutil.which(program)
        if binary is None:
            raise AudioToolsError(program)
        size_limit = output_max_bytes or self._settings.ffmpeg.output_max_bytes
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={},
                cwd=directory,
                preexec_fn=partial(self._limit, size_limit),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise AudioToolsError(program) from error
        assert process.stdout is not None
        output = bytearray()
        try:
            async with asyncio.timeout(self._settings.ffmpeg.timeout_seconds):
                while chunk := await process.stdout.read(_READ_BYTES):
                    output += chunk
                    if len(output) > _PROBE_OUTPUT_MAX_BYTES:
                        raise AudioToolsError(program)
                code = await process.wait()
        except TimeoutError:
            raise AudioToolsError(program) from None
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
        if code != 0:
            logger.info("media tool failed", extra={"tool": program, "returncode": code})
        if code < 0:
            raise AudioToolsError(program)
        return code, bytes(output)
