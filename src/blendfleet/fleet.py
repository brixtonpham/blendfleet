"""One-command headless multi-account Google Colab + local GPU fleet launcher.

Spins up a local work-stealing ``CoordinatorServer``, exposes it via ``QuickTunnel`` (or a
user-provided public URL), launches isolated ``google-colab-cli`` GPU workers across all
selected saved accounts in parallel, optionally mixes in local Blender workers, tracks
progress, cleans up remote sessions on completion or interrupt, and optionally stitches MP4.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blendfleet import __version__
from blendfleet.accounts import (
    account_env,
    find_colab_binary,
    resolve_accounts,
    sessions_path,
)
from blendfleet.coordinator import CoordinatorServer, JobState, QuickTunnel
from blendfleet.frames import parse_frame_spec, verify_frame_sequence
from blendfleet.stitch import stitch_frames
from blendfleet.worker import run_worker

DEFAULT_REPO_URL = "https://github.com/brixtonpham/blendfleet.git"
FLEET_TMP_DIR = Path.home() / ".cache" / "blendfleet" / "tmp"
SUPPORTED_GPUS: tuple[str, ...] = ("T4", "L4", "G4", "A100", "H100", "CPU")


def build_colab_runner_script(
    coordinator_url: str,
    token: str,
    *,
    blender_version: str = "5.2.3",
    device: str = "auto",
    repo_url: str = DEFAULT_REPO_URL,
    worker_id: str | None = None,
    cache_dir: str = "/content/blendfleet_cache",
    engine: str | None = None,
    samples: int | None = None,
) -> str:
    """Generate a standalone Python runner script executed on a fresh Colab VM via ``colab run``."""
    lines: list[str] = [
        '"""Auto-generated blendfleet headless Colab worker runner."""',
        "from __future__ import annotations",
        "",
        "import os",
        "import shutil",
        "import subprocess",
        "import sys",
        "",
        f"COORDINATOR_URL = {coordinator_url!r}",
        f"TOKEN = {token!r}",
        f"BLENDER_VERSION = {blender_version!r}",
        f"DEVICE = {device!r}",
        f"REPO_URL = {repo_url!r}",
        f"WORKER_ID = {worker_id!r}",
        f"CACHE_DIR = {cache_dir!r}",
        f"ENGINE = {engine!r}",
        f"SAMPLES = {samples!r}",
        "",
        "",
        "def _run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> int:",
        "    proc = subprocess.run(cmd, check=False, env=env)",
        "    if check and proc.returncode != 0:",
        "        raise SystemExit(proc.returncode)",
        "    return int(proc.returncode)",
        "",
        "",
        "def main() -> int:",
        "    if shutil.which('nvidia-smi'):",
        "        _run(",
        "            ['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'],",
        "            check=False,",
        "        )",
        "    # Pre-install libtcmalloc-minimal4 to prevent Colab glibc OOM crashes during Cycles renders.",
        "    if shutil.which('apt-get'):",
        "        _run(",
        "            [",
        "                'bash',",
        "                '-lc',",
        "                'apt-get -qq update && apt-get -qq install -y libtcmalloc-minimal4 >/dev/null 2>&1 || true',",
        "            ],",
        "            check=False,",
        "        )",
        "    # Install blendfleet from GitHub (100% Python stdlib core).",
        "    _run([sys.executable, '-m', 'pip', '-q', 'install', '--upgrade', f'git+{REPO_URL}'])",
        "",
        "    blendfleet_bin = shutil.which('blendfleet')",
        "    worker_cmd = ([blendfleet_bin] if blendfleet_bin else [sys.executable, '-m', 'blendfleet.cli']) + [",
        "        'worker',",
        "        '--mode', 'http',",
        "        '--coordinator-url', COORDINATOR_URL,",
        "        '--token', TOKEN,",
        "        '--blender-version', BLENDER_VERSION,",
        "        '--cache-dir', CACHE_DIR,",
        "        '--device', DEVICE,",
        "    ]",
        "    if WORKER_ID:",
        "        worker_cmd.extend(['--worker-id', WORKER_ID])",
        "    if ENGINE:",
        "        worker_cmd.extend(['--engine', ENGINE])",
        "    if SAMPLES is not None:",
        "        worker_cmd.extend(['--samples', str(SAMPLES)])",
        "",
        "    env = os.environ.copy()",
        "    env['PYTHONUNBUFFERED'] = '1'",
        "    return _run(worker_cmd, check=False, env=env)",
        "",
        "",
        "if __name__ == '__main__':",
        "    raise SystemExit(main())",
    ]
    return "\n".join(lines) + "\n"


def build_colab_run_command(
    colab_bin: str,
    sessions_file: Path,
    script_path: Path,
    *,
    gpu: str = "T4",
    session_name: str | None = None,
    high_mem: bool = False,
) -> list[str]:
    """Build the ``colab --auth=adc --config <sessions.json> run ...`` command."""
    gpu_upper = gpu.strip().upper()
    if gpu_upper not in SUPPORTED_GPUS:
        raise ValueError(f"Unsupported GPU {gpu!r}. Expected one of: {', '.join(SUPPORTED_GPUS)}.")

    cmd: list[str] = [
        colab_bin,
        "--auth=adc",
        "--config",
        str(sessions_file),
        "run",
        "--timeout",
        "0",
    ]
    if gpu_upper != "CPU":
        cmd.extend(["--gpu", gpu_upper])
    if high_mem:
        cmd.append("--high-mem")
    if session_name:
        cmd.extend(["--session", session_name])
    cmd.append(str(script_path))
    return cmd


def stop_colab_session(
    colab_bin: str,
    sessions_file: Path,
    env: dict[str, str],
    *,
    session_name: str | None = None,
    timeout: float = 15.0,
) -> None:
    """Best-effort teardown of a remote Colab session so no VM lingers after interrupt."""
    cmd = [colab_bin, "--auth=adc", "--config", str(sessions_file), "stop"]
    if session_name:
        cmd.extend(["--session", session_name])
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )


@dataclass
class ColabWorkerHandle:
    """Lifecycle handle for one headless ``colab run`` worker subprocess."""

    alias: str
    session_name: str
    cmd: list[str]
    sessions_file: Path
    env: dict[str, str]
    proc: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    returncode: int | None = None
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stopped: bool = field(default=False, repr=False)

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run,
            name=f"colab-worker-{self.alias}",
            daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        try:
            proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=self.env,
            )
            with self._lock:
                self.proc = proc
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\r\n")
                if line:
                    print(f"[colab:{self.alias}] {line}", flush=True)
            self.returncode = int(proc.wait())
        except Exception as exc:
            self.error = str(exc)
            self.returncode = 1
            print(f"[colab:{self.alias}] error: {exc}", file=sys.stderr, flush=True)

    @property
    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def join(self, timeout: float = 10.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def terminate_and_stop(self, colab_bin: str, *, stop_remote: bool = True) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
        if stop_remote:
            stop_colab_session(
                colab_bin,
                self.sessions_file,
                self.env,
                session_name=self.session_name,
            )


def _cleanup_handles(
    handles: list[ColabWorkerHandle],
    colab_bin: str,
    *,
    stop_remote: bool = True,
) -> None:
    if not handles:
        return
    threads = [
        threading.Thread(
            target=h.terminate_and_stop,
            args=(colab_bin,),
            kwargs={"stop_remote": stop_remote},
            daemon=True,
        )
        for h in handles
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20.0)


def _format_progress(st: dict[str, Any]) -> str:
    workers_summary = ", ".join(
        f"{w['worker_id']}({w['gpu']}:{w['frames_completed']})"
        for w in st["workers"]
    )
    eta = f"ETA {st['eta_sec']}s" if st["eta_sec"] is not None else "ETA --"
    return (
        f"[progress] {st['completed']}/{st['total']} done | "
        f"active={st['in_progress']} pending={st['pending']} "
        f"reclaimed={st['reclaimed']} failed={st['failed']} | {eta}"
        + (f" | {workers_summary}" if workers_summary else "")
    )


def fly_fleet(
    scene: Path,
    *,
    frames_spec: str,
    output_dir: Path,
    accounts_spec: str = "all",
    gpu: str = "T4",
    local_workers: int = 0,
    local_device: str | None = None,
    blender_bin: str | None = None,
    blender_version: str = "5.2.3",
    public_url: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    engine: str = "CYCLES",
    device: str = "auto",
    samples: int | None = None,
    file_format: str = "PNG",
    prefix: str = "frame_",
    pad: int = 4,
    lease_ttl: float = 300.0,
    max_retries: int = 3,
    poll_interval: float = 2.0,
    stitch_out: Path | None = None,
    fps: float = 24.0,
    repo_url: str = DEFAULT_REPO_URL,
    high_mem: bool = False,
    accounts_dir: Path | None = None,
    tmp_dir: Path | None = None,
    colab_bin: str | None = None,
) -> int:
    """Launch a 1-command headless multi-account Colab + local GPU render fleet."""
    gpu_upper = gpu.strip().upper()
    if gpu_upper not in SUPPORTED_GPUS:
        raise ValueError(f"Unsupported GPU {gpu!r}. Expected one of: {', '.join(SUPPORTED_GPUS)}.")

    frames = parse_frame_spec(frames_spec)
    aliases = resolve_accounts(accounts_spec, accounts_dir=accounts_dir)
    if not aliases and local_workers <= 0:
        raise ValueError(
            "No workers selected. Add a Colab account (`blendfleet account add <alias>`) "
            "or pass `--local-workers N`."
        )

    resolved_colab_bin = ""
    if aliases:
        resolved_colab_bin = colab_bin or find_colab_binary()

    state = JobState(
        scene_path=Path(scene),
        output_dir=Path(output_dir),
        frames=frames,
        token=token,
        prefix=prefix,
        file_format=file_format,
        pad=pad,
        engine=engine,
        samples=samples,
        device=device,
        lease_ttl=lease_ttl,
        max_retries=max_retries,
    )

    if state.status()["done"]:
        print(
            f"[blendfleet] All {len(frames)} frame(s) already rendered in {state.output_dir}.",
            flush=True,
        )
        if stitch_out is not None:
            out_mp4 = stitch_frames(
                state.output_dir,
                frames,
                Path(stitch_out),
                fps=fps,
                prefix=state.prefix,
                ext=state.ext,
                pad=state.pad,
            )
            print(f"[blendfleet] Stitched video -> {out_mp4}", flush=True)
        return 0

    run_id = secrets.token_hex(4)
    base_tmp = Path(tmp_dir).expanduser() if tmp_dir is not None else FLEET_TMP_DIR
    run_tmp_dir = base_tmp / f"fly-{run_id}"
    run_tmp_dir.mkdir(parents=True, exist_ok=True)

    tunnel: QuickTunnel | None = None
    colab_handles: list[ColabWorkerHandle] = []
    local_threads: list[threading.Thread] = []

    try:
        with CoordinatorServer(state, host=host, port=port) as server:
            remote_url = server.url
            if aliases:
                if public_url:
                    remote_url = public_url.rstrip("/")
                else:
                    tunnel = QuickTunnel(server.url)
                    remote_url = tunnel.start()

            print(f"blendfleet v{__version__} fleet launching", flush=True)
            print(
                f"  Scene        : {state.scene_path.name} ({state.scene_size:,} bytes)",
                flush=True,
            )
            print(
                f"  Frames       : {len(frames)} ({frames[0]}..{frames[-1]}), "
                f"already done={len(state.completed)}",
                flush=True,
            )
            print(f"  Output dir   : {state.output_dir}", flush=True)
            print(f"  Local URL    : {server.url}", flush=True)
            if aliases:
                print(f"  Remote URL   : {remote_url}", flush=True)
                print(f"  Colab fleet  : {', '.join(aliases)} (GPU={gpu_upper})", flush=True)
            if local_workers > 0:
                print(
                    f"  Local workers: {local_workers} (device={local_device or device})",
                    flush=True,
                )

            for i in range(local_workers):
                t = threading.Thread(
                    target=run_worker,
                    kwargs={
                        "mode": "http",
                        "worker_id": f"local-{i}",
                        "blender_bin": blender_bin,
                        "coordinator_url": server.url,
                        "token": state.token,
                        "device": local_device or device,
                    },
                    name=f"local-worker-{i}",
                    daemon=True,
                )
                t.start()
                local_threads.append(t)

            for alias in aliases:
                script_src = build_colab_runner_script(
                    remote_url,
                    state.token,
                    blender_version=blender_version,
                    device=device,
                    repo_url=repo_url,
                    worker_id=f"colab-{alias}",
                    engine=engine,
                    samples=samples,
                )
                script_file = run_tmp_dir / f"worker_{alias}.py"
                script_file.write_text(script_src, encoding="utf-8")

                sess_file = sessions_path(alias, accounts_dir=accounts_dir)
                session_name = f"blendfleet-{alias}-{run_id}"
                cmd = build_colab_run_command(
                    resolved_colab_bin,
                    sess_file,
                    script_file,
                    gpu=gpu_upper,
                    session_name=session_name,
                    high_mem=high_mem,
                )
                env = {**os.environ, **account_env(alias, accounts_dir=accounts_dir)}
                handle = ColabWorkerHandle(
                    alias=alias,
                    session_name=session_name,
                    cmd=cmd,
                    sessions_file=sess_file,
                    env=env,
                )
                handle.start()
                colab_handles.append(handle)

            last_line = ""
            while True:
                st = state.status()
                line = _format_progress(st)
                if line != last_line:
                    print(line, flush=True)
                    last_line = line
                if st["done"]:
                    break

                # Bail out cleanly if every spawned worker exited before the job completed.
                any_local_alive = any(t.is_alive() for t in local_threads)
                any_colab_alive = any(h.is_alive for h in colab_handles)
                if not any_local_alive and not any_colab_alive and st["in_progress"] == 0:
                    print(
                        "[blendfleet] All workers exited before remaining frames completed.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 2

                time.sleep(poll_interval)

            for t in local_threads:
                t.join(timeout=10.0)
            for h in colab_handles:
                h.join(timeout=15.0)
            lingering = [h for h in colab_handles if h.is_alive]
            if lingering:
                _cleanup_handles(lingering, resolved_colab_bin, stop_remote=True)

            final_st = state.status()
            report = verify_frame_sequence(
                state.output_dir,
                frames,
                prefix=state.prefix,
                ext=state.ext,
                pad=state.pad,
            )
            if final_st["failed"] > 0 or not report.ok:
                print(
                    f"[blendfleet] Render incomplete: failed={final_st['failed']} "
                    f"missing={len(report.missing_frames)} corrupted={len(report.corrupted_frames)}",
                    file=sys.stderr,
                    flush=True,
                )
                return 2

            if stitch_out is not None:
                out_mp4 = stitch_frames(
                    state.output_dir,
                    frames,
                    Path(stitch_out),
                    fps=fps,
                    prefix=state.prefix,
                    ext=state.ext,
                    pad=state.pad,
                )
                print(f"[blendfleet] Stitched video -> {out_mp4}", flush=True)

            print("[blendfleet] Fleet render verified and complete.", flush=True)
            return 0
    except KeyboardInterrupt:
        print(
            "\n[blendfleet] Interrupted; stopping Colab VMs and shutting down coordinator...",
            flush=True,
        )
        if colab_handles and resolved_colab_bin:
            _cleanup_handles(colab_handles, resolved_colab_bin, stop_remote=True)
        return 130
    finally:
        if tunnel is not None:
            tunnel.stop()
        shutil.rmtree(run_tmp_dir, ignore_errors=True)
