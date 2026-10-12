"""Белые списки ffmpeg и ffprobe на версии из образа воркера (docs/portal-design.md §8.1).

Тесты бэкенда проверяют их на ffmpeg машины разработчика и пропускаются, если его нет.
Здесь те же команды портала запускаются в контейнере portal-worker — на закреплённой в
образе версии: запись с видеодорожкой проходит проверку, а видеодекодер не открывается.

Запуск: `uv run pytest stand` при поднятом стенде (make dev-up).
"""

import json
from typing import Any

import pytest

from stand.support import compose, ok

# Исполняется в контейнере воркера: команды собирает код портала, белый список декодеров —
# из его настроек. `-loglevel debug` вместо `error` показывает, какие декодеры открывались.
IN_WORKER = """
import asyncio, json, os, subprocess, tempfile
from pathlib import Path

from portal.core.settings import load_settings
from portal.media import ffmpeg
from portal.media.ffmpeg import DEMUXERS, FfmpegAudioTools
from portal.media.ports import AudioToolsError, MediaRejectedError


def run(tool, command):
    command = ["debug" if argument == "error" else argument for argument in command]
    done = subprocess.run([tool, *command], capture_output=True, text=True)
    return {"code": done.returncode, "log": done.stderr}


def raised(call):
    try:
        asyncio.run(call)
    except (AudioToolsError, MediaRejectedError) as error:
        return type(error).__name__
    return None


with tempfile.TemporaryDirectory() as directory:
    source = Path(directory) / "source.mp4"
    track = Path(directory) / "track.mka"
    wav = Path(directory) / "audio.wav"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "mpeg4", "-c:a", "aac",
         "-shortest", str(source)],
        check=True,
    )
    tools = FfmpegAudioTools(load_settings(os.environ).media)
    decoders = tools._decoders
    demuxer = DEMUXERS["video/mp4"]
    on_source = {
        "track": ffmpeg.track_command(source, demuxer, decoders, track),
        "wav": ffmpeg.wav_command(source, demuxer, decoders, wav, 1),
        "container": ffmpeg.container_command(source, demuxer, decoders),
        "audio_presence": ffmpeg.audio_presence_command(source, demuxer, decoders),
        "track_fit": ffmpeg.track_fit_command(source, demuxer, decoders),
    }
    report = {
        "on_source": {name: run("ffmpeg", command) for name, command in on_source.items()},
        "probe": run("ffprobe", ffmpeg.probe_command(track, decoders)),
        "video_allowed": run(
            "ffmpeg", ffmpeg.track_command(source, demuxer, [*decoders, "mpeg4"], track)
        ),
    }

    playlist = Path(directory) / "playlist"
    playlist.write_text(
        f"#EXTM3U\\n#EXT-X-TARGETDURATION:1\\n#EXTINF:1,\\nfile://{wav}\\n#EXT-X-ENDLIST\\n"
    )
    extracted = Path(directory) / "out.wav"
    report["playlist"] = {
        "target_bytes": wav.stat().st_size,
        "probe": raised(tools.probe(playlist, "audio/mpeg")),
        "to_wav": raised(tools.to_wav(playlist, "audio/mpeg", extracted)),
        "extracted_bytes": extracted.stat().st_size if extracted.exists() else 0,
    }
print(json.dumps(report))
"""
REFUSED = "Codec (mpeg4) not on whitelist"
# Строка декодера видео: он выбрал формат пикселей — значит, был открыт.
DECODER_OPENED = "chosen by get_format"


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    """Отчёт сценария из контейнера воркера: один запуск на оба теста."""
    found: dict[str, Any] = json.loads(
        ok(compose("exec", "-T", "portal-worker", "python", "-", stdin=IN_WORKER))
    )
    return found


def test_video_decoder_is_not_opened_on_recording_with_video(report: dict[str, Any]) -> None:
    probe, video_allowed = report["probe"], report["video_allowed"]

    # Все команды, которые получают исходный файл; wav — единственная, что декодирует.
    for name, done in report["on_source"].items():
        assert done["code"] == 0, (name, done["log"])
        assert REFUSED in done["log"], name
        assert DECODER_OPENED not in done["log"], name
    assert probe["code"] == 0, probe["log"]
    assert DECODER_OPENED not in probe["log"]
    # С видеодекодером в белом списке признак открытия появляется — значит, его отсутствие
    # выше означает работу списка, а не то, что ffmpeg перестал так писать.
    assert DECODER_OPENED in video_allowed["log"]


def test_playlist_pointing_at_local_file_is_not_followed(report: dict[str, Any]) -> None:
    playlist = report["playlist"]

    # Файл, на который ссылается плейлист, существует и непуст: отказ — не из-за него.
    assert playlist["target_bytes"] > 0
    assert playlist["probe"] == "MediaRejectedError"
    assert playlist["to_wav"] == "AudioToolsError"
    assert playlist["extracted_bytes"] == 0
