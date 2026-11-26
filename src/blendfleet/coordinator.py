"""Stdlib HTTP work-stealing coordinator for heterogeneous Blender workers.

Zero external dependencies. Serves the `.blend` scene once per worker, hands out frames with
TTL leases, reclaims leases from disconnected Colab/Kaggle tabs automatically, validates
uploaded frames atomically, and optionally exposes itself through a Cloudflare Quick Tunnel.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from blendfleet import __version__
from blendfleet.frames import (
    BLENDER_FORMAT_EXT,
    frame_filename,
    validate_image_bytes,
    validate_image_file,
)


@dataclass
class FrameLease:
    frame: int
    lease_id: str
    worker_id: str
    claimed_at: float
    expires_at: float


@dataclass
class WorkerStats:
    worker_id: str
    gpu: str = "unknown"
    host: str = "unknown"
    frames_completed: int = 0
    frames_failed: int = 0
    total_render_sec: float = 0.0
    last_seen: float = field(default_factory=time.time)

    @property
    def avg_sec_per_frame(self) -> float | None:
        if self.frames_completed == 0:
            return None
        return round(self.total_render_sec / self.frames_completed, 2)


class JobState:
    """Thread-safe work-stealing frame state machine."""

    def __init__(
        self,
        scene_path: Path,
        output_dir: Path,
        frames: list[int],
        *,
        token: str | None = None,
        prefix: str = "frame_",
        file_format: str = "PNG",
        pad: int = 4,
        engine: str = "CYCLES",
        samples: int | None = None,
        device: str = "auto",
        lease_ttl: float = 300.0,
        max_retries: int = 3,
    ) -> None:
        self.scene_path = Path(scene_path).resolve()
        if not self.scene_path.is_file():
            raise FileNotFoundError(f"Scene file not found: {self.scene_path}")

        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.token = token or secrets.token_urlsafe(18)
        self.prefix = prefix
        self.file_format = file_format.upper()
        self.ext = BLENDER_FORMAT_EXT.get(self.file_format, "png")
        self.pad = pad
        self.engine = engine.upper()
        self.samples = samples
        self.device = device.lower()
        self.lease_ttl = float(lease_ttl)
        self.max_retries = max_retries
        self.started_at = time.time()

        self.scene_sha256 = self._hash_file(self.scene_path)
        self.scene_size = self.scene_path.stat().st_size

        self._lock = threading.Lock()
        self.all_frames: list[int] = sorted(set(frames))
        self.completed: set[int] = set()
        self.pending: list[int] = []
        self.leases: dict[int, FrameLease] = {}
        self.retries: dict[int, int] = {}
        self.permanent_failures: dict[int, str] = {}
        self.reclaimed_count: int = 0
        self.workers: dict[str, WorkerStats] = {}

        # Pre-scan output_dir so restarts resume cleanly without re-rendering finished frames.
        for f in self.all_frames:
            fpath = self.output_dir / frame_filename(f, prefix=self.prefix, ext=self.ext, pad=self.pad)
            valid, _ = validate_image_file(fpath, ext=self.ext)
            if valid:
                self.completed.add(f)
            else:
                self.pending.append(f)

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _reclaim_expired_locked(self, now: float) -> None:
        expired = [f for f, lease in self.leases.items() if now >= lease.expires_at]
        for frame in sorted(expired):
            del self.leases[frame]
            self.reclaimed_count += 1
            self.retries[frame] = self.retries.get(frame, 0) + 1
            if self.retries[frame] > self.max_retries:
                self.permanent_failures[frame] = "exceeded max lease timeout retries"
            elif frame not in self.completed and frame not in self.pending:
                # Front-load reclaimed frames so tail latency stays short.
                self.pending.insert(0, frame)

    def _touch_worker_locked(self, worker_id: str, gpu: str = "", host: str = "") -> WorkerStats:
        w = self.workers.get(worker_id)
        if w is None:
            w = WorkerStats(worker_id=worker_id, gpu=gpu or "unknown", host=host or "unknown")
            self.workers[worker_id] = w
        else:
            if gpu:
                w.gpu = gpu
            if host:
                w.host = host
            w.last_seen = time.time()
        return w

    def job_info(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "scene_name": self.scene_path.name,
            "scene_sha256": self.scene_sha256,
            "scene_size": self.scene_size,
            "total_frames": len(self.all_frames),
            "prefix": self.prefix,
            "file_format": self.file_format,
            "ext": self.ext,
            "pad": self.pad,
            "engine": self.engine,
            "samples": self.samples,
            "device": self.device,
            "lease_ttl": self.lease_ttl,
        }

    def claim(self, worker_id: str, gpu: str = "", host: str = "") -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._touch_worker_locked(worker_id, gpu=gpu, host=host)
            self._reclaim_expired_locked(now)

            if self.pending:
                frame = self.pending.pop(0)
                lease = FrameLease(
                    frame=frame,
                    lease_id=secrets.token_hex(12),
                    worker_id=worker_id,
                    claimed_at=now,
                    expires_at=now + self.lease_ttl,
                )
                self.leases[frame] = lease
                return {
                    "status": "claimed",
                    "frame": frame,
                    "lease_id": lease.lease_id,
                    "lease_ttl": self.lease_ttl,
                }

            if self.leases:
                return {"status": "wait", "retry_after": 3.0}

            return {"status": "done"}

    def heartbeat(self, worker_id: str, frame: int, lease_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._touch_worker_locked(worker_id)
            lease = self.leases.get(frame)
            if lease is None or lease.lease_id != lease_id or lease.worker_id != worker_id:
                return {"ok": False, "reason": "lease_not_found_or_expired"}
            lease.expires_at = now + self.lease_ttl
            return {"ok": True, "expires_at": lease.expires_at}

    def submit_frame(
        self,
        frame: int,
        data: bytes,
        *,
        worker_id: str,
        lease_id: str,
        duration_sec: float = 0.0,
    ) -> tuple[bool, str]:
        valid, reason = validate_image_bytes(data, self.ext)
        if not valid:
            return False, reason

        dest = self.output_dir / frame_filename(
            frame, prefix=self.prefix, ext=self.ext, pad=self.pad
        )
        part = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")
        part.write_bytes(data)

        with self._lock:
            w = self._touch_worker_locked(worker_id)
            lease = self.leases.get(frame)
            if frame in self.completed:
                with contextlib.suppress(OSError):
                    part.unlink()
                return True, "already_completed"

            # Accept even if lease timed out right before upload completed, so good work isn't wasted.
            os.replace(part, dest)
            if lease is not None and (lease.lease_id == lease_id or lease.worker_id == worker_id) or frame in self.leases:
                del self.leases[frame]
            if frame in self.pending:
                self.pending.remove(frame)
            self.permanent_failures.pop(frame, None)
            self.completed.add(frame)
            w.frames_completed += 1
            if duration_sec > 0:
                w.total_render_sec += duration_sec

        return True, "ok"

    def report_failure(self, worker_id: str, frame: int, lease_id: str, error: str) -> dict[str, Any]:
        with self._lock:
            w = self._touch_worker_locked(worker_id)
            w.frames_failed += 1
            lease = self.leases.get(frame)
            if lease is not None and lease.lease_id == lease_id:
                del self.leases[frame]
            self.retries[frame] = self.retries.get(frame, 0) + 1
            if self.retries[frame] > self.max_retries:
                self.permanent_failures[frame] = error or "worker reported failure"
                return {"requeued": False, "permanent_failure": True}
            if frame not in self.completed and frame not in self.pending:
                self.pending.append(frame)
            return {"requeued": True, "attempt": self.retries[frame]}

    def status(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._reclaim_expired_locked(now)
            total = len(self.all_frames)
            done_count = len(self.completed)
            remaining = len(self.pending) + len(self.leases)
            elapsed = max(0.001, now - self.started_at)

            # Aggregate cluster throughput from workers that have completed at least one frame.
            frames_per_sec = 0.0
            worker_rows = []
            for w in self.workers.values():
                active = (now - w.last_seen) < max(60.0, self.lease_ttl)
                avg = w.avg_sec_per_frame
                if active and avg and avg > 0:
                    frames_per_sec += 1.0 / avg
                worker_rows.append(
                    {
                        "worker_id": w.worker_id,
                        "gpu": w.gpu,
                        "host": w.host,
                        "frames_completed": w.frames_completed,
                        "frames_failed": w.frames_failed,
                        "avg_sec_per_frame": avg,
                        "active": active,
                    }
                )

            eta_sec = round(remaining / frames_per_sec, 1) if frames_per_sec > 0 else None
            return {
                "total": total,
                "completed": done_count,
                "in_progress": len(self.leases),
                "pending": len(self.pending),
                "failed": len(self.permanent_failures),
                "reclaimed": self.reclaimed_count,
                "done": (len(self.pending) == 0 and len(self.leases) == 0),
                "elapsed_sec": round(elapsed, 1),
                "eta_sec": eta_sec,
                "workers": worker_rows,
                "permanent_failures": dict(self.permanent_failures),
            }


def _make_handler(state: JobState) -> type[BaseHTTPRequestHandler]:
    class BlendfleetHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header_tok = self.headers.get("X-Blendfleet-Token", "")
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                header_tok = auth[7:].strip()
            return secrets.compare_digest(header_tok, state.token)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send_json(200, {"ok": True, "version": __version__})
                return
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            if self.path == "/status":
                self._send_json(200, state.status())
                return
            if self.path == "/job":
                self._send_json(200, state.job_info())
                return
            if self.path == "/scene":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(state.scene_size))
                self.send_header("ETag", state.scene_sha256)
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{state.scene_path.name}"'
                )
                self.end_headers()
                with state.scene_path.open("rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, length=1024 * 256)
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            if self.path == "/claim":
                body = self._read_json()
                res = state.claim(
                    worker_id=str(body.get("worker_id", "anon")),
                    gpu=str(body.get("gpu", "")),
                    host=str(body.get("host", "")),
                )
                self._send_json(200, res)
                return
            if self.path == "/heartbeat":
                body = self._read_json()
                res = state.heartbeat(
                    worker_id=str(body.get("worker_id", "")),
                    frame=int(body["frame"]),
                    lease_id=str(body.get("lease_id", "")),
                )
                self._send_json(200, res)
                return
            if self.path == "/fail":
                body = self._read_json()
                res = state.report_failure(
                    worker_id=str(body.get("worker_id", "")),
                    frame=int(body["frame"]),
                    lease_id=str(body.get("lease_id", "")),
                    error=str(body.get("error", "")),
                )
                self._send_json(200, res)
                return

            match = re.fullmatch(r"/frames/(-?\d+)", self.path)
            if match:
                frame = int(match.group(1))
                length = int(self.headers.get("Content-Length", "0"))
                data = self.rfile.read(length) if length > 0 else b""
                worker_id = self.headers.get("X-Blendfleet-Worker", "anon")
                lease_id = self.headers.get("X-Blendfleet-Lease", "")
                try:
                    duration = float(self.headers.get("X-Blendfleet-Duration", "0"))
                except ValueError:
                    duration = 0.0
                ok, reason = state.submit_frame(
                    frame,
                    data,
                    worker_id=worker_id,
                    lease_id=lease_id,
                    duration_sec=duration,
                )
                self._send_json(200 if ok else 400, {"ok": ok, "reason": reason})
                return

            self._send_json(404, {"error": "not_found"})

    return BlendfleetHandler


class CoordinatorServer:
    """Lifecycle wrapper around ``ThreadingHTTPServer`` + ``JobState``."""

    def __init__(self, state: JobState, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = state
        self.host = host
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(state))
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        connect_host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{connect_host}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def __enter__(self) -> CoordinatorServer:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()


class QuickTunnel:
    """Optional Cloudflare Quick Tunnel (``cloudflared``) exposing a local coordinator to Colab."""

    URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

    def __init__(self, local_url: str, timeout: float = 25.0) -> None:
        self.local_url = local_url
        self.timeout = timeout
        self.proc: subprocess.Popen[str] | None = None
        self.public_url: str | None = None

    def start(self) -> str:
        binary = shutil.which("cloudflared")
        if not binary:
            raise RuntimeError(
                "cloudflared is not installed. Install it (`brew install cloudflared` or "
                "`mise use -g github:cloudflare/cloudflared`) or pass a Tailscale/public URL."
            )
        self.proc = subprocess.Popen(
            [binary, "tunnel", "--url", self.local_url, "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + self.timeout
        assert self.proc.stdout is not None
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    break
                continue
            match = self.URL_RE.search(line)
            if match:
                self.public_url = match.group(0)
                return self.public_url
        self.stop()
        raise TimeoutError("Timed out waiting for cloudflared Quick Tunnel URL.")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self) -> QuickTunnel:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
