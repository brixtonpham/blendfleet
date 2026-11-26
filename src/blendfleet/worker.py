"""Outer worker launcher: Blender binary caching, tcmalloc preload, NVMe staging."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blendfleet import __version__

BLENDER_MIRRORS: tuple[str, ...] = (
    "https://download.blender.org/release/Blender{major_minor}/blender-{version}-linux-x64.tar.xz",
    "https://ftp.nluug.nl/pub/graphics/blender/release/Blender{major_minor}/blender-{version}-linux-x64.tar.xz",
)


def blender_download_urls(version: str) -> list[str]:
    """Return primary + mirror download URLs for a Linux x64 Blender release."""
    parts = version.strip().split(".")
    if len(parts) < 2:
        raise ValueError(f"Expected Blender version like '5.2.2' or '4.5.1', got {version!r}")
    major_minor = f"{parts[0]}.{parts[1]}"
    return [tpl.format(major_minor=major_minor, version=version.strip()) for tpl in BLENDER_MIRRORS]


def find_local_blender(explicit: str | None = None) -> Path | None:
    """Locate an already-installed Blender binary on macOS, Linux, or Windows."""
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        raise FileNotFoundError(f"Specified Blender binary does not exist: {explicit}")

    on_path = shutil.which("blender")
    # Guard against unpinned mise shims that fail when invoked without a default version.
    if on_path and "mise/shims/blender" not in on_path:
        return Path(on_path)

    # Check common macOS app bundle and mise install paths.
    candidates: list[Path] = [
        Path("/Applications/Blender.app/Contents/MacOS/Blender"),
    ]
    mise_installs = Path.home() / ".local/share/mise/installs/blender"
    if mise_installs.is_dir():
        for ver_dir in sorted(mise_installs.iterdir(), reverse=True):
            for sub in (
                ver_dir / "Blender.app/Contents/MacOS/Blender",
                ver_dir / "blender",
            ):
                candidates.append(sub)

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _download_with_timeout(url: str, dest: Path, *, timeout: float = 120.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"blendfleet/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 256)


def _extract_tar_xz_strip1(archive: Path, dest_dir: Path) -> None:
    """Extract a ``.tar.xz`` archive stripping its leading component safely."""
    tar_bin = shutil.which("tar")
    if tar_bin and platform.system() == "Linux":
        cmd = [tar_bin]
        if shutil.which("xz"):
            cmd.extend(["-I", "xz -T0"])
        cmd.extend(["-xf", str(archive), "--strip-components=1", "-C", str(dest_dir)])
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if res.returncode == 0 and (dest_dir / "blender").is_file():
            return

    with tarfile.open(archive, mode="r:xz") as tf:
        for member in tf.getmembers():
            parts = Path(member.name).parts
            if len(parts) <= 1:
                continue
            member.name = str(Path(*parts[1:]))
            tf.extract(member, path=dest_dir, filter="data")


def ensure_linux_blender(version: str, cache_dir: Path, extract_root: Path | None = None) -> Path:
    """Download (or reuse from Drive/local cache) and unpack a Linux x64 Blender binary."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    effective_extract_root = extract_root if extract_root is not None else (cache_dir / "extracted")
    effective_extract_root.mkdir(parents=True, exist_ok=True)
    extract_dir = effective_extract_root / f"blender-{version}"
    binary = extract_dir / "blender"
    if binary.is_file():
        return binary

    tar_name = f"blender-{version}-linux-x64.tar.xz"
    cached_tar = cache_dir / tar_name

    if not cached_tar.is_file() or cached_tar.stat().st_size == 0:
        last_err: Exception | None = None
        for url in blender_download_urls(version):
            part = cache_dir / f".{tar_name}.{secrets.token_hex(4)}.part"
            try:
                print(f"[blendfleet] Downloading Blender {version} from {url} ...", flush=True)
                _download_with_timeout(url, part, timeout=180.0)
                os.replace(part, cached_tar)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                part.unlink(missing_ok=True)
        if last_err is not None:
            raise RuntimeError(
                f"Failed to download Blender {version} from all mirrors: {last_err}"
            ) from last_err

    staging_dir = effective_extract_root / f".blender-{version}.{secrets.token_hex(4)}.tmp"
    staging_dir.mkdir(parents=True, exist_ok=True)
    print(f"[blendfleet] Extracting {cached_tar.name} -> {extract_dir} ...", flush=True)
    try:
        _extract_tar_xz_strip1(cached_tar, staging_dir)
        if not (staging_dir / "blender").is_file():
            raise FileNotFoundError(
                f"Expected blender binary at {staging_dir / 'blender'} after extraction."
            )
        try:
            os.replace(staging_dir, extract_dir)
        except OSError:
            # Another concurrent worker finished populating extract_dir first.
            if not binary.is_file():
                raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

    if not binary.is_file():
        raise FileNotFoundError(f"Expected blender binary at {binary} after extraction.")
    return binary


def detect_tcmalloc_preload() -> str | None:
    """Locate libtcmalloc_minimal on Linux (prevents glibc OOM crashes on Colab)."""
    if platform.system() != "Linux":
        return None
    for cand in (
        "/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4",
        "/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4.5.9",
        "/usr/lib/libtcmalloc_minimal.so.4",
    ):
        if Path(cand).is_file():
            return cand
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract_zip(archive: Path, unpack_dir: Path) -> None:
    """Extract a ``.zip`` archive while blocking Zip Slip path traversal entries."""
    root = unpack_dir.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            member_path = (root / info.filename).resolve()
            if member_path != root and not member_path.is_relative_to(root):
                raise ValueError(f"Unsafe path in scene archive {archive.name}: {info.filename!r}")
        zf.extractall(root)


def stage_scene_from_http(
    coordinator_url: str,
    token: str,
    work_dir: Path,
    blend_subpath: str | None = None,
    *,
    scene_attempts: int = 4,
) -> tuple[Path, dict]:
    """Fetch job metadata and cache + SHA-256 verify the `.blend`/`.zip` asset on local NVMe."""
    base = coordinator_url.rstrip("/")
    headers = {
        "X-Blendfleet-Token": token,
        "User-Agent": f"blendfleet/{__version__}",
    }

    job: dict | None = None
    last_exc: Exception | None = None
    for attempt in range(1, 7):
        try:
            req = urllib.request.Request(f"{base}/job", headers=headers)
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                job = json.loads(resp.read().decode("utf-8"))
                break
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(f"Coordinator rejected token (HTTP {exc.code}).") from exc
            last_exc = exc
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
        if attempt < 6:
            print(
                f"[blendfleet] Waiting for coordinator /job (attempt {attempt}/6: {last_exc})...",
                flush=True,
            )
            time.sleep(min(5.0, float(attempt)))
    if job is None:
        raise RuntimeError(f"Failed to fetch job metadata from {base}/job: {last_exc}") from last_exc

    work_dir.mkdir(parents=True, exist_ok=True)
    scene_name = Path(job["scene_name"]).name
    expected_sha = str(job["scene_sha256"])
    local_asset = work_dir / scene_name

    if not local_asset.is_file() or _sha256(local_asset) != expected_sha:
        last_dl_err: Exception | None = None
        attempts = max(1, int(scene_attempts))
        for attempt in range(1, attempts + 1):
            print(
                f"[blendfleet] Downloading scene {scene_name} ({job['scene_size']} bytes)...",
                flush=True,
            )
            part = local_asset.with_name(f".{scene_name}.{secrets.token_hex(4)}.part")
            try:
                sreq = urllib.request.Request(f"{base}/scene", headers=headers)
                with urllib.request.urlopen(sreq, timeout=300.0) as resp, part.open("wb") as out:
                    shutil.copyfileobj(resp, out, length=1024 * 256)
                actual_sha = _sha256(part)
                if actual_sha != expected_sha:
                    raise RuntimeError(
                        f"Scene SHA-256 mismatch (expected {expected_sha[:12]}, got {actual_sha[:12]})"
                    )
                os.replace(part, local_asset)
                last_dl_err = None
                break
            except Exception as exc:
                last_dl_err = exc
                part.unlink(missing_ok=True)
                if attempt < attempts:
                    delay = min(10.0, float(2 ** (attempt - 1)))
                    print(
                        f"[blendfleet] Scene download attempt {attempt}/{attempts} failed "
                        f"({exc}); retrying in {delay:.1f}s...",
                        flush=True,
                    )
                    time.sleep(delay)
        if last_dl_err is not None:
            raise RuntimeError(
                f"Failed to download verified scene {scene_name}: {last_dl_err}"
            ) from last_dl_err

    return _resolve_staged_blend(local_asset, work_dir, blend_subpath), job


def stage_scene_from_path(
    scene_path: Path,
    work_dir: Path,
    blend_subpath: str | None = None,
) -> Path:
    """Copy a `.blend` or `.zip` project from Drive/shared mount to local NVMe before rendering."""
    src = Path(scene_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Scene file not found: {src}")

    work_dir.mkdir(parents=True, exist_ok=True)
    local_asset = work_dir / src.name
    if src != local_asset and (
        not local_asset.is_file() or local_asset.stat().st_size != src.stat().st_size
    ):
        shutil.copy2(src, local_asset)
    return _resolve_staged_blend(local_asset, work_dir, blend_subpath)


def _resolve_staged_blend(asset: Path, work_dir: Path, blend_subpath: str | None) -> Path:
    if asset.suffix.lower() == ".blend":
        return asset
    if asset.suffix.lower() == ".zip":
        unpack_dir = work_dir / f"{asset.stem}_unpacked"
        unpack_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract_zip(asset, unpack_dir)
        if blend_subpath:
            target = (unpack_dir / blend_subpath).resolve()
            if not target.is_relative_to(unpack_dir.resolve()) or not target.is_file():
                raise FileNotFoundError(f"Blend file {blend_subpath!r} not found inside {asset.name}")
            return target
        blends = sorted(unpack_dir.rglob("*.blend"))
        if not blends:
            raise FileNotFoundError(f"No .blend file found inside archive {asset.name}")
        return blends[0]
    raise ValueError(f"Unsupported scene archive format: {asset.name} (expected .blend or .zip)")


def run_worker(
    *,
    mode: str,
    worker_id: str | None = None,
    blender_bin: str | None = None,
    blender_version: str = "5.2.3",
    cache_dir: Path | None = None,
    work_dir: Path | None = None,
    coordinator_url: str = "",
    token: str = "",
    scene_path: Path | None = None,
    output_dir: Path | None = None,
    frames: str = "",
    prefix: str = "frame_",
    file_format: str | None = None,
    pad: int = 4,
    engine: str | None = None,
    device: str | None = None,
    samples: int | None = None,
    worker_index: int = 0,
    total_workers: int = 1,
    lease_ttl: float = 300.0,
    use_cpu_with_gpu: bool = False,
    no_persistent_data: bool = False,
    blend_subpath: str | None = None,
    _on_spawn: Any | None = None,
) -> int:
    """Resolve Blender + scene on fast local storage and run the persistent bpy worker loop."""
    wid = worker_id or f"{socket.gethostname()}-{secrets.token_hex(3)}"
    cache = Path(cache_dir or (Path.home() / ".cache/blendfleet/blender")).expanduser()
    ephemeral_work = work_dir is None
    work = Path(work_dir or (Path.home() / ".cache/blendfleet/work" / wid)).expanduser()

    try:
        resolved_blender = find_local_blender(blender_bin)
        if resolved_blender is None:
            if platform.system() == "Linux":
                resolved_blender = ensure_linux_blender(blender_version, cache)
            else:
                raise FileNotFoundError(
                    "Blender binary not found. Pass --blender /path/to/blender or install Blender."
                )

        if mode == "http":
            if not coordinator_url or not token:
                raise ValueError("HTTP worker mode requires --coordinator-url and --token.")
            local_blend, job = stage_scene_from_http(
                coordinator_url, token, work, blend_subpath=blend_subpath
            )
            eff_engine = engine or job.get("engine", "CYCLES")
            eff_device = device or job.get("device", "auto")
            eff_format = file_format or job.get("file_format", "PNG")
            eff_samples = samples if samples is not None else job.get("samples")
        else:
            if scene_path is None or output_dir is None or not frames:
                raise ValueError("Shared FS worker mode requires --scene, --out, and --frames.")
            local_blend = stage_scene_from_path(scene_path, work, blend_subpath=blend_subpath)
            eff_engine = engine or "CYCLES"
            eff_device = device or "auto"
            eff_format = file_format or "PNG"
            eff_samples = samples

        worker_script = Path(__file__).resolve().parent / "blender_worker.py"
        cmd: list[str] = [
            str(resolved_blender),
            "-b",
            str(local_blend),
            "--python-exit-code",
            "1",
            "-P",
            str(worker_script),
            "--",
            "--mode",
            mode,
            "--worker-id",
            wid,
            "--engine",
            eff_engine,
            "--device",
            eff_device,
            "--format",
            eff_format,
        ]
        if eff_samples is not None:
            cmd.extend(["--samples", str(eff_samples)])
        if use_cpu_with_gpu:
            cmd.append("--use-cpu-with-gpu")
        if no_persistent_data:
            cmd.append("--no-persistent-data")

        if mode == "http":
            cmd.extend(["--coordinator-url", coordinator_url, "--token", token])
        else:
            cmd.extend(
                [
                    "--output-dir",
                    str(Path(output_dir).resolve()),  # type: ignore[arg-type]
                    "--frames",
                    frames,
                    "--prefix",
                    prefix,
                    "--pad",
                    str(pad),
                    "--worker-index",
                    str(worker_index),
                    "--total-workers",
                    str(total_workers),
                    "--lease-ttl",
                    str(lease_ttl),
                ]
            )

        env = os.environ.copy()
        tcmalloc = detect_tcmalloc_preload()
        if tcmalloc and not env.get("LD_PRELOAD"):
            env["LD_PRELOAD"] = tcmalloc

        print(f"[blendfleet] Starting persistent Blender worker {wid} -> {resolved_blender}", flush=True)
        proc = subprocess.Popen(cmd, env=env)
        if _on_spawn is not None:
            _on_spawn(proc)
        return int(proc.wait())
    finally:
        if ephemeral_work and work.exists():
            shutil.rmtree(work, ignore_errors=True)


@dataclass
class LocalWorkerHandle:
    """Lifecycle handle for a local Blender worker thread so Ctrl+C terminates its child process."""

    worker_id: str
    kwargs: dict[str, Any]
    proc: subprocess.Popen[Any] | None = None
    thread: threading.Thread | None = None
    returncode: int | None = None
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _terminated: bool = field(default=False, repr=False)

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run,
            name=f"local-worker-{self.worker_id}",
            daemon=True,
        )
        self.thread.start()

    def _on_spawn(self, proc: subprocess.Popen[Any]) -> None:
        with self._lock:
            self.proc = proc
            already_stopped = self._terminated
        if already_stopped and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()

    def _run(self) -> None:
        try:
            self.returncode = run_worker(
                worker_id=self.worker_id,
                _on_spawn=self._on_spawn,
                **self.kwargs,
            )
        except Exception as exc:
            self.error = str(exc)
            self.returncode = 1
            print(f"[blendfleet:{self.worker_id}] error: {exc}", flush=True)

    @property
    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def join(self, timeout: float = 10.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def terminate(self) -> None:
        with self._lock:
            self._terminated = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
