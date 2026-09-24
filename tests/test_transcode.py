"""Transcoding profile (SPEC §8). Acceptance of F1: a MOV/ProRes comes out as a
compliant MP4 and passes ffprobe."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lj_signage.extensions import db
from lj_signage.media.transcode import (
    MediaError,
    Probe,
    ffmpeg_command,
    moov_before_mdat,
    probe,
    process_video,
    target_fps,
)
from lj_signage.models import Video
from lj_signage.reconciler.executor import sha256_of

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe não instalados",
)


def make_source(path: Path, *, codec: str, size: str, rate: int, seconds: float = 2.0, audio=True):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}"]
    if codec == "prores":
        cmd += ["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le"]
    else:
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "pcm_s16le" if codec == "prores" else "aac"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, timeout=120)
    return path


def library_video(settings, source: Path) -> Video:
    original = settings.media_dir / "originals" / source.name
    shutil.copy(source, original)
    video = Video(
        title="Teste",
        slug="teste",
        original_filename=source.name,
        original_path=str(original.relative_to(settings.data_dir)),
        status="processing",
    )
    db.session.add(video)
    db.session.commit()
    return video


def test_fps_rule():
    assert target_fps(Probe(fps=25.0, fps_expr="25/1")) == "25/1"
    assert target_fps(Probe(fps=29.97, fps_expr="30000/1001")) == "30000/1001"
    assert target_fps(Probe(fps=50.0, fps_expr="50/1")) == "25"
    assert target_fps(Probe(fps=59.94, fps_expr="60000/1001")) == "25"
    assert target_fps(Probe()) == "25"


def test_command_follows_the_profile():
    cmd = ffmpeg_command(
        Path("in.mov"), Path("out.mp4"), Probe(fps=25.0, fps_expr="25/1"), audio=False
    )
    text = " ".join(cmd)
    for part in (
        "-c:v libx264",
        "-profile:v high",
        "-level:v 4.1",
        "-b:v 8M",
        "-maxrate 10M",
        "-movflags +faststart",
        "-an",
        "force_original_aspect_ratio=decrease",
        "pad=1920:1080",
        "format=yuv420p",
    ):
        assert part in text
    interlaced = ffmpeg_command(
        Path("a"), Path("b"), Probe(fps=25.0, fps_expr="25/1", field_order="tt"), audio=False
    )
    assert "yadif" in " ".join(interlaced)
    with_audio = ffmpeg_command(
        Path("a"), Path("b"), Probe(fps=25.0, fps_expr="25/1", has_audio=True), audio=True
    )
    assert "-c:a aac -b:a 128k -ar 48000 -ac 2" in " ".join(with_audio)


@needs_ffmpeg
def test_prores_mov_becomes_a_compliant_mp4(app, settings, tmp_path):
    source = make_source(tmp_path / "campanha.mov", codec="prores", size="1280x720", rate=50)
    assert probe(source).video_codec == "prores"
    video = library_video(settings, source)
    progress = []
    process_video(video, settings, progress.append)

    output = settings.data_dir / video.media_path
    result = probe(output)
    assert result.video_codec == "h264"
    assert result.profile == "High" and result.level == 41
    assert (result.width, result.height) == (1920, 1080)
    assert result.pix_fmt == "yuv420p"
    assert result.fps == 25.0  # source at 50 fps -> 25
    assert not result.has_audio  # AUDIO_ENABLED=false
    assert result.duration_s == pytest.approx(2.0, abs=0.1)
    assert moov_before_mdat(output)
    assert video.sha256 == sha256_of(output) and video.size_bytes == output.stat().st_size
    assert (settings.data_dir / video.thumbnail_path).stat().st_size > 0
    assert video.source_info["video_codec"] == "prores"
    assert progress and progress[-1] <= 99.0


@needs_ffmpeg
def test_vertical_video_is_pillarboxed_and_keeps_its_frame_rate(app, settings, tmp_path):
    source = make_source(tmp_path / "vertical.mp4", codec="h264", size="720x1280", rate=30)
    video = library_video(settings, source)
    process_video(video, settings)
    result = probe(settings.data_dir / video.media_path)
    assert (result.width, result.height) == (1920, 1080)
    assert result.fps == 30.0


@needs_ffmpeg
def test_audio_when_enabled(app, settings, tmp_path):
    source = make_source(tmp_path / "som.mov", codec="prores", size="640x360", rate=25)
    video = library_video(settings, source)
    process_video(video, replace(settings, audio_enabled=True))
    result = probe(settings.data_dir / video.media_path)
    assert result.has_audio and result.audio_codec == "aac"
    assert result.audio_rate == 48000 and result.audio_channels == 2


@needs_ffmpeg
def test_invalid_file_is_reported(app, settings, tmp_path):
    source = tmp_path / "falso.mov"
    source.write_bytes(b"isto nao e um video")
    video = library_video(settings, source)
    with pytest.raises(MediaError, match="não parece ser um vídeo"):
        process_video(video, settings)
