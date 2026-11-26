"""Unit tests for worker staging, Zip Slip protection, LocalWorkerHandle, FS queue races, and FFmpeg stitching."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
import urllib.error
import zipfile
from pathlib import Path
from typing import Any

import pytest

from blendfleet import stitch, worker
from blendfleet.fs_queue import SharedFSQueue

VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_stage_scene_from_http_verifies_sha256_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene_bytes = b"BLENDER-v502valid-scene-payload"
    good_sha = hashlib.sha256(scene_bytes).hexdigest()
    scene_calls = 0

    class _Resp:
        def __init__(self, data: bytes) -> None:
            self._buf = io.BytesIO(data)

        def read(self, n: int = -1) -> bytes:
            return self._buf.read(n)

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float = 30.0) -> _Resp:
        nonlocal scene_calls
        if req.full_url.endswith("/job"):
            payload = {
                "scene_name": "shot.blend",
                "scene_sha256": good_sha,
                "scene_size": len(scene_bytes),
                "engine": "CYCLES",
                "device": "auto",
                "file_format": "PNG",
            }
            return _Resp(json.dumps(payload).encode("utf-8"))
        if req.full_url.endswith("/scene"):
            scene_calls += 1
            if scene_calls == 1:
                # Truncated/corrupted first transfer must be rejected & retried.
                return _Resp(b"CORRUPTED_TRUNCATED")
            return _Resp(scene_bytes)
        raise AssertionError(f"Unexpected URL: {req.full_url}")

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)

    blend_path, job = worker.stage_scene_from_http(
        "https://coord.example", "tok", tmp_path / "work", scene_attempts=3
    )
    assert scene_calls == 2
    assert blend_path.is_file()
    assert blend_path.read_bytes() == scene_bytes
    assert job["scene_name"] == "shot.blend"


def test_zip_slip_blocked_during_scene_extract(tmp_path: Path) -> None:
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../escape.blend", b"EVIL")

    with pytest.raises(ValueError, match="Unsafe path"):
        worker._resolve_staged_blend(evil_zip, tmp_path / "work", None)


def test_ensure_linux_blender_extracts_into_shared_cache(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    tar_path = cache_dir / "blender-5.2.3-linux-x64.tar.xz"
    with tarfile.open(tar_path, "w:xz") as tf:
        info = tarfile.TarInfo("blender-5.2.3-linux-x64/blender")
        payload = b"#!/bin/sh\necho blender\n"
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))

    binary = worker.ensure_linux_blender("5.2.3", cache_dir)
    assert binary == cache_dir / "extracted" / "blender-5.2.3" / "blender"
    assert binary.is_file()
    # Second call reuses extracted binary immediately.
    assert worker.ensure_linux_blender("5.2.3", cache_dir) == binary


def test_local_worker_handle_terminates_child_process(tmp_path: Path) -> None:
    # Spawn a real sleeper process via LocalWorkerHandle._on_spawn and verify terminate() stops it.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    handle = worker.LocalWorkerHandle(worker_id="local-0", kwargs={})
    handle._on_spawn(proc)
    assert proc.poll() is None
    handle.terminate()
    assert proc.poll() is not None


def test_fs_queue_unowned_heartbeat_rejected_and_stale_reclaim_safe(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    q1 = SharedFSQueue(out_dir, [1], worker_id="w1", lease_ttl=0.1)
    q2 = SharedFSQueue(out_dir, [1], worker_id="w2", lease_ttl=10.0)

    f1, done1 = q1.claim_next()
    assert f1 == 1 and not done1
    assert q1.heartbeat(1) is True

    # Let w1's short lease expire so w2 reclaims frame 1.
    time.sleep(0.15)
    f2, done2 = q2.claim_next()
    assert f2 == 1 and not done2

    # Late wakeup from w1 must NOT clobber w2's lease!
    assert q1.heartbeat(1) is False
    lease_data = json.loads(q2.lease_path(1).read_text(encoding="utf-8"))
    assert lease_data["worker_id"] == "w2"


def test_stitch_frames_includes_even_dimension_pad_and_videotoolbox_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "frame_0001.png").write_bytes(VALID_PNG)
    (frames_dir / "frame_0002.png").write_bytes(VALID_PNG)

    recorded_cmds: list[list[str]] = []

    monkeypatch.setattr(stitch.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(stitch.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        stitch.subprocess,
        "check_output",
        lambda *args, **kwargs: " V..... h264_videotoolbox\n",
    )

    def fake_run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"MP4")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(stitch.subprocess, "run", fake_run)

    out_mp4 = stitch.stitch_frames(frames_dir, [1, 2], tmp_path / "out.mp4", fps=24.0)
    assert out_mp4.is_file()
    assert len(recorded_cmds) == 1
    cmd = recorded_cmds[0]
    assert "-vf" in cmd and "pad=ceil(iw/2)*2:ceil(ih/2)*2" in cmd
    assert "h264_videotoolbox" in cmd and "-q:v" in cmd and "65" in cmd


def test_http_worker_fails_fast_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    from blendfleet import blender_worker

    class _Args:
        coordinator_url = "https://coord.example"
        token = "wrong"
        worker_id = "w1"

    def raise_401(self: Any) -> dict:
        raise urllib.error.HTTPError(
            "https://coord.example/claim", 401, "Unauthorized", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(blender_worker.HTTPQueueClient, "claim", raise_401)
    rc = blender_worker.run_http_loop(_Args(), "Tesla T4", "png")  # type: ignore[arg-type]
    assert rc == 2
