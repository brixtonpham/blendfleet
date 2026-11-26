"""Persistent Blender-internal worker script (runs INSIDE Blender's embedded Python).

Launch via:
    blender -b scene.blend --python-exit-code 1 -P blender_worker.py -- [args]

Key design property: loads the .blend file and compiles OptiX/CUDA/Metal shaders ONCE,
then pulls frames dynamically in a loop until the job completes (zero per-frame restart tax).
Uses Python stdlib + bpy only so it runs inside any Blender 3.6 / 4.x / 5.x runtime.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Ensure parent package is importable when launched via `blender -P .../blender_worker.py`.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

from blendfleet import __version__  # noqa: E402
from blendfleet.frames import BLENDER_FORMAT_EXT, parse_frame_spec  # noqa: E402
from blendfleet.fs_queue import SharedFSQueue  # noqa: E402

MAX_CONSECUTIVE_CLAIM_ERRORS = 20
UPLOAD_MAX_ATTEMPTS = 4


def detect_gpu_label() -> str:
    """Detect human-readable GPU name for coordinator telemetry."""
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            brand = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True, timeout=3
            ).strip()
            return f"{brand} (Metal)"
        except Exception:
            return "Apple Silicon (Metal)"

    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True,
            timeout=5,
        ).strip()
        if out:
            return out.splitlines()[0].strip()
    except Exception:
        pass
    return f"CPU ({os.cpu_count() or 1} cores)"


def pick_cycles_backend(requested: str, gpu_label: str) -> str:
    """Choose the optimal Cycles compute device type for this hardware.

    - NVIDIA Turing/Ada/Blackwell/RTX GPUs (T4, L4, G4, RTX) -> OPTIX (hardware RT cores).
    - NVIDIA A100/H100/V100/P100/K80 (no RT cores) -> CUDA.
    - Apple Silicon -> METAL.
    """
    req = requested.lower()
    if req in ("optix", "cuda", "metal", "hip", "oneapi", "cpu"):
        return req.upper()

    upper = gpu_label.upper()
    if any(tag in upper for tag in ("APPLE", "METAL")):
        return "METAL"
    if any(tag in upper for tag in ("RTX", "T4", "L4", "G4", "BLACKWELL", "ADA", "L40")):
        return "OPTIX"
    if any(tag in upper for tag in ("NVIDIA", "TESLA", "A100", "H100", "A10", "V100", "P100", "K80")):
        return "CUDA"
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "METAL"
    return "CPU"


def configure_blender_scene(
    engine: str,
    device_pref: str,
    gpu_label: str,
    samples: int | None,
    file_format: str,
    use_cpu_with_gpu: bool = False,
    persistent_data: bool = True,
) -> str:
    """Configure Blender's render engine, compute device, and output format once per worker."""
    import bpy  # type: ignore[import-not-found]

    scene = bpy.context.scene
    eng = engine.upper()

    if eng in ("EEVEE", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        # Blender 4.2–4.5 uses BLENDER_EEVEE_NEXT; Blender 3.x and 5.x use BLENDER_EEVEE.
        available = {
            item.identifier
            for item in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
        }
        if "BLENDER_EEVEE_NEXT" in available:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
        else:
            scene.render.engine = "BLENDER_EEVEE"
        if samples is not None and hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
            scene.eevee.taa_render_samples = samples
        active_backend = f"{scene.render.engine} ({gpu_label})"
    else:
        scene.render.engine = "CYCLES"
        backend = pick_cycles_backend(device_pref, gpu_label)
        if samples is not None:
            scene.cycles.samples = samples
        if hasattr(scene.render, "use_persistent_data"):
            scene.render.use_persistent_data = bool(persistent_data)

        if backend == "CPU":
            scene.cycles.device = "CPU"
            active_backend = "CYCLES/CPU"
        else:
            cprefs = bpy.context.preferences.addons["cycles"].preferences
            chosen = "CPU"
            for candidate in (backend, "CUDA", "METAL"):
                try:
                    cprefs.compute_device_type = candidate
                    cprefs.get_devices()
                    gpu_devs = [d for d in cprefs.devices if d.type != "CPU"]
                    if gpu_devs:
                        for d in cprefs.devices:
                            d.use = (d.type != "CPU") or use_cpu_with_gpu
                        scene.cycles.device = "GPU"
                        chosen = candidate
                        break
                except Exception:
                    continue
            if chosen == "CPU":
                scene.cycles.device = "CPU"
            active_backend = f"CYCLES/{chosen}"

    scene.render.image_settings.file_format = file_format.upper()
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True
    print(
        f"[blendfleet] Configured engine={active_backend} format={file_format} "
        f"persistent_data={persistent_data}",
        flush=True,
    )
    return active_backend


class HTTPQueueClient:
    """Stdlib HTTP client for talking to ``blendfleet serve``."""

    def __init__(self, base_url: str, token: str, worker_id: str, gpu: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.worker_id = worker_id
        self.gpu = gpu
        self.host = socket.gethostname()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "X-Blendfleet-Token": self.token,
            "User-Agent": f"blendfleet/{__version__}",
        }
        if extra:
            h.update(extra)
        return h

    def _post_json(self, path: str, payload: dict, timeout: float = 30.0) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def claim(self) -> dict:
        return self._post_json(
            "/claim",
            {"worker_id": self.worker_id, "gpu": self.gpu, "host": self.host},
        )

    def heartbeat(self, frame: int, lease_id: str) -> None:
        with contextlib.suppress(Exception):
            self._post_json(
                "/heartbeat",
                {"worker_id": self.worker_id, "frame": frame, "lease_id": lease_id},
                timeout=15.0,
            )

    def upload_frame(
        self,
        frame: int,
        lease_id: str,
        path: Path,
        duration_sec: float,
        *,
        max_attempts: int = UPLOAD_MAX_ATTEMPTS,
        retry_backoff: float = 1.0,
    ) -> dict:
        """Upload a rendered frame with exponential retry on transient network/5xx errors."""
        data = path.read_bytes()
        headers = self._headers(
            {
                "Content-Type": "application/octet-stream",
                "X-Blendfleet-Worker": self.worker_id,
                "X-Blendfleet-Lease": lease_id,
                "X-Blendfleet-Duration": f"{duration_sec:.3f}",
            }
        )
        last_exc: Exception | None = None
        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(
                f"{self.base_url}/frames/{frame}",
                data=data,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # Never retry 4xx client errors (400 corrupted image, 401 unauthorized, etc.).
                if exc.code < 500:
                    raise
                last_exc = exc
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc

            if attempt < attempts:
                delay = retry_backoff * (2 ** (attempt - 1))
                print(
                    f"[blendfleet] Upload retry {attempt}/{attempts - 1} for frame {frame} "
                    f"after error ({last_exc}); waiting {delay:.1f}s...",
                    flush=True,
                )
                time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def report_failure(self, frame: int, lease_id: str, error: str) -> None:
        with contextlib.suppress(Exception):
            self._post_json(
                "/fail",
                {
                    "worker_id": self.worker_id,
                    "frame": frame,
                    "lease_id": lease_id,
                    "error": error[:500],
                },
                timeout=15.0,
            )


def render_single_frame(frame: int, scratch_dir: Path, ext: str) -> tuple[Path, float]:
    """Render one frame inside the already-loaded Blender scene."""
    import bpy  # type: ignore[import-not-found]

    scene = bpy.context.scene
    scene.frame_set(frame)
    stem = scratch_dir / f"frame_{frame:06d}"
    scene.render.filepath = str(stem)

    t0 = time.monotonic()
    bpy.ops.render.render(write_still=True)
    elapsed = time.monotonic() - t0

    expected = Path(f"{stem}.{ext}")
    if not expected.is_file():
        # Blender sometimes appends uppercase extension or omits if already present.
        matches = sorted(scratch_dir.glob(f"frame_{frame:06d}.*"))
        if not matches:
            raise FileNotFoundError(f"Blender did not produce output file for frame {frame}")
        expected = matches[0]
    return expected, elapsed


def _scratch_base() -> Path:
    base = Path.home() / ".cache/blendfleet/tmp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def run_http_loop(args: argparse.Namespace, gpu_label: str, ext: str) -> int:
    client = HTTPQueueClient(args.coordinator_url, args.token, args.worker_id, gpu_label)
    rendered = 0
    consecutive_errors = 0

    with tempfile.TemporaryDirectory(prefix="blendfleet_frames_", dir=_scratch_base()) as tmp:
        scratch = Path(tmp)
        while True:
            try:
                claim = client.claim()
                consecutive_errors = 0
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401, 403, 404):
                    print(
                        f"[blendfleet] Fatal coordinator error HTTP {exc.code}; exiting worker.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 2
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_CLAIM_ERRORS:
                    print(
                        f"[blendfleet] Coordinator failed {consecutive_errors}x consecutively "
                        f"(HTTP {exc.code}); exiting worker.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 2
                print(
                    f"[blendfleet] Coordinator HTTP {exc.code} ({consecutive_errors}/{MAX_CONSECUTIVE_CLAIM_ERRORS}); "
                    f"retrying in 5s...",
                    flush=True,
                )
                time.sleep(5.0)
                continue
            except (urllib.error.URLError, OSError) as exc:
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_CLAIM_ERRORS:
                    print(
                        f"[blendfleet] Coordinator unreachable {consecutive_errors}x consecutively "
                        f"({exc}); exiting worker.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 2
                print(
                    f"[blendfleet] Coordinator unreachable ({exc}) "
                    f"({consecutive_errors}/{MAX_CONSECUTIVE_CLAIM_ERRORS}); retrying in 5s...",
                    flush=True,
                )
                time.sleep(5.0)
                continue

            status = claim.get("status")
            if status == "done":
                print(f"[blendfleet] Worker {args.worker_id} finished ({rendered} frames).", flush=True)
                return 0
            if status == "wait":
                time.sleep(float(claim.get("retry_after", 3.0)))
                continue

            frame = int(claim["frame"])
            lease_id = str(claim["lease_id"])
            hb_interval = max(10.0, float(claim.get("lease_ttl", 300.0)) / 3.0)
            stop_hb = threading.Event()

            def _hb_loop(
                _stop: threading.Event = stop_hb,
                _interval: float = hb_interval,
                _frame: int = frame,
                _lease: str = lease_id,
            ) -> None:
                while not _stop.wait(_interval):
                    client.heartbeat(_frame, _lease)

            hb_thread = threading.Thread(target=_hb_loop, daemon=True)
            hb_thread.start()
            try:
                out_file, elapsed = render_single_frame(frame, scratch, ext)
                resp = client.upload_frame(frame, lease_id, out_file, elapsed)
                if not resp.get("ok"):
                    raise RuntimeError(f"Upload rejected: {resp}")
                out_file.unlink(missing_ok=True)
                rendered += 1
                print(
                    f"[blendfleet] Frame {frame} done in {elapsed:.2f}s (total={rendered})",
                    flush=True,
                )
            except Exception as exc:
                print(f"[blendfleet] Frame {frame} FAILED: {exc}", flush=True)
                client.report_failure(frame, lease_id, str(exc))
            finally:
                stop_hb.set()
                hb_thread.join(timeout=1.0)


def run_fs_loop(args: argparse.Namespace, ext: str) -> int:
    frames = parse_frame_spec(args.frames)
    queue = SharedFSQueue(
        output_dir=Path(args.output_dir),
        frames=frames,
        worker_id=args.worker_id,
        worker_index=args.worker_index,
        total_workers=args.total_workers,
        prefix=args.prefix,
        ext=ext,
        pad=args.pad,
        lease_ttl=args.lease_ttl,
    )
    rendered = 0

    with tempfile.TemporaryDirectory(prefix="blendfleet_fs_", dir=_scratch_base()) as tmp:
        scratch = Path(tmp)
        while True:
            frame, all_done = queue.claim_next()
            if all_done:
                print(f"[blendfleet] Worker {args.worker_id} finished ({rendered} frames).", flush=True)
                return 0
            if frame is None:
                time.sleep(3.0)
                continue

            hb_interval = max(10.0, args.lease_ttl / 3.0)
            stop_hb = threading.Event()

            def _hb_loop(
                _stop: threading.Event = stop_hb,
                _interval: float = hb_interval,
                _frame: int = frame,
            ) -> None:
                while not _stop.wait(_interval):
                    queue.heartbeat(_frame)

            hb_thread = threading.Thread(target=_hb_loop, daemon=True)
            hb_thread.start()
            try:
                out_file, elapsed = render_single_frame(frame, scratch, ext)
                dest = queue.complete_frame(frame, out_file)
                out_file.unlink(missing_ok=True)
                rendered += 1
                print(
                    f"[blendfleet] Frame {frame} -> {dest.name} ({elapsed:.2f}s, total={rendered})",
                    flush=True,
                )
            except Exception as exc:
                print(f"[blendfleet] Frame {frame} FAILED: {exc}", flush=True)
                queue.release_lease(frame)
            finally:
                stop_hb.set()
                hb_thread.join(timeout=1.0)


def parse_blender_args(argv: list[str]) -> argparse.Namespace:
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []

    p = argparse.ArgumentParser(prog="blendfleet-blender-worker")
    p.add_argument("--mode", choices=["http", "fs"], required=True)
    p.add_argument("--worker-id", required=True)
    p.add_argument("--engine", default="CYCLES")
    p.add_argument("--device", default="auto")
    p.add_argument("--samples", type=int, default=None)
    p.add_argument("--format", dest="file_format", default="PNG")
    p.add_argument("--use-cpu-with-gpu", action="store_true")
    p.add_argument("--no-persistent-data", action="store_true")

    # HTTP mode args
    p.add_argument("--coordinator-url", default="")
    p.add_argument("--token", default="")

    # Shared FS mode args
    p.add_argument("--output-dir", default="")
    p.add_argument("--frames", default="")
    p.add_argument("--prefix", default="frame_")
    p.add_argument("--pad", type=int, default=4)
    p.add_argument("--worker-index", type=int, default=0)
    p.add_argument("--total-workers", type=int, default=1)
    p.add_argument("--lease-ttl", type=float, default=300.0)
    return p.parse_args(argv)


def main() -> int:
    args = parse_blender_args(sys.argv)
    fmt = args.file_format.upper()
    if fmt not in BLENDER_FORMAT_EXT:
        supported = ", ".join(sorted(BLENDER_FORMAT_EXT))
        raise ValueError(f"Unsupported file format {args.file_format!r}. Supported: {supported}.")

    gpu_label = detect_gpu_label()
    ext = BLENDER_FORMAT_EXT[fmt]
    configure_blender_scene(
        engine=args.engine,
        device_pref=args.device,
        gpu_label=gpu_label,
        samples=args.samples,
        file_format=fmt,
        use_cpu_with_gpu=args.use_cpu_with_gpu,
        persistent_data=not args.no_persistent_data,
    )
    if args.mode == "http":
        return run_http_loop(args, gpu_label, ext)
    return run_fs_loop(args, ext)


if __name__ == "__main__":
    raise SystemExit(main())
