"""One-command headless multi-account Google Colab + local GPU fleet launcher.

Spins up a local work-stealing ``CoordinatorServer``, exposes it via ``QuickTunnel`` (or a
user-provided public URL), launches isolated ``google-colab-cli`` GPU workers across all
selected saved accounts (with optional per-account GPU tiers and staggering), optionally
mixes in local Blender workers, tracks progress, cleans up both local and remote sessions
on completion or interrupt, and optionally stitches MP4.
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

from blendfleet import __version__
from blendfleet.accounts import (
    account_env,
    clean_account_sessions,
    find_colab_binary,
    parse_account_gpu_specs,
    sessions_path,
)
from blendfleet.coordinator import CoordinatorServer, JobState, QuickTunnel, format_progress
from blendfleet.frames import parse_frame_spec, verify_frame_sequence
from blendfleet.stitch import stitch_frames
from blendfleet.worker import LocalWorkerHandle

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
    drive_cache: bool = False,
    no_persistent_data: bool = False,
) -> str:
    """Generate a standalone Python runner script executed on a fresh Colab VM via ``colab run``.

    Downloads the exact running ``blendfleet`` worker bundle (~25 KB) directly from the
    coordinator's ``/worker-bundle.zip`` endpoint (zero ``pip install git+...`` overhead and
    guaranteed version parity), falling back to ``pip install git+...`` only if the coordinator
    does not serve ``/worker-bundle.zip``.
    """
    effective_cache = "/content/drive/MyDrive/blendfleet_cache" if drive_cache else cache_dir
    lines: list[str] = [
        '"""Auto-generated blendfleet headless Colab worker runner."""',
        "from __future__ import annotations",
        "",
        "import io",
        "import os",
        "import shutil",
        "import subprocess",
        "import sys",
        "import time",
        "import urllib.error",
        "import urllib.request",
        "import zipfile",
        "from pathlib import Path",
        "",
        f"COORDINATOR_URL = {coordinator_url!r}",
        f"TOKEN = {token!r}",
        f"BLENDER_VERSION = {blender_version!r}",
        f"DEVICE = {device!r}",
        f"REPO_URL = {repo_url!r}",
        f"WORKER_ID = {worker_id!r}",
        f"CACHE_DIR = {effective_cache!r}",
        f"ENGINE = {engine!r}",
        f"SAMPLES = {samples!r}",
        f"NO_PERSISTENT_DATA = {no_persistent_data!r}",
        f"USER_AGENT = {f'blendfleet/{__version__}'!r}",
        "",
        "",
        "def _run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> int:",
        "    # Stream child stdout/stderr through Python print() so Jupyter iopub forwards live logs.",
        "    proc = subprocess.Popen(",
        "        cmd,",
        "        stdout=subprocess.PIPE,",
        "        stderr=subprocess.STDOUT,",
        "        text=True,",
        "        bufsize=1,",
        "        env=env,",
        "    )",
        "    assert proc.stdout is not None",
        "    for line in proc.stdout:",
        "        print(line, end='', flush=True)",
        "    rc = int(proc.wait())",
        "    if check and rc != 0:",
        "        raise SystemExit(rc)",
        "    return rc",
        "",
        "",
        "def _bootstrap_worker_bundle(dest: Path) -> bool:",
        "    base = COORDINATOR_URL.rstrip('/')",
        "    headers = {'X-Blendfleet-Token': TOKEN, 'User-Agent': USER_AGENT}",
        "    for attempt in range(1, 5):",
        "        try:",
        "            req = urllib.request.Request(f'{base}/worker-bundle.zip', headers=headers)",
        "            with urllib.request.urlopen(req, timeout=30.0) as resp:",
        "                raw = resp.read()",
        "            if dest.exists():",
        "                shutil.rmtree(dest, ignore_errors=True)",
        "            dest.mkdir(parents=True, exist_ok=True)",
        "            with zipfile.ZipFile(io.BytesIO(raw), 'r') as zf:",
        "                zf.extractall(dest)",
        "            print(f'[blendfleet] Loaded coordinator worker bundle ({len(raw):,} bytes)', flush=True)",
        "            return True",
        "        except urllib.error.HTTPError as exc:",
        "            if exc.code == 404:",
        "                return False",
        "            print(f'[blendfleet] Bundle fetch attempt {attempt}/4 failed: HTTP {exc.code}', flush=True)",
        "        except Exception as exc:",
        "            print(f'[blendfleet] Bundle fetch attempt {attempt}/4 failed: {exc}', flush=True)",
        "        if attempt < 4:",
        "            time.sleep(min(5.0, float(attempt)))",
        "    return False",
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
        "                'test -e /usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4 || "
        "(DEBIAN_FRONTEND=noninteractive timeout 30 apt-get -qq update && "
        "DEBIAN_FRONTEND=noninteractive timeout 45 apt-get -qq install -y libtcmalloc-minimal4) >/dev/null 2>&1 || true',",
        "            ],",
        "            check=False,",
        "        )",
        "",
        "    env = os.environ.copy()",
        "    env['PYTHONUNBUFFERED'] = '1'",
        "    bundle_dir = Path('/content/blendfleet_pkg')",
        "    if _bootstrap_worker_bundle(bundle_dir):",
        "        existing_pp = env.get('PYTHONPATH', '')",
        "        env['PYTHONPATH'] = f'{bundle_dir}:{existing_pp}' if existing_pp else str(bundle_dir)",
        "        worker_base = [sys.executable, '-m', 'blendfleet.cli']",
        "    else:",
        "        _run([sys.executable, '-m', 'pip', '-q', 'install', '--upgrade', f'git+{REPO_URL}'])",
        "        blendfleet_bin = shutil.which('blendfleet')",
        "        worker_base = [blendfleet_bin] if blendfleet_bin else [sys.executable, '-m', 'blendfleet.cli']",
        "",
        "    worker_cmd = worker_base + [",
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
        "    if NO_PERSISTENT_DATA:",
        "        worker_cmd.append('--no-persistent-data')",
        "",
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
) -> bool:
    """Best-effort teardown of a remote Colab session so no VM lingers after interrupt."""
    cmd = [colab_bin, "--auth=adc", "--config", str(sessions_file), "stop"]
    if session_name:
        cmd.extend(["--session", session_name])
    try:
        res = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class ColabWorkerHandle:
    """Lifecycle handle for one headless ``colab run`` worker subprocess."""

    alias: str
    gpu: str
    session_name: str
    cmd: list[str]
    sessions_file: Path
    env: dict[str, str]
    accounts_dir: Path | None = None
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
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**self.env, "PYTHONUNBUFFERED": "1"},
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

    def terminate_and_stop(
        self,
        colab_bin: str,
        *,
        stop_remote: bool = True,
        fallback_clean_remote: bool = True,
    ) -> None:
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
            stopped = stop_colab_session(
                colab_bin,
                self.sessions_file,
                self.env,
                session_name=self.session_name,
            )
            if not stopped and fallback_clean_remote:
                with contextlib.suppress(Exception):
                    clean_account_sessions(self.alias, accounts_dir=self.accounts_dir)


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


def _terminate_local_handles(handles: list[LocalWorkerHandle]) -> None:
    if not handles:
        return
    threads = [threading.Thread(target=h.terminate, daemon=True) for h in handles]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)


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
    stagger: float = 1.5,
    drive_cache: bool = False,
    no_persistent_data: bool = False,
    stitch_out: Path | None = None,
    fps: float = 24.0,
    repo_url: str = DEFAULT_REPO_URL,
    high_mem: bool = False,
    accounts_dir: Path | None = None,
    tmp_dir: Path | None = None,
    colab_bin: str | None = None,
) -> int:
    """Launch a 1-command headless multi-account Colab + local GPU render fleet."""
    account_specs = parse_account_gpu_specs(
        accounts_spec,
        gpu,
        SUPPORTED_GPUS,
        accounts_dir=accounts_dir,
    )
    if not account_specs and local_workers <= 0:
        raise ValueError(
            "No workers selected. Add a Colab account (`blendfleet account add <alias>`) "
            "or pass `--local-workers N`."
        )

    frames = parse_frame_spec(frames_spec)

    resolved_colab_bin = ""
    if account_specs:
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
    local_handles: list[LocalWorkerHandle] = []

    try:
        with CoordinatorServer(state, host=host, port=port) as server:
            remote_url = server.url
            if account_specs:
                if public_url:
                    remote_url = public_url.rstrip("/")
                else:
                    tunnel = QuickTunnel(server.url)
                    remote_url = tunnel.start()

            fleet_desc = ", ".join(f"{alias}:{acc_gpu}" for alias, acc_gpu in account_specs)
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
            if account_specs:
                print(f"  Remote URL   : {remote_url}", flush=True)
                print(f"  Colab fleet  : {fleet_desc}", flush=True)
            if local_workers > 0:
                print(
                    f"  Local workers: {local_workers} (device={local_device or device})",
                    flush=True,
                )

            for i in range(local_workers):
                lh = LocalWorkerHandle(
                    worker_id=f"local-{i}",
                    kwargs={
                        "mode": "http",
                        "blender_bin": blender_bin,
                        "coordinator_url": server.url,
                        "token": state.token,
                        "device": local_device or device,
                        "no_persistent_data": no_persistent_data,
                    },
                )
                lh.start()
                local_handles.append(lh)

            for idx, (alias, acc_gpu) in enumerate(account_specs):
                if idx > 0 and stagger > 0:
                    time.sleep(stagger)
                script_src = build_colab_runner_script(
                    remote_url,
                    state.token,
                    blender_version=blender_version,
                    device=device,
                    repo_url=repo_url,
                    worker_id=f"colab-{alias}",
                    engine=engine,
                    samples=samples,
                    drive_cache=drive_cache,
                    no_persistent_data=no_persistent_data,
                )
                script_file = run_tmp_dir / f"worker_{alias}.py"
                script_file.write_text(script_src, encoding="utf-8")

                sess_file = sessions_path(alias, accounts_dir=accounts_dir)
                session_name = f"blendfleet-{alias}-{run_id}"
                cmd = build_colab_run_command(
                    resolved_colab_bin,
                    sess_file,
                    script_file,
                    gpu=acc_gpu,
                    session_name=session_name,
                    high_mem=high_mem,
                )
                env = {**os.environ, **account_env(alias, accounts_dir=accounts_dir)}
                handle = ColabWorkerHandle(
                    alias=alias,
                    gpu=acc_gpu,
                    session_name=session_name,
                    cmd=cmd,
                    sessions_file=sess_file,
                    env=env,
                    accounts_dir=accounts_dir,
                )
                handle.start()
                colab_handles.append(handle)

            last_line = ""
            while True:
                st = state.status()
                line = format_progress(st)
                if line != last_line:
                    print(line, flush=True)
                    last_line = line
                if st["done"]:
                    break

                # Bail out immediately if every spawned worker exited before the job completed.
                any_local_alive = any(h.is_alive for h in local_handles)
                any_colab_alive = any(h.is_alive for h in colab_handles)
                if not any_local_alive and not any_colab_alive:
                    print(
                        "[blendfleet] All workers exited before remaining frames completed.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 2

                time.sleep(poll_interval)

            for lh in local_handles:
                lh.join(timeout=10.0)
            lingering_local = [lh for lh in local_handles if lh.is_alive]
            if lingering_local:
                _terminate_local_handles(lingering_local)

            for h in colab_handles:
                h.join(timeout=15.0)
            lingering_colab = [h for h in colab_handles if h.is_alive]
            if lingering_colab:
                _cleanup_handles(lingering_colab, resolved_colab_bin, stop_remote=True)

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
            "\n[blendfleet] Interrupted; stopping local workers, Colab VMs, and coordinator...",
            flush=True,
        )
        _terminate_local_handles(local_handles)
        if colab_handles and resolved_colab_bin:
            _cleanup_handles(colab_handles, resolved_colab_bin, stop_remote=True)
        return 130
    finally:
        _terminate_local_handles([lh for lh in local_handles if lh.is_alive])
        if tunnel is not None:
            tunnel.stop()
        shutil.rmtree(run_tmp_dir, ignore_errors=True)
