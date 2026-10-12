"""Запуск ffprobe и ffmpeg на недоверенных файлах и расшифровка частями (§8.7).

Тесты с настоящим ffmpeg пропускаются там, где его нет; параметры запуска проверяются
всегда — по собранной командной строке.
"""

import asyncio
import logging
import math
import os
import shutil
import struct
import subprocess
import wave
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from portal.core.settings import Settings
from portal.media import ffmpeg as ffmpeg_module
from portal.media.ffmpeg import (
    FfmpegAudioTools,
    audio_info,
    audio_presence_command,
    container_command,
    opus_command,
    probe_command,
    track_command,
    track_fit_command,
    wav_command,
)
from portal.media.ports import AudioToolsError, MediaRejectedError, SpeechSegment
from portal.media.stub import StubSpeechRecognizer
from portal.media.transcription import mark_time, part_bounds, transcript_part

pytestmark = pytest.mark.anyio

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="нужны ffmpeg и ffprobe",
)


def _wav(path: Path, seconds: float = 1.0, rate: int = 44_100) -> Path:
    """WAV с тоном 440 Гц, стерео — чтобы извлечение звука меняло и частоту, и каналы."""
    frames = b"".join(
        struct.pack("<hh", sample, sample)
        for sample in (
            round(12_000 * math.sin(2 * math.pi * 440 * index / rate))
            for index in range(round(seconds * rate))
        )
    )
    with wave.open(str(path), "wb") as file:
        file.setnchannels(2)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(frames)
    return path


def _video(path: Path, *, sound: Path | None) -> Path:
    """MP4 с видеодорожкой; `sound` — добавить звук из этого файла."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi"]
    command += ["-i", "testsrc=duration=1:size=64x48:rate=5"]
    if sound is not None:
        command += ["-i", str(sound), "-c:a", "aac", "-shortest"]
    subprocess.run([*command, "-c:v", "mpeg4", str(path)], check=True, capture_output=True)
    return path


def _tools(settings: Settings, **overrides: Any) -> FfmpegAudioTools:
    return FfmpegAudioTools(settings.media.model_copy(update=overrides))


def _rejection(error: pytest.ExceptionInfo[MediaRejectedError]) -> str:
    return error.value.code


# --- командная строка: проверяется без ffmpeg ---


def test_source_is_read_only_by_ffmpeg_with_whitelists_and_ffprobe_gets_the_copied_track() -> None:
    source, track = Path("/data/media/tmp/job/source"), Path("/data/media/tmp/job/track.mka")
    decoders = ["aac", "mp3", "mp3float"]
    # Исходный файл: явный формат и все три белых списка — на каждом шаге проверки.
    for command in (
        track_command(source, "mov", decoders, track),
        container_command(source, "mov", decoders),
        audio_presence_command(source, "mov", decoders),
        track_fit_command(source, "mov", decoders),
    ):
        options = dict(pairwise(command))
        assert command[command.index("-f") + 1] == options["-format_whitelist"] == "mov"
        assert command.index("-f") < command.index("-i")
        assert options["-codec_whitelist"] == "aac,mp3,mp3float"
        assert options["-protocol_whitelist"] == "file"
        assert options["-i"] == "file:/data/media/tmp/job/source"
    # Дорожка копируется без декодирования; видео, субтитры и данные в копию не идут.
    copy = track_command(source, "mov", decoders, track)
    assert dict(pairwise(copy))["-map"] == "0:a:0" and {"-vn", "-sn", "-dn"} <= set(copy)
    assert copy[-6:] == ["-c:a", "copy", "-f", "matroska", "-y", f"file:{track}"]
    # Проверка «ложится ли дорожка в Matroska» — та же копия, но начала и мимо диска.
    fit = track_fit_command(source, "mov", decoders)
    assert fit == [*copy[:-2], "-t", "1", "-y", "file:/dev/null"]

    command = probe_command(track, decoders)
    options = dict(pairwise(command))
    assert options["-f"] == options["-format_whitelist"] == "matroska"
    assert options["-codec_whitelist"] == "aac,mp3,mp3float"
    assert options["-protocol_whitelist"] == "file"
    assert options["-i"] == "file:/data/media/tmp/job/track.mka"
    # Метаданных (`tags`) в перечне полей нет; `-nostats` у ffprobe нет — с ним он падает.
    assert options["-show_entries"] == "format=format_name,duration:stream=codec_type,codec_name"
    assert "-nostats" not in command


def test_ffmpeg_decodes_only_whitelisted_audio_and_never_video(settings: Settings) -> None:
    decoders = ["aac", "mp3", "mp3float"]
    command = wav_command(Path("/t/source"), "mov", decoders, Path("/t/audio.wav"), 2)
    options = dict(pairwise(command))

    assert command[:5] == ["-hide_banner", "-nostats", "-loglevel", "error", "-nostdin"]
    # Формат входа задан явно и стоит до входного файла; второй `-f` — формат результата.
    assert command[command.index("-f") + 1] == options["-format_whitelist"] == "mov"
    assert command.index("-f") < command.index("-i")
    assert options["-codec_whitelist"] == "aac,mp3,mp3float"
    assert options["-protocol_whitelist"] == "file"
    assert options["-map"] == "0:a:0" and {"-vn", "-sn", "-dn"} <= set(command)
    assert (options["-ar"], options["-ac"], options["-threads"]) == ("16000", "1", "2")

    opus = opus_command(Path("/t/audio.wav"), Path("/t/audio.ogg"), 32, 2)
    assert dict(pairwise(opus))["-codec_whitelist"] == "pcm_s16le"
    assert opus[-8:] == ["-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "-y", "file:/t/audio.ogg"]
    # Декодер MP3 у ffmpeg называется иначе, чем кодек: без него MP3 не прошёл бы список.
    assert "mp3float" in FfmpegAudioTools(settings.media)._decoders


_AAC = [{"codec_type": "audio", "codec_name": "aac"}]


@pytest.mark.parametrize(
    ("container", "streams", "code"),
    [
        ({"format_name": "matroska,webm", "duration": "3.0"}, [], "media_no_audio"),
        (
            {"format_name": "matroska,webm", "duration": "3.0"},
            [{"codec_type": "video", "codec_name": "vp8"}],
            "media_no_audio",
        ),
        (
            {"format_name": "matroska,webm", "duration": "3.0"},
            [{"codec_type": "audio", "codec_name": "ac3"}],
            "media_invalid",
        ),
        # Формат не тот, длительность не читается, ответ — не то, что запрошено.
        ({"format_name": "hls", "duration": "3.0"}, _AAC, "media_invalid"),
        ({"format_name": "matroska,webm"}, _AAC, "media_invalid"),
        ({"format_name": "matroska,webm", "duration": "nan"}, _AAC, "media_invalid"),
        ({"format_name": "matroska,webm", "duration": "3.0"}, "streams", "media_invalid"),
        ("format", _AAC, "media_invalid"),
    ],
)
def test_probe_report_is_read_as_untrusted_data(container: Any, streams: Any, code: str) -> None:
    with pytest.raises(MediaRejectedError) as error:
        audio_info({"format": container, "streams": streams}, ["aac", "mp3"])
    assert _rejection(error) == code
    with pytest.raises(MediaRejectedError):
        audio_info([], ["aac"])


def test_first_audio_track_decides_and_duration_is_in_milliseconds() -> None:
    report = {
        "format": {"format_name": "matroska,webm", "duration": "61.2504"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "audio", "codec_name": "ac3"},
        ],
    }
    assert audio_info(report, ["aac"]).duration_ms == 61_250


# --- настоящий ffmpeg ---


@needs_ffmpeg
async def test_recording_is_checked_extracted_and_compressed(
    settings: Settings, tmp_path: Path
) -> None:
    tools = _tools(settings)
    source = _wav(tmp_path / "source", seconds=1.5)

    assert (await tools.probe(source, "audio/wav")).duration_ms == 1500
    # Скопированная для проверки дорожка в каталоге задания не остаётся.
    assert [file.name for file in tmp_path.iterdir()] == ["source"]
    extracted = tmp_path / "audio.wav"
    await tools.to_wav(source, "audio/wav", extracted)
    with wave.open(str(extracted), "rb") as file:
        assert (file.getframerate(), file.getnchannels(), file.getsampwidth()) == (16_000, 1, 2)
        assert file.getnframes() == 24_000

    compressed = tmp_path / "audio.ogg"
    await tools.to_opus(extracted, compressed)
    assert compressed.read_bytes().startswith(b"OggS")
    assert compressed.stat().st_size < extracted.stat().st_size


@needs_ffmpeg
async def test_playlist_is_rejected_whatever_type_it_claims(
    settings: Settings, tmp_path: Path
) -> None:
    """Плейлист со ссылкой на локальный файл не открывается ни под одним типом записи."""
    target = _wav(tmp_path / "secret.wav")
    playlist = tmp_path / "source"
    playlist.write_text(
        f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile://{target}\n#EXT-X-ENDLIST\n"
    )
    concat = tmp_path / "concat"
    concat.write_text(f"ffconcat version 1.0\nfile '{target}'\n")
    tools = _tools(settings)

    for media_type in ("audio/mpeg", "audio/wav", "audio/ogg", "video/mp4", "video/webm"):
        for file in (playlist, concat):
            with pytest.raises(MediaRejectedError):
                await tools.probe(file, media_type)
            with pytest.raises(AudioToolsError):
                await tools.to_wav(file, media_type, tmp_path / "out.wav")
    assert not (tmp_path / "out.wav").exists() or (tmp_path / "out.wav").stat().st_size == 0


@needs_ffmpeg
async def test_codec_outside_the_whitelist_is_rejected(settings: Settings, tmp_path: Path) -> None:
    source = _wav(tmp_path / "source")
    only_mp3 = _tools(settings, audio_codecs=("mp3",))

    with pytest.raises(MediaRejectedError) as error:
        await only_mp3.probe(source, "audio/wav")
    assert _rejection(error) == "media_invalid"
    # И сам ffmpeg такой звук не декодирует: белый список декодеров — в его параметрах.
    with pytest.raises(AudioToolsError):
        await only_mp3.to_wav(source, "audio/wav", tmp_path / "out.wav")


@needs_ffmpeg
async def test_content_that_does_not_match_the_type_is_rejected(
    settings: Settings, tmp_path: Path
) -> None:
    source = _wav(tmp_path / "source")
    with pytest.raises(MediaRejectedError) as error:
        await _tools(settings).probe(source, "video/webm")
    assert _rejection(error) == "media_invalid"


@needs_ffmpeg
async def test_video_gives_its_sound_and_video_without_sound_is_rejected(
    settings: Settings, tmp_path: Path
) -> None:
    tools = _tools(settings)
    sound = _wav(tmp_path / "sound.wav")
    with_sound = _video(tmp_path / "with.mp4", sound=sound)
    silent = _video(tmp_path / "silent.mp4", sound=None)

    assert (await tools.probe(with_sound, "video/mp4")).duration_ms > 0
    # Видеодекодера в белом списке нет: звук извлекается, видео не декодируется.
    await tools.to_wav(with_sound, "video/mp4", tmp_path / "audio.wav")
    assert (tmp_path / "audio.wav").stat().st_size > 10_000

    with pytest.raises(MediaRejectedError) as error:
        await tools.probe(silent, "video/mp4")
    assert _rejection(error) == "media_no_audio"


@needs_ffmpeg
async def test_recording_without_duration_in_its_header_is_accepted(
    settings: Settings, tmp_path: Path
) -> None:
    """WebM, записанный в поток (MediaRecorder браузера), длительности в заголовке не несёт."""
    source = tmp_path / "source"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi"]
    command += ["-i", "sine=duration=3", "-c:a", "libopus", "-f", "webm", "pipe:1"]
    source.write_bytes(subprocess.run(command, check=True, capture_output=True).stdout)
    described = subprocess.run(
        ["ffprobe", "-loglevel", "error", "-show_entries", "format=duration", str(source)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "duration=N/A" in described

    info = await _tools(settings).probe(source, "video/webm")

    assert 2900 <= info.duration_ms <= 3100


@needs_ffmpeg
async def test_no_step_opens_a_decoder_of_a_video_track(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каждая команда проверки и извлечения звука повторяется с отладочным выводом."""
    started: list[tuple[Any, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    tools = _tools(settings)
    source = _video(tmp_path / "source.mp4", sound=_wav(tmp_path / "sound.wav"))
    silent = _video(tmp_path / "silent.mp4", sound=None)

    await tools.probe(source, "video/mp4")
    await tools.to_wav(source, "video/mp4", tmp_path / "audio.wav")
    with pytest.raises(MediaRejectedError):
        await tools.probe(silent, "video/mp4")

    assert {Path(command[0]).name for command in started} == {"ffmpeg", "ffprobe"}
    # Самопроверка после отказа читает свой образец, а не запись: её команды не повторяются.
    for command in (c for c in started if not any("reference.wav" in a for a in c)):
        verbose = ["debug" if argument == "error" else argument for argument in command]
        log = subprocess.run(verbose, cwd=tmp_path, capture_output=True, text=True).stderr
        streams = [line for line in log.splitlines() if "Video: mpeg4" in line]
        # У дорожки с открытым декодером ffmpeg называет формат кадра (`yuv420p`).
        assert all(" none" in line for line in streams), command
        assert "chosen by get_format" not in log, command
        if "ffprobe" not in command[0]:
            assert "Codec (mpeg4) not on whitelist" in log


@needs_ffmpeg
async def test_process_gets_no_environment_and_its_output_is_never_logged(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("PORTAL_LLM_API_KEY", "sk-secret")
    started: list[dict[str, Any]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        started.append(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    broken = tmp_path / "личная запись Иванова"
    broken.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64)

    with caplog.at_level(logging.DEBUG), pytest.raises(MediaRejectedError):
        await _tools(settings).probe(broken, "audio/wav")
    with caplog.at_level(logging.DEBUG), pytest.raises(AudioToolsError):
        await _tools(settings).to_wav(broken, "audio/wav", tmp_path / "out.wav")

    # Проверка: копирование дорожки и разбор контейнера, после отказа — самопроверка на
    # образце (копирование и ffprobe); затем извлечение звука.
    assert len(started) == 5
    for process in started:
        assert process["env"] == {}
        assert process["stderr"] == asyncio.subprocess.DEVNULL
        assert process["stdin"] == asyncio.subprocess.DEVNULL
    # В журнале — только имя программы и код возврата: ни пути, ни сообщений ffmpeg.
    logged = [(record.getMessage(), record.__dict__.get("tool")) for record in caplog.records]
    assert logged == [("media tool failed", "ffmpeg")] * 3
    assert "Иванова" not in caplog.text and "Invalid" not in caplog.text


@needs_ffmpeg
async def test_result_larger_than_the_limit_is_a_failure_not_a_truncated_file(
    settings: Settings, tmp_path: Path
) -> None:
    source = _wav(tmp_path / "source", seconds=2)
    limits = settings.media.ffmpeg.model_copy(update={"output_max_bytes": 10_000})

    with pytest.raises(AudioToolsError):
        await _tools(settings, ffmpeg=limits).to_wav(source, "audio/wav", tmp_path / "out.wav")


async def test_failed_copy_of_a_sound_track_is_not_blamed_on_the_file(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Копия дорожки пишется на том: её сбой при годной дорожке — временный, не `media_invalid`."""
    tools = _tools(settings)
    source = tmp_path / "source"
    copy = track_command(source, "wav", tools._decoders, tmp_path / "track.mka")

    async def run(program: str, arguments: list[str], *_: Any) -> tuple[int, bytes]:
        return (1 if arguments == copy else 0), b""

    monkeypatch.setattr(tools, "_run", run)
    with pytest.raises(AudioToolsError):
        await tools.probe(source, "audio/wav")


@needs_ffmpeg
@pytest.mark.skipif(os.geteuid() == 0, reason="root пишет в каталог без права записи")
async def test_job_directory_without_write_access_is_a_tool_failure(
    settings: Settings, tmp_path: Path
) -> None:
    """Тот же класс отказа, что нехватка места на томе: запись исправна и остаётся на месте."""
    scratch = tmp_path / "job"
    scratch.mkdir()
    source = _wav(scratch / "source")
    scratch.chmod(0o555)
    try:
        with pytest.raises(AudioToolsError):
            await _tools(settings).probe(source, "audio/wav")
    finally:
        scratch.chmod(0o755)
    assert [file.name for file in scratch.iterdir()] == ["source"]


@needs_ffmpeg
async def test_tools_that_fail_on_a_sound_reference_never_reject_a_recording(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой среды (здесь — параметр, которого у ffmpeg нет) роняет и проверку образца."""
    real = ffmpeg_module._input_options

    def unknown_option(*args: Any) -> list[str]:
        return ["-no_such_option", *real(*args)]

    source = _wav(tmp_path / "source")
    broken = tmp_path / "broken"
    broken.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64)
    tools = _tools(settings)
    # С исправными программами негодный файл отклоняется, образец после проверки убран.
    with pytest.raises(MediaRejectedError):
        await tools.probe(broken, "audio/wav")
    assert sorted(file.name for file in tmp_path.iterdir()) == ["broken", "source"]

    monkeypatch.setattr(ffmpeg_module, "_input_options", unknown_option)
    for file in (source, broken):
        with pytest.raises(AudioToolsError):
            await tools.probe(file, "audio/wav")
    assert sorted(file.name for file in tmp_path.iterdir()) == ["broken", "source"]


async def test_missing_program_is_a_failure_of_the_tool(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(AudioToolsError):
        await _tools(settings).probe(tmp_path / "source", "audio/wav")


# --- расшифровка частями ---


def test_parts_are_cut_by_fixed_time_and_marks_start_at_zero() -> None:
    assert part_bounds(70_000, 30_000) == [(0, 30_000), (30_000, 60_000), (60_000, 70_000)]
    assert part_bounds(60_000, 30_000) == [(0, 30_000), (30_000, 60_000)]

    part = transcript_part(
        [SpeechSegment(31_500, 40_000, "Первая реплика."), SpeechSegment(42_000, 50_000, "Вторая.")]
    )
    assert part.text == "Первая реплика. Вторая."
    assert part.marks == ((0, 31_500), (16, 42_000))
    assert part.text[16:] == "Вторая."
    # Таймкод фрагмента — время последней отметки не позже его начала.
    assert [mark_time(part.marks, offset) for offset in (0, 15, 16, 20)] == [
        31_500,
        31_500,
        42_000,
        42_000,
    ]
    assert transcript_part([]) == transcript_part(()) and transcript_part([]).text == ""


def test_stub_recognizer_gives_a_segment_per_thirty_seconds(tmp_path: Path) -> None:
    stub = StubSpeechRecognizer()
    whole = stub.transcribe(tmp_path / "none.wav", 0, 70_000, 2)
    assert [(segment.start_ms, segment.text) for segment in whole] == [
        (0, "Расшифровка-заглушка, отрезок 1."),
        (30_000, "Расшифровка-заглушка, отрезок 2."),
        (60_000, "Расшифровка-заглушка, отрезок 3."),
    ]
    # Нарезка на части нумерацию не меняет: отрезки начинаются там же, что у записи целиком.
    cut = [
        (segment.start_ms, segment.text)
        for start, end in part_bounds(70_000, 45_000)
        for segment in stub.transcribe(tmp_path / "none.wav", start, end, 2)
    ]
    assert cut == [(segment.start_ms, segment.text) for segment in whole]
