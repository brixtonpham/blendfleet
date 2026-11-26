"""Frame sequence verification and hardware-accelerated FFmpeg video stitching."""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from blendfleet.frames import frame_filename, verify_frame_sequence


def detect_video_encoder(preferred: str = "auto") -> tuple[str, list[str]]:
    """Choose the best available H.264 encoder for the host platform."""
    if preferred != "auto":
        return preferred, []

    ffmpeg = shutil.which("ffmpeg")
    encoders_out = ""
    if ffmpeg:
        try:
            encoders_out = subprocess.check_output(
                [ffmpeg, "-hide_banner", "-encoders"], text=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            encoders_out = ""

    if platform.system() == "Darwin" and "h264_videotoolbox" in encoders_out:
        return "h264_videotoolbox", ["-b:v", "12M"]

    if platform.system() == "Linux" and shutil.which("nvidia-smi") and "h264_nvenc" in encoders_out:
        return "h264_nvenc", ["-preset", "p5", "-cq", "18"]

    return "libx264", ["-preset", "medium", "-crf", "16"]


def stitch_frames(
    output_dir: Path,
    frames: list[int],
    out_video: Path,
    *,
    fps: float = 24.0,
    prefix: str = "frame_",
    ext: str = "png",
    pad: int = 4,
    encoder: str = "auto",
    linear_exr: bool = False,
) -> Path:
    """Verify every frame in ``frames`` and stitch them into an MP4 using FFmpeg concat demuxer."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed or not on PATH.")

    report = verify_frame_sequence(output_dir, frames, prefix=prefix, ext=ext, pad=pad)
    if not report.ok:
        raise ValueError(
            f"Cannot stitch incomplete/corrupted sequence: "
            f"missing={report.missing_frames[:10]}, corrupted={list(report.corrupted_frames.items())[:5]}"
        )

    out_video = Path(out_video).resolve()
    out_video.parent.mkdir(parents=True, exist_ok=True)
    frame_dur = 1.0 / float(fps)

    # Use FFmpeg concat demuxer so arbitrary/non-contiguous frame specs work cleanly.
    tmp_base = Path.home() / ".cache/blendfleet/tmp"
    tmp_base.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".ffconcat", dir=tmp_base, delete=False, encoding="utf-8"
    ) as tf:
        concat_path = Path(tf.name)
        tf.write("ffconcat version 1.0\n")
        ordered_files = [
            (output_dir / frame_filename(f, prefix=prefix, ext=ext, pad=pad)).resolve()
            for f in frames
        ]
        for fpath in ordered_files:
            escaped = str(fpath).replace("'", "'\\''")
            tf.write(f"file '{escaped}'\n")
            tf.write(f"duration {frame_dur:.9f}\n")
        # Repeat final file per FFmpeg concat demuxer spec so the last frame holds full duration.
        last_escaped = str(ordered_files[-1]).replace("'", "'\\''")
        tf.write(f"file '{last_escaped}'\n")

    try:
        vcodec, codec_flags = detect_video_encoder(encoder)
        cmd: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if linear_exr or ext.lower() == "exr":
            cmd.extend(["-apply_trc", "iec61966_2_1"])
        cmd.extend(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-r",
                str(fps),
                "-c:v",
                vcodec,
                *codec_flags,
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(out_video),
            ]
        )
        subprocess.run(cmd, check=True)
    finally:
        concat_path.unlink(missing_ok=True)

    return out_video
