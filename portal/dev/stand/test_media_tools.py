"""Белые списки ffmpeg и ffprobe на версии из образа воркера (docs/portal-design.md §8.1).

Тесты бэкенда проверяют их на ffmpeg машины разработчика и пропускаются, если его нет.
Здесь те же команды портала запускаются в контейнере portal-worker — на закреплённой в
образе версии: запись с видеодорожкой проходит проверку, а видеодекодер не открывается.

Запуск: `uv run pytest stand` при поднятом стенде (make dev-up).
"""

import json

from stand.support import compose, ok

# Исполняется в контейнере воркера: команды собирает код портала, белый список декодеров —
# из его настроек. `-loglevel debug` вместо `error` показывает, какие декодеры открывались.
IN_WORKER = """
import json, os, subprocess, tempfile
from pathlib import Path

from portal.core.settings import load_settings
from portal.media.ffmpeg import DEMUXERS, FfmpegAudioTools, probe_command, track_command


def run(tool, command):
    command = ["debug" if argument == "error" else argument for argument in command]
    done = subprocess.run([tool, *command], capture_output=True, text=True)
    return {"code": done.returncode, "log": done.stderr}


with tempfile.TemporaryDirectory() as directory:
    source = Path(directory) / "source.mp4"
    track = Path(directory) / "track.mka"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "mpeg4", "-c:a", "aac",
         "-shortest", str(source)],
        check=True,
    )
    decoders = FfmpegAudioTools(load_settings(os.environ).media)._decoders
    demuxer = DEMUXERS["video/mp4"]
    report = {
        "track": run("ffmpeg", track_command(source, demuxer, decoders, track)),
        "probe": run("ffprobe", probe_command(track, decoders)),
        "video_allowed": run(
            "ffmpeg", track_command(source, demuxer, [*decoders, "mpeg4"], track)
        ),
    }
print(json.dumps(report))
"""
REFUSED = "Codec (mpeg4) not on whitelist"
# Строка декодера видео: он выбрал формат пикселей — значит, был открыт.
DECODER_OPENED = "chosen by get_format"


def test_video_decoder_is_not_opened_on_recording_with_video() -> None:
    report = json.loads(ok(compose("exec", "-T", "portal-worker", "python", "-", stdin=IN_WORKER)))
    track, probe, video_allowed = report["track"], report["probe"], report["video_allowed"]

    assert track["code"] == 0, track["log"]
    assert REFUSED in track["log"]
    assert DECODER_OPENED not in track["log"]
    assert probe["code"] == 0, probe["log"]
    assert DECODER_OPENED not in probe["log"]
    # С видеодекодером в белом списке признак открытия появляется — значит, его отсутствие
    # выше означает работу списка, а не то, что ffmpeg перестал так писать.
    assert DECODER_OPENED in video_allowed["log"]
