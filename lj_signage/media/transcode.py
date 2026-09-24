"""Transcoding profile for Raspberry Pi 1 + omxplayer on a 1080p TV (SPEC §8).

- MP4 with +faststart; H.264 High, level 4.1, yuv420p.
- 1920×1080, letterboxed/pillarboxed (never stretched); source fps up to 30,
  otherwise 25; interlaced sources are deinterlaced.
- 8 Mb/s target, 10 Mb/s maximum.
- Audio removed unless AUDIO_ENABLED (then AAC-LC 128 kb/s, 48 kHz, stereo).
- The output is validated with ffprobe before the video is marked ready.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from ..config import Settings
from ..models import Video
from ..reconciler.executor import sha256_of

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1920, 1080
MAX_FPS = 30.0
FALLBACK_FPS = "25"
PROBE_TIMEOUT_S = 120
STALL_TIMEOUT_S = 600


class MediaError(Exception):
    """Conversion or validation problem (message in Portuguese, shown in the panel)."""


@dataclass
class Probe:
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    fps_expr: str | None = None
    video_codec: str | None = None
    profile: str | None = None
    level: int | None = None
    pix_fmt: str | None = None
    field_order: str | None = None
    has_audio: bool = False
    audio_codec: str | None = None
    audio_rate: int | None = None
    audio_channels: int | None = None
    format_name: str | None = None
    bit_rate: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _rate(expr: str | None) -> float | None:
    if not expr or expr in ("0/0", "0"):
        return None
    try:
        value = float(Fraction(expr))
    except (ValueError, ZeroDivisionError):
        return None
    return value or None


def probe(path: Path) -> Probe:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False
        )
    except FileNotFoundError as exc:
        raise MediaError("O ffprobe não está instalado no servidor.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("A análise do ficheiro demorou demasiado.") from exc
    if done.returncode != 0:
        raise MediaError("O ficheiro não parece ser um vídeo válido.")
    data = json.loads(done.stdout or "{}")
    streams = data.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    result = Probe(format_name=fmt.get("format_name"), has_audio=audio is not None)
    if fmt.get("bit_rate", "").isdigit():
        result.bit_rate = int(fmt["bit_rate"])
    duration = fmt.get("duration") or (video or {}).get("duration")
    try:
        result.duration_s = float(duration) if duration else None
    except ValueError:
        result.duration_s = None
    if video:
        avg = video.get("avg_frame_rate")
        real = video.get("r_frame_rate")
        result.fps_expr = avg if _rate(avg) else real
        result.fps = _rate(result.fps_expr)
        result.width = video.get("width")
        result.height = video.get("height")
        result.video_codec = video.get("codec_name")
        result.profile = video.get("profile")
        result.level = video.get("level")
        result.pix_fmt = video.get("pix_fmt")
        result.field_order = video.get("field_order")
    if audio:
        result.audio_codec = audio.get("codec_name")
        rate = audio.get("sample_rate")
        result.audio_rate = int(rate) if rate and str(rate).isdigit() else None
        result.audio_channels = audio.get("channels")
    return result


def target_fps(source: Probe) -> str:
    """Source frame rate up to 30 fps, otherwise 25 (SPEC §8)."""
    if source.fps and source.fps <= MAX_FPS + 0.01:
        return source.fps_expr or f"{source.fps:g}"
    return FALLBACK_FPS


def ffmpeg_command(src: Path, dst: Path, source: Probe, *, audio: bool) -> list[str]:
    filters = []
    if source.field_order in ("tt", "bb", "tb", "bt"):
        filters.append("yadif")
    filters += [
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos",
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={target_fps(source)}",
        "format=yuv420p",
    ]
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(src)]
    cmd += ["-map", "0:v:0"]
    if audio and source.has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-preset",
        "medium",
        "-b:v",
        "8M",
        "-maxrate",
        "10M",
        "-bufsize",
        "20M",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-sn",
        "-dn",
        "-progress",
        "pipe:1",
        "-nostats",
        str(dst),
    ]
    return cmd


def transcode(
    src: Path,
    dst: Path,
    source: Probe,
    *,
    audio: bool,
    on_progress: Callable[[float], None] | None = None,
    stall_timeout: float = STALL_TIMEOUT_S,
) -> None:
    cmd = ffmpeg_command(src, dst, source, audio=audio)
    last_output = time.monotonic()
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True)
        except FileNotFoundError as exc:
            raise MediaError("O ffmpeg não está instalado no servidor.") from exc

        stalled = threading.Event()

        def watchdog() -> None:
            while proc.poll() is None:
                if time.monotonic() - last_output > stall_timeout:
                    stalled.set()
                    proc.kill()
                    return
                time.sleep(1)

        threading.Thread(target=watchdog, daemon=True).start()
        assert proc.stdout is not None
        for line in proc.stdout:
            last_output = time.monotonic()
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit() and source.duration_s and on_progress:
                on_progress(min(99.0, 100.0 * int(value) / 1e6 / source.duration_s))
        proc.wait()
        if stalled.is_set():
            raise MediaError("A conversão parou de responder e foi interrompida.")
        if proc.returncode != 0:
            errors.seek(0)
            tail = errors.read()[-4000:].decode("utf-8", "replace").strip().splitlines()
            log.warning("ffmpeg failed (%s): %s", proc.returncode, " | ".join(tail[-5:]))
            detail = tail[-1] if tail else f"código {proc.returncode}"
            raise MediaError(f"A conversão falhou ({detail[:200]}).")


def moov_before_mdat(path: Path) -> bool:
    """True when the MP4 index (moov) comes before the media data (faststart)."""
    with path.open("rb") as fh:
        while True:
            header = fh.read(8)
            if len(header) < 8:
                return False
            size = int.from_bytes(header[:4], "big")
            kind = header[4:8]
            if kind == b"moov":
                return True
            if kind == b"mdat":
                return False
            if size == 1:
                size = int.from_bytes(fh.read(8), "big")
                fh.seek(size - 16, os.SEEK_CUR)
            elif size < 8:
                return False
            else:
                fh.seek(size - 8, os.SEEK_CUR)


def validate(path: Path, *, audio: bool) -> Probe:
    """Check the output against the profile with ffprobe (SPEC §8)."""
    result = probe(path)
    problems = []
    if result.video_codec != "h264":
        problems.append(f"codec {result.video_codec} (esperado h264)")
    if (result.profile or "").lower() != "high":
        problems.append(f"perfil {result.profile} (esperado High)")
    if result.level != 41:
        problems.append(f"nível {result.level} (esperado 4.1)")
    if (result.width, result.height) != (WIDTH, HEIGHT):
        problems.append(f"dimensões {result.width}×{result.height} (esperado 1920×1080)")
    if result.pix_fmt != "yuv420p":
        problems.append(f"formato de cor {result.pix_fmt} (esperado yuv420p)")
    if not result.duration_s or result.duration_s <= 0:
        problems.append("duração inválida")
    if result.fps and result.fps > MAX_FPS + 0.01:
        problems.append(f"{result.fps:g} fps (máximo 30)")
    if result.has_audio and not audio:
        problems.append("tem som (o som está desativado)")
    if result.has_audio and audio and result.audio_codec != "aac":
        problems.append(f"som {result.audio_codec} (esperado AAC)")
    if not moov_before_mdat(path):
        problems.append("sem faststart")
    if problems:
        raise MediaError("O ficheiro convertido não cumpre o perfil: " + "; ".join(problems) + ".")
    return result


def make_thumbnail(src: Path, dst: Path, duration: float | None) -> bool:
    at = min(1.0, (duration or 0) / 3)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{at:.2f}",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-vf",
        "scale=480:-2",
        "-q:v",
        "4",
        str(dst),
    ]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and dst.exists()


def process_video(
    video: Video, settings: Settings, on_progress: Callable[[float], None] | None = None
) -> None:
    """Probe, transcode, validate and store a video (runs in the worker).

    Fills the Video row; the caller commits and sets the status.
    """
    if not video.original_path:
        raise MediaError("O ficheiro original não foi encontrado.")
    src = settings.data_dir / video.original_path
    if not src.is_file():
        raise MediaError("O ficheiro original não foi encontrado.")
    source = probe(src)
    if source.video_codec is None:
        raise MediaError("O ficheiro não tem imagem de vídeo.")
    if not source.duration_s:
        raise MediaError("Não foi possível ler a duração do vídeo.")
    video.source_info = source.to_dict()

    tmp = settings.tmp_dir / f"transcode-{video.id}-{uuid.uuid4().hex[:8]}.mp4"
    try:
        transcode(src, tmp, source, audio=settings.audio_enabled, on_progress=on_progress)
        output = validate(tmp, audio=settings.audio_enabled)
        digest = sha256_of(tmp)
        final = settings.media_dir / "videos" / f"{video.id}-{digest[:12]}.mp4"
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)

    thumb = settings.media_dir / "thumbnails" / f"{video.id}.jpg"
    has_thumb = make_thumbnail(final, thumb, output.duration_s)

    old = settings.data_dir / video.media_path if video.media_path else None
    if old and old != final:
        old.unlink(missing_ok=True)
    video.media_path = str(final.relative_to(settings.data_dir))
    video.thumbnail_path = str(thumb.relative_to(settings.data_dir)) if has_thumb else None
    video.sha256 = digest
    video.size_bytes = final.stat().st_size
    video.duration_s = output.duration_s
    video.width, video.height = output.width, output.height
    video.fps = output.fps
