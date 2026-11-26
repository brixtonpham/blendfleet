"""Unit and integration tests for the 1-command headless Colab fleet launcher."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from blendfleet import accounts, cli, fleet

VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_build_colab_runner_script_compiles_and_contains_bootstrap() -> None:
    script = fleet.build_colab_runner_script(
        "https://demo.trycloudflare.com",
        "tok-secret",
        blender_version="5.2.3",
        device="optix",
        worker_id="colab-acc1",
        engine="CYCLES",
        samples=64,
    )
    compile(script, "<colab_runner>", "exec")
    assert "libtcmalloc-minimal4" in script
    assert "git+{REPO_URL}" in script
    assert "https://github.com/brixtonpham/blendfleet.git" in script
    assert "https://demo.trycloudflare.com" in script
    assert "tok-secret" in script
    assert "5.2.3" in script
    assert "optix" in script
    assert "colab-acc1" in script
    assert "'--mode', 'http'" in script


def test_build_colab_run_command_gpu_and_cpu(tmp_path: Path) -> None:
    sess = tmp_path / "sessions.json"
    script = tmp_path / "worker.py"

    gpu_cmd = fleet.build_colab_run_command(
        "/usr/local/bin/colab",
        sess,
        script,
        gpu="L4",
        session_name="blendfleet-a1-1234",
        high_mem=True,
    )
    assert gpu_cmd == [
        "/usr/local/bin/colab",
        "--auth=adc",
        "--config",
        str(sess),
        "run",
        "--timeout",
        "0",
        "--gpu",
        "L4",
        "--high-mem",
        "--session",
        "blendfleet-a1-1234",
        str(script),
    ]

    cpu_cmd = fleet.build_colab_run_command(
        "/usr/local/bin/colab",
        sess,
        script,
        gpu="CPU",
    )
    assert "--gpu" not in cpu_cmd
    assert cpu_cmd[-1] == str(script)

    with pytest.raises(ValueError, match="Unsupported GPU"):
        fleet.build_colab_run_command("/usr/local/bin/colab", sess, script, gpu="RTX9090")


def _write_fake_colab_cli(path: Path, src_root: Path, log_file: Path) -> Path:
    """Create a real executable Python script mimicking ``google-colab-cli``."""
    script = f"""#!{sys.executable}
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, {str(src_root)!r})
from blendfleet.blender_worker import HTTPQueueClient

args = sys.argv[1:]
log_path = Path({str(log_file)!r})

# Record invocation and isolated env
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{
        "args": args,
        "gac": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
        "colab_config": os.environ.get("COLAB_CONFIG", ""),
    }}) + "\\n")

if "stop" in args:
    sys.exit(0)

script_path = Path(args[-1])
text = script_path.read_text(encoding="utf-8")
coord_url = re.search(r"COORDINATOR_URL = '([^']+)'", text).group(1)
token = re.search(r"TOKEN = '([^']+)'", text).group(1)
worker_id = re.search(r"WORKER_ID = '([^']+)'", text).group(1)

print(f"booting fake colab runtime for {{worker_id}}", flush=True)
client = HTTPQueueClient(coord_url, token, worker_id, "Tesla T4")
png_file = script_path.parent / f"{{worker_id}}.png"
png_file.write_bytes({VALID_PNG!r})

while True:
    claim = client.claim()
    st = claim.get("status")
    if st == "done":
        print(f"worker {{worker_id}} done", flush=True)
        sys.exit(0)
    if st == "wait":
        continue
    frame = int(claim["frame"])
    client.upload_frame(frame, claim["lease_id"], png_file, duration_sec=0.05)
    print(f"rendered frame {{frame}}", flush=True)
"""
    path.write_text(script, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


def test_fly_fleet_end_to_end_parallel_colab_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    acc_root = tmp_path / "accounts"
    for alias in ("acc1", "acc2"):
        accounts.save_account_credentials(
            alias,
            refresh_token=f"1//rf-{alias}",
            email=f"{alias}@example.com",
            accounts_dir=acc_root,
        )

    scene = tmp_path / "demo.blend"
    scene.write_bytes(b"BLENDER-v502dummy_scene_bytes")
    out_dir = tmp_path / "frames"
    fleet_tmp = tmp_path / "fleet_cache_tmp"
    log_file = tmp_path / "colab_invocations.jsonl"
    src_root = Path(__file__).resolve().parents[1] / "src"
    fake_colab = _write_fake_colab_cli(tmp_path / "fake_colab", src_root, log_file)

    tunnel_started = False
    tunnel_stopped = False

    class _FakeTunnel:
        def __init__(self, local_url: str, timeout: float = 25.0) -> None:
            self.local_url = local_url

        def start(self) -> str:
            nonlocal tunnel_started
            tunnel_started = True
            return self.local_url

        def stop(self) -> None:
            nonlocal tunnel_stopped
            tunnel_stopped = True

    monkeypatch.setattr(fleet, "QuickTunnel", _FakeTunnel)

    rc = fleet.fly_fleet(
        scene,
        frames_spec="1-6",
        output_dir=out_dir,
        accounts_spec="all",
        gpu="T4",
        poll_interval=0.05,
        accounts_dir=acc_root,
        tmp_dir=fleet_tmp,
        colab_bin=str(fake_colab),
    )

    assert rc == 0
    assert tunnel_started and tunnel_stopped
    for frame in range(1, 7):
        assert (out_dir / f"frame_{frame:04d}.png").is_file()

    # Temp runner directory must be cleaned up after completion.
    assert list(fleet_tmp.iterdir()) == []

    # Verify both accounts ran with isolated credentials and sessions.json.
    invocations = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert len(invocations) == 2
    by_gac = {Path(inv["gac"]).parent.name: inv for inv in invocations}
    assert set(by_gac.keys()) == {"acc1", "acc2"}
    for alias, inv in by_gac.items():
        assert inv["args"][0] == "--auth=adc"
        assert inv["args"][1] == "--config"
        assert inv["args"][2] == str(acc_root / alias / "sessions.json")
        assert inv["colab_config"] == str(acc_root / alias / "sessions.json")
        assert "--gpu" in inv["args"] and "T4" in inv["args"]
        assert str(fleet_tmp) in inv["args"][-1]

    out = capsys.readouterr().out
    assert "[colab:acc1]" in out or "[colab:acc2]" in out
    assert "Fleet render verified and complete." in out


def test_fly_fleet_keyboard_interrupt_stops_remote_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc_root = tmp_path / "accounts"
    accounts.save_account_credentials("acc1", refresh_token="1//rf1", accounts_dir=acc_root)

    scene = tmp_path / "demo.blend"
    scene.write_bytes(b"BLENDER-v502dummy")
    out_dir = tmp_path / "frames"
    fleet_tmp = tmp_path / "fleet_tmp"
    stop_log = tmp_path / "stops.jsonl"

    sleeper = tmp_path / "fake_colab_slow"
    sleeper.write_text(
        f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if "stop" in args:
    with Path({str(stop_log)!r}).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"args": args, "gac": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")}}) + "\\n")
    sys.exit(0)
print("waiting for render", flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    os.chmod(sleeper, stat.S_IRWXU)

    real_sleep = time.sleep
    calls = 0

    def interrupt_on_second_sleep(_sec: float) -> None:
        nonlocal calls
        calls += 1
        real_sleep(0.05)
        if calls == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(fleet.time, "sleep", interrupt_on_second_sleep)

    rc = fleet.fly_fleet(
        scene,
        frames_spec="1-2",
        output_dir=out_dir,
        accounts_spec="acc1",
        gpu="T4",
        public_url="https://custom.example.com",
        poll_interval=0.05,
        accounts_dir=acc_root,
        tmp_dir=fleet_tmp,
        colab_bin=str(sleeper),
    )

    assert rc == 130
    stops = [json.loads(line) for line in stop_log.read_text(encoding="utf-8").splitlines()]
    assert len(stops) == 1
    assert stops[0]["args"][:3] == ["--auth=adc", "--config", str(acc_root / "acc1" / "sessions.json")]
    assert "stop" in stops[0]["args"]
    assert "--session" in stops[0]["args"]


def test_fly_fleet_skips_colab_when_frames_already_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc_root = tmp_path / "accounts"
    accounts.save_account_credentials("acc1", refresh_token="1//rf1", accounts_dir=acc_root)

    scene = tmp_path / "demo.blend"
    scene.write_bytes(b"BLENDER-v502dummy")
    out_dir = tmp_path / "frames"
    out_dir.mkdir()
    (out_dir / "frame_0001.png").write_bytes(VALID_PNG)

    def fail_tunnel(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("QuickTunnel should not be started when all frames are done")

    monkeypatch.setattr(fleet, "QuickTunnel", fail_tunnel)

    rc = fleet.fly_fleet(
        scene,
        frames_spec="1",
        output_dir=out_dir,
        accounts_spec="acc1",
        accounts_dir=acc_root,
        colab_bin="/fake/colab",
    )
    assert rc == 0


def test_fly_fleet_validation_errors(tmp_path: Path) -> None:
    scene = tmp_path / "demo.blend"
    scene.write_bytes(b"BLENDER")

    with pytest.raises(ValueError, match="No workers selected"):
        fleet.fly_fleet(
            scene,
            frames_spec="1-2",
            output_dir=tmp_path / "out",
            accounts_spec="all",
            local_workers=0,
            accounts_dir=tmp_path / "empty_accounts",
        )

    with pytest.raises(ValueError, match="Unsupported GPU"):
        fleet.fly_fleet(
            scene,
            frames_spec="1-2",
            output_dir=tmp_path / "out",
            accounts_spec="none",
            local_workers=1,
            gpu="INVALID",
            accounts_dir=tmp_path / "empty_accounts",
        )


def test_cli_fly_and_account_parser_flags() -> None:
    parser = cli.build_parser()

    fly_args = parser.parse_args(
        [
            "fly",
            "scene.blend",
            "--frames",
            "1-120",
            "--out",
            "./frames",
            "--accounts",
            "acc1,acc2",
            "--gpu",
            "L4",
            "--local-workers",
            "2",
            "--public-url",
            "https://render.example.com",
            "--stitch-out",
            "out.mp4",
            "--fps",
            "30",
            "--engine",
            "EEVEE",
            "--samples",
            "32",
            "--blender-version",
            "5.2.3",
        ]
    )
    assert fly_args.command == "fly"
    assert fly_args.accounts == "acc1,acc2"
    assert fly_args.gpu == "L4"
    assert fly_args.local_workers == 2
    assert fly_args.public_url == "https://render.example.com"
    assert fly_args.stitch_out == "out.mp4"
    assert fly_args.fps == 30.0
    assert fly_args.engine == "EEVEE"
    assert fly_args.samples == 32
    assert fly_args.blender_version == "5.2.3"

    add_args = parser.parse_args(
        ["account", "add", "triad", "--chrome-profile", "Profile 19", "--code", "4/abc", "--no-browser"]
    )
    assert add_args.command == "account"
    assert add_args.account_command == "add"
    assert add_args.alias == "triad"
    assert add_args.chrome_profile == "Profile 19"
    assert add_args.code == "4/abc"
    assert add_args.no_browser is True

    chk_args = parser.parse_args(["account", "check", "--accounts", "triad,alt", "--json"])
    assert chk_args.account_command == "check"
    assert chk_args.accounts == "triad,alt"
    assert chk_args.json is True

    cp_args = parser.parse_args(["account", "chrome-profiles", "--json"])
    assert cp_args.account_command == "chrome-profiles"
    assert cp_args.json is True
