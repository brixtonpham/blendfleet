"""CLI entrypoint for blendfleet."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from blendfleet import __version__
from blendfleet.accounts import (
    add_account,
    check_all_accounts,
    clean_all_account_sessions,
    list_accounts,
    list_chrome_profiles,
    remove_account,
    resolve_accounts,
)
from blendfleet.colab_bridge import (
    build_colab_cell_snippet,
    launch_colab_profiles,
    write_worker_notebook,
)
from blendfleet.coordinator import CoordinatorServer, JobState, QuickTunnel, format_progress
from blendfleet.fleet import DEFAULT_REPO_URL, SUPPORTED_GPUS, fly_fleet
from blendfleet.frames import BLENDER_FORMAT_EXT, parse_frame_spec, verify_frame_sequence
from blendfleet.stitch import stitch_frames
from blendfleet.worker import LocalWorkerHandle, run_worker


def _cmd_serve(args: argparse.Namespace) -> int:
    frames = parse_frame_spec(args.frames)
    state = JobState(
        scene_path=Path(args.scene),
        output_dir=Path(args.out),
        frames=frames,
        token=args.token,
        prefix=args.prefix,
        file_format=args.file_format,
        pad=args.pad,
        engine=args.engine,
        samples=args.samples,
        device=args.device,
        lease_ttl=args.lease_ttl,
        max_retries=args.max_retries,
    )

    tunnel: QuickTunnel | None = None
    local_handles: list[LocalWorkerHandle] = []

    with CoordinatorServer(state, host=args.host, port=args.port) as server:
        connect_url = server.url
        if args.tunnel:
            tunnel = QuickTunnel(server.url)
            connect_url = tunnel.start()

        try:
            print(f"blendfleet v{__version__} coordinator live", flush=True)
            print(f"  Scene       : {state.scene_path.name} ({state.scene_size:,} bytes)", flush=True)
            print(
                f"  Frames      : {len(frames)} ({frames[0]}..{frames[-1]}), already done={len(state.completed)}",
                flush=True,
            )
            print(f"  Output dir  : {state.output_dir}", flush=True)
            print(f"  Local URL   : {server.url}", flush=True)
            if tunnel:
                print(f"  Public URL  : {connect_url}", flush=True)
            print(f"  Auth Token  : {state.token}", flush=True)
            print("\n--- Paste into any Google Colab / Kaggle cell to attach a GPU worker ---", flush=True)
            print(
                build_colab_cell_snippet(
                    connect_url,
                    state.token,
                    blender_version=args.blender_version,
                    device=args.device,
                ),
                flush=True,
            )

            for i in range(args.local_workers):
                lh = LocalWorkerHandle(
                    worker_id=f"local-{i}",
                    kwargs={
                        "mode": "http",
                        "blender_bin": args.blender,
                        "coordinator_url": server.url,
                        "token": state.token,
                        "device": args.local_device or args.device,
                        "no_persistent_data": args.no_persistent_data,
                    },
                )
                lh.start()
                local_handles.append(lh)

            last_line = ""
            while True:
                st = state.status()
                line = format_progress(st)
                if line != last_line:
                    print(line, flush=True)
                    last_line = line
                if st["done"]:
                    break
                time.sleep(args.poll_interval)

            for lh in local_handles:
                lh.join(timeout=10.0)

            final_st = state.status()
            if final_st["failed"] > 0:
                print(f"[blendfleet] Completed with {final_st['failed']} failed frames.", file=sys.stderr)
                return 2

            if args.stitch_out:
                out_mp4 = stitch_frames(
                    state.output_dir,
                    frames,
                    Path(args.stitch_out),
                    fps=args.fps,
                    prefix=state.prefix,
                    ext=state.ext,
                    pad=state.pad,
                )
                print(f"[blendfleet] Stitched video -> {out_mp4}", flush=True)

            print("[blendfleet] All frames rendered cleanly.", flush=True)
            return 0
        except KeyboardInterrupt:
            print("\n[blendfleet] Interrupted; shutting down local workers and coordinator.", flush=True)
            for lh in local_handles:
                lh.terminate()
            return 130
        finally:
            for lh in local_handles:
                if lh.is_alive:
                    lh.terminate()
            if tunnel is not None:
                tunnel.stop()


def _cmd_worker(args: argparse.Namespace) -> int:
    return run_worker(
        mode=args.mode,
        worker_id=args.worker_id,
        blender_bin=args.blender,
        blender_version=args.blender_version,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        coordinator_url=args.coordinator_url,
        token=args.token,
        scene_path=Path(args.scene) if args.scene else None,
        output_dir=Path(args.out) if args.out else None,
        frames=args.frames,
        prefix=args.prefix,
        file_format=args.file_format,
        pad=args.pad,
        engine=args.engine,
        device=args.device,
        samples=args.samples,
        worker_index=args.worker_index,
        total_workers=args.total_workers,
        lease_ttl=args.lease_ttl,
        use_cpu_with_gpu=args.use_cpu_with_gpu,
        no_persistent_data=args.no_persistent_data,
        blend_subpath=args.blend_subpath,
    )


def _cmd_verify(args: argparse.Namespace) -> int:
    frames = parse_frame_spec(args.frames)
    ext = BLENDER_FORMAT_EXT.get(args.file_format.upper(), args.file_format.lower())
    report = verify_frame_sequence(
        Path(args.out), frames, prefix=args.prefix, ext=ext, pad=args.pad
    )
    payload = {
        "ok": report.ok,
        "expected": len(report.expected_frames),
        "present": len(report.present_frames),
        "missing": report.missing_frames,
        "corrupted": report.corrupted_frames,
    }
    print(json.dumps(payload, indent=2))
    return 0 if report.ok else 1


def _cmd_stitch(args: argparse.Namespace) -> int:
    frames = parse_frame_spec(args.frames)
    ext = BLENDER_FORMAT_EXT.get(args.file_format.upper(), args.file_format.lower())
    out = stitch_frames(
        Path(args.input_dir),
        frames,
        Path(args.out),
        fps=args.fps,
        prefix=args.prefix,
        ext=ext,
        pad=args.pad,
        encoder=args.encoder,
        linear_exr=args.linear_exr,
    )
    print(f"Wrote {out}")
    return 0


def _cmd_notebook(args: argparse.Namespace) -> int:
    out = write_worker_notebook(
        Path(args.out),
        coordinator_url=args.coordinator_url,
        token=args.token,
        blender_version=args.blender_version,
    )
    print(f"Wrote Colab/Kaggle worker notebook -> {out}")
    return 0


def _cmd_colab_launch(args: argparse.Namespace) -> int:
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    results = launch_colab_profiles(profiles, notebook_url=args.notebook_url)
    print(json.dumps(results, indent=2))
    if args.coordinator_url and args.token:
        print("\n--- Worker cell to run in each opened Colab notebook ---")
        print(build_colab_cell_snippet(args.coordinator_url, args.token))
    return 0


def _cmd_account_add(args: argparse.Namespace) -> int:
    result = add_account(
        args.alias,
        code=args.code,
        chrome_profile=args.chrome_profile,
        no_browser=args.no_browser,
    )
    if result.get("status") == "saved":
        email_part = f" ({result['email']})" if result.get("email") else ""
        print(f"[blendfleet] Saved account '{result['alias']}'{email_part}")
        print(f"  Credentials : {result['credentials_path']}")
        print(f"  Sessions    : {result['sessions_path']}")
    return 0


def _cmd_account_list(args: argparse.Namespace) -> int:
    rows = list_accounts()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No saved blendfleet Colab accounts. Add one with: blendfleet account add <alias>")
        return 0
    for row in rows:
        email = row.get("email") or "(unknown email)"
        profile = f" [chrome: {row['chrome_profile']}]" if row.get("chrome_profile") else ""
        print(f"{row['alias']:<18} {email}{profile}")
    return 0


def _cmd_account_check(args: argparse.Namespace) -> int:
    aliases = resolve_accounts(args.accounts)
    if not aliases:
        if args.json:
            print("[]")
        else:
            print("No saved blendfleet Colab accounts to check.")
        return 0

    results = check_all_accounts(aliases)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            status = "OK" if row["ok"] else row["status"].upper()
            email = row.get("email") or "-"
            ccu = row.get("ccu_balance")
            detail = (
                f"CCU={ccu}"
                if ccu is not None
                else (row.get("usage") or "").splitlines()[0]
                if row.get("usage")
                else row.get("error") or ""
            )
            print(f"{row['alias']:<18} {status:<10} {email:<34} {detail}")
    return 0 if all(r["ok"] for r in results) else 1


def _cmd_account_clean_sessions(args: argparse.Namespace) -> int:
    aliases = resolve_accounts(args.accounts)
    if not aliases:
        if args.json:
            print("[]")
        else:
            print("No saved blendfleet Colab accounts to clean.")
        return 0

    results = clean_all_account_sessions(aliases, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            if not row["ok"]:
                print(f"{row['alias']:<18} ERROR      {row.get('error') or ''}")
                continue
            found = row.get("found") or []
            unassigned = row.get("unassigned") or []
            if not found:
                print(f"{row['alias']:<18} CLEAN      0 active Colab runtimes")
            elif args.dry_run:
                print(
                    f"{row['alias']:<18} ACTIVE     {len(found)} runtime(s): {', '.join(found)}"
                )
            else:
                print(
                    f"{row['alias']:<18} UNASSIGNED {len(unassigned)}/{len(found)}: {', '.join(unassigned)}"
                )
    return 0 if all(r["ok"] for r in results) else 1


def _cmd_account_remove(args: argparse.Namespace) -> int:
    removed = remove_account(args.alias)
    if not removed:
        print(f"[blendfleet] Account '{args.alias}' not found.", file=sys.stderr)
        return 1
    print(f"[blendfleet] Removed account '{args.alias}'.")
    return 0


def _cmd_account_chrome_profiles(args: argparse.Namespace) -> int:
    profiles = list_chrome_profiles()
    if args.json:
        print(json.dumps(profiles, indent=2))
        return 0
    if not profiles:
        print("No Chrome profiles found.")
        return 0
    for prof in profiles:
        email_part = f" ({prof['email']})" if prof["email"] else ""
        print(f"{prof['profile_directory']:<14} -> {prof['name']}{email_part}")
    return 0


def _cmd_fly(args: argparse.Namespace) -> int:
    return fly_fleet(
        Path(args.scene),
        frames_spec=args.frames,
        output_dir=Path(args.out),
        accounts_spec=args.accounts,
        gpu=args.gpu,
        local_workers=args.local_workers,
        local_device=args.local_device,
        blender_bin=args.blender,
        blender_version=args.blender_version,
        public_url=args.public_url,
        host=args.host,
        port=args.port,
        token=args.token,
        engine=args.engine,
        device=args.device,
        samples=args.samples,
        file_format=args.file_format,
        prefix=args.prefix,
        pad=args.pad,
        lease_ttl=args.lease_ttl,
        max_retries=args.max_retries,
        poll_interval=args.poll_interval,
        stagger=args.stagger,
        drive_cache=args.drive_cache,
        no_persistent_data=args.no_persistent_data,
        stitch_out=Path(args.stitch_out) if args.stitch_out else None,
        fps=args.fps,
        repo_url=args.repo_url,
        high_mem=args.high_mem,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="blendfleet",
        description="Heterogeneous distributed Blender render farm for ephemeral Colab, Kaggle, and local GPUs.",
    )
    p.add_argument("--version", action="version", version=f"blendfleet {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    # serve
    sv = sub.add_parser("serve", help="Start HTTP work-stealing coordinator (+ optional local workers & tunnel)")
    sv.add_argument("scene", help="Path to .blend or .zip project archive")
    sv.add_argument("--frames", required=True, help="Frame spec, e.g. '1-240' or '1-10, 20, 35-55'")
    sv.add_argument("--out", required=True, help="Directory to store verified rendered frames")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--token", default=None, help="Shared bearer token (auto-generated if omitted)")
    sv.add_argument("--tunnel", action="store_true", help="Expose via cloudflared Quick Tunnel for Colab/Kaggle")
    sv.add_argument("--local-workers", type=int, default=0, help="Spawn N local persistent Blender workers alongside remote GPUs")
    sv.add_argument("--local-device", default=None, help="Device override for local workers (e.g. metal, optix, cpu)")
    sv.add_argument("--blender", default=None, help="Explicit path to local Blender binary")
    sv.add_argument("--blender-version", default="5.2.3", help="Target Linux Blender version for Colab workers")
    sv.add_argument("--engine", default="CYCLES", choices=["CYCLES", "EEVEE"])
    sv.add_argument("--device", default="auto", choices=["auto", "optix", "cuda", "metal", "cpu"])
    sv.add_argument("--samples", type=int, default=None)
    sv.add_argument("--format", dest="file_format", default="PNG")
    sv.add_argument("--prefix", default="frame_")
    sv.add_argument("--pad", type=int, default=4)
    sv.add_argument("--lease-ttl", type=float, default=300.0)
    sv.add_argument("--max-retries", type=int, default=3)
    sv.add_argument("--poll-interval", type=float, default=2.0)
    sv.add_argument("--no-persistent-data", action="store_true", help="Disable Cycles use_persistent_data for animated-topology/low-VRAM scenes")
    sv.add_argument("--stitch-out", default=None, help="Optional MP4 path to stitch automatically when complete")
    sv.add_argument("--fps", type=float, default=24.0)
    sv.set_defaults(func=_cmd_serve)

    # worker
    wk = sub.add_parser("worker", help="Join a blendfleet coordinator (HTTP) or shared Drive/FS job")
    wk.add_argument("--mode", choices=["http", "fs"], default="http")
    wk.add_argument("--worker-id", default=None)
    wk.add_argument("--coordinator-url", default="")
    wk.add_argument("--token", default="")
    wk.add_argument("--scene", default=None, help="Scene path (.blend/.zip) for --mode fs")
    wk.add_argument("--out", default=None, help="Shared output dir for --mode fs")
    wk.add_argument("--frames", default="", help="Frame spec for --mode fs")
    wk.add_argument("--prefix", default="frame_")
    wk.add_argument("--pad", type=int, default=4)
    wk.add_argument("--worker-index", type=int, default=0, help="0-based worker shard index for --mode fs")
    wk.add_argument("--total-workers", type=int, default=1, help="Total concurrent workers for --mode fs")
    wk.add_argument("--lease-ttl", type=float, default=300.0)
    wk.add_argument("--blender", default=None)
    wk.add_argument("--blender-version", default="5.2.3")
    wk.add_argument("--cache-dir", default=None)
    wk.add_argument("--work-dir", default=None)
    wk.add_argument("--engine", default=None)
    wk.add_argument("--device", default=None)
    wk.add_argument("--samples", type=int, default=None)
    wk.add_argument("--format", dest="file_format", default=None)
    wk.add_argument("--use-cpu-with-gpu", action="store_true")
    wk.add_argument("--no-persistent-data", action="store_true")
    wk.add_argument("--blend-subpath", default=None)
    wk.set_defaults(func=_cmd_worker)

    # verify
    vf = sub.add_parser("verify", help="Verify frame completeness and image header integrity")
    vf.add_argument("out", help="Directory containing rendered frames")
    vf.add_argument("--frames", required=True)
    vf.add_argument("--prefix", default="frame_")
    vf.add_argument("--format", dest="file_format", default="PNG")
    vf.add_argument("--pad", type=int, default=4)
    vf.set_defaults(func=_cmd_verify)

    # stitch
    st = sub.add_parser("stitch", help="Verify frames and encode to MP4 with hardware-accelerated FFmpeg")
    st.add_argument("input_dir", help="Directory containing rendered frames")
    st.add_argument("--frames", required=True)
    st.add_argument("--out", required=True, help="Output MP4 path")
    st.add_argument("--fps", type=float, default=24.0)
    st.add_argument("--prefix", default="frame_")
    st.add_argument("--format", dest="file_format", default="PNG")
    st.add_argument("--pad", type=int, default=4)
    st.add_argument("--encoder", default="auto")
    st.add_argument("--linear-exr", action="store_true")
    st.set_defaults(func=_cmd_stitch)

    # notebook
    nb = sub.add_parser("notebook", help="Generate a 1-click Google Colab / Kaggle worker notebook")
    nb.add_argument("--out", default="notebooks/blendfleet_worker.ipynb")
    nb.add_argument("--coordinator-url", default="")
    nb.add_argument("--token", default="")
    nb.add_argument("--blender-version", default="5.2.3")
    nb.set_defaults(func=_cmd_notebook)

    # colab-launch
    cl = sub.add_parser("colab-launch", help="Open Colab across multiple Chrome profiles via bsk")
    cl.add_argument("--profiles", required=True, help="Comma-separated bsk browser labels")
    cl.add_argument("--notebook-url", default="https://colab.research.google.com/notebooks/empty.ipynb")
    cl.add_argument("--coordinator-url", default="")
    cl.add_argument("--token", default="")
    cl.set_defaults(func=_cmd_colab_launch)

    # account
    ac = sub.add_parser(
        "account",
        help="Manage login-once Google Colab accounts (isolated ADC credentials & sessions)",
    )
    ac_sub = ac.add_subparsers(dest="account_command", required=True)

    ac_add = ac_sub.add_parser("add", help="Authenticate and save a Google Colab account once")
    ac_add.add_argument("alias", help="Short local name for this account (e.g. 'studio1', 'triad')")
    ac_add.add_argument(
        "--chrome-profile",
        default=None,
        help="Optional Chrome profile directory (e.g. 'Profile 19') to open the OAuth login in",
    )
    ac_add.add_argument(
        "--code",
        default=None,
        help="Authorization code for non-interactive / agent-driven OAuth completion",
    )
    ac_add.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the OAuth URL without launching a browser window",
    )
    ac_add.set_defaults(func=_cmd_account_add)

    ac_list = ac_sub.add_parser("list", help="List saved Google Colab accounts")
    ac_list.add_argument("--json", action="store_true", help="Emit JSON output")
    ac_list.set_defaults(func=_cmd_account_list)

    ac_check = ac_sub.add_parser(
        "check",
        help="Check live Colab identity and compute-unit (CCU) balance across saved accounts",
    )
    ac_check.add_argument(
        "--accounts",
        default="all",
        help="Comma-separated account aliases or 'all' (default: all)",
    )
    ac_check.add_argument("--json", action="store_true", help="Emit JSON output")
    ac_check.set_defaults(func=_cmd_account_check)

    ac_clean = ac_sub.add_parser(
        "clean-sessions",
        help="Discover and terminate orphaned server-side Colab runtimes across saved accounts",
    )
    ac_clean.add_argument(
        "--accounts",
        default="all",
        help="Comma-separated account aliases or 'all' (default: all)",
    )
    ac_clean.add_argument(
        "--dry-run",
        action="store_true",
        help="List active server-side assignments without terminating them",
    )
    ac_clean.add_argument("--json", action="store_true", help="Emit JSON output")
    ac_clean.set_defaults(func=_cmd_account_clean_sessions)

    ac_rm = ac_sub.add_parser("remove", help="Delete saved credentials for an account alias")
    ac_rm.add_argument("alias", help="Account alias to remove")
    ac_rm.set_defaults(func=_cmd_account_remove)

    ac_cp = ac_sub.add_parser(
        "chrome-profiles",
        help="List local Google Chrome profiles and their signed-in emails",
    )
    ac_cp.add_argument("--json", action="store_true", help="Emit JSON output")
    ac_cp.set_defaults(func=_cmd_account_chrome_profiles)

    # fly
    fl = sub.add_parser(
        "fly",
        help="1-command headless multi-account Colab + local GPU render fleet launcher",
    )
    fl.add_argument("scene", help="Path to .blend or .zip project archive")
    fl.add_argument("--frames", required=True, help="Frame spec, e.g. '1-240' or '1-10, 20, 35-55'")
    fl.add_argument("--out", required=True, help="Directory to store verified rendered frames")
    fl.add_argument(
        "--accounts",
        default="all",
        help="Saved Colab accounts: 'all', 'none', 'a,b', or mixed tiers 'pro1:L4,free1:T4' (default: all)",
    )
    fl.add_argument(
        "--gpu",
        default="T4",
        choices=list(SUPPORTED_GPUS),
        help="Default Colab accelerator tier (default: T4)",
    )
    fl.add_argument(
        "--local-workers",
        type=int,
        default=0,
        help="Also spawn N local persistent Blender workers alongside remote Colab GPUs",
    )
    fl.add_argument("--local-device", default=None, help="Device override for local workers")
    fl.add_argument("--blender", default=None, help="Explicit path to local Blender binary")
    fl.add_argument("--public-url", default=None, help="Existing public URL (skips cloudflared Quick Tunnel)")
    fl.add_argument("--host", default="127.0.0.1")
    fl.add_argument("--port", type=int, default=0)
    fl.add_argument("--token", default=None, help="Shared bearer token (auto-generated if omitted)")
    fl.add_argument("--stitch-out", default=None, help="Optional MP4 path to stitch automatically when complete")
    fl.add_argument("--fps", type=float, default=24.0)
    fl.add_argument("--engine", default="CYCLES", choices=["CYCLES", "EEVEE"])
    fl.add_argument("--device", default="auto", choices=["auto", "optix", "cuda", "metal", "cpu"])
    fl.add_argument("--samples", type=int, default=None)
    fl.add_argument("--format", dest="file_format", default="PNG")
    fl.add_argument("--prefix", default="frame_")
    fl.add_argument("--pad", type=int, default=4)
    fl.add_argument("--lease-ttl", type=float, default=300.0)
    fl.add_argument("--max-retries", type=int, default=3)
    fl.add_argument("--poll-interval", type=float, default=2.0)
    fl.add_argument(
        "--stagger",
        type=float,
        default=1.5,
        help="Seconds between launching each Colab account to avoid burst throttling (default: 1.5)",
    )
    fl.add_argument(
        "--drive-cache",
        action="store_true",
        help="Use /content/drive/MyDrive/blendfleet_cache for Blender archive reuse on Colab",
    )
    fl.add_argument(
        "--no-persistent-data",
        action="store_true",
        help="Disable Cycles use_persistent_data for animated-topology or low-VRAM scenes",
    )
    fl.add_argument("--blender-version", default="5.2.3", help="Target Linux Blender version for Colab workers")
    fl.add_argument("--high-mem", action="store_true", help="Request high-RAM Colab VMs (Pro/Pro+)")
    fl.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Fallback Git URL if coordinator bundle is unavailable")
    fl.set_defaults(func=_cmd_fly)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except subprocess.CalledProcessError as exc:
        print(f"[blendfleet] Command failed (exit {exc.returncode})", file=sys.stderr)
        return exc.returncode or 1
    except (ValueError, FileNotFoundError, RuntimeError, TimeoutError) as exc:
        print(f"[blendfleet] Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
