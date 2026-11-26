"""Outer worker launcher: Blender binary caching, tcmalloc preload, NVMe staging."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

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


def ensure_linux_blender(version: str, cache_dir: Path, extract_root: Path) -> Path:
    """Download (or reuse from Drive/local cache) and unpack a Linux x64 Blender binary."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    extract_dir = extract_root / f"blender-{version}"
    binary = extract_dir / "blender"
    if binary.is_file():
        return binary

    tar_name = f"blender-{version}-linux-x64.tar.xz"
    cached_tar = cache_dir / tar_name

    if not cached_tar.is_file() or cached_tar.stat().st_size == 0:
        last_err: Exception | None = None
        for url in blender_download_urls(version):
            part = cached_tar.with_suffix(".xz.part")
            try:
                print(f"[blendfleet] Downloading Blender {version} from {url} ...", flush=True)
                urllib.request.urlretrieve(url, part)
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

    extract_dir.mkdir(parents=True, exist_ok=True)
    print(f"[blendfleet] Extracting {cached_tar.name} -> {extract_dir} ...", flush=True)
    with tarfile.open(cached_tar, mode="r:xz") as tf:
        for member in tf.getmembers():
            parts = Path(member.name).parts
            if len(parts) <= 1:
                continue
            member.name = str(Path(*parts[1:]))
            tf.extract(member, path=extract_dir, filter="data")

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


def stage_scene_from_http(
    coordinator_url: str,
    token: str,
    work_dir: Path,
    blend_subpath: str | None = None,
) -> tuple[Path, dict]:
    """Fetch job metadata and cache the `.blend` (or `.zip`) asset locally on fast NVMe."""
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
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt < 6:
                print(
                    f"[blendfleet] Waiting for coordinator /job (attempt {attempt}/6: {exc})...",
                    flush=True,
                )
                time.sleep(min(5.0, float(attempt)))
    if job is None:
        raise RuntimeError(f"Failed to fetch job metadata from {base}/job: {last_exc}") from last_exc

    work_dir.mkdir(parents=True, exist_ok=True)
    scene_name = job["scene_name"]
    expected_sha = job["scene_sha256"]
    local_asset = work_dir / scene_name

    if not local_asset.is_file() or _sha256(local_asset) != expected_sha:
        print(f"[blendfleet] Downloading scene {scene_name} ({job['scene_size']} bytes)...", flush=True)
        part = local_asset.with_suffix(local_asset.suffix + ".part")
        sreq = urllib.request.Request(f"{base}/scene", headers=headers)
        with urllib.request.urlopen(sreq, timeout=300.0) as resp, part.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1024 * 256)
        os.replace(part, local_asset)

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
        with zipfile.ZipFile(asset, "r") as zf:
            zf.extractall(unpack_dir)
        if blend_subpath:
            target = unpack_dir / blend_subpath
            if not target.is_file():
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
    blend_subpath: str | None = None,
) -> int:
    """Resolve Blender + scene on fast local storage and run the persistent bpy worker loop."""
    wid = worker_id or f"{socket.gethostname()}-{secrets.token_hex(3)}"
    cache = Path(cache_dir or (Path.home() / ".cache/blendfleet/blender")).expanduser()
    work = Path(work_dir or (Path.home() / ".cache/blendfleet/work" / wid)).expanduser()

    resolved_blender = find_local_blender(blender_bin)
    if resolved_blender is None:
        if platform.system() == "Linux":
            resolved_blender = ensure_linux_blender(blender_version, cache, work / "bin")
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
    proc = subprocess.run(cmd, env=env, check=False)
    return int(proc.returncode)
