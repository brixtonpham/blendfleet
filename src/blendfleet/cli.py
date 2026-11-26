"""CLI entrypoint for blendfleet."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from blendfleet import __version__
from blendfleet.colab_bridge import (
    build_colab_cell_snippet,
    launch_colab_profiles,
    write_worker_notebook,
)
from blendfleet.coordinator import CoordinatorServer, JobState, QuickTunnel
from blendfleet.frames import BLENDER_FORMAT_EXT, parse_frame_spec, verify_frame_sequence
from blendfleet.stitch import stitch_frames
from blendfleet.worker import run_worker


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
    local_threads: list[threading.Thread] = []

    with CoordinatorServer(state, host=args.host, port=args.port) as server:
        connect_url = server.url
        if args.tunnel:
            tunnel = QuickTunnel(server.url)
            connect_url = tunnel.start()

        try:
            print(f"blendfleet v{__version__} coordinator live", flush=True)
            print(f"  Scene       : {state.scene_path.name} ({state.scene_size:,} bytes)", flush=True)
            print(f"  Frames      : {len(frames)} ({frames[0]}..{frames[-1]}), already done={len(state.completed)}", flush=True)
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
                t = threading.Thread(
                    target=run_worker,
                    kwargs={
                        "mode": "http",
                        "worker_id": f"local-{i}",
                        "blender_bin": args.blender,
                        "coordinator_url": server.url,
                        "token": state.token,
                        "device": args.local_device or args.device,
                    },
                    daemon=True,
                )
                t.start()
                local_threads.append(t)

            last_line = ""
            while True:
                st = state.status()
                workers_summary = ", ".join(
                    f"{w['worker_id']}({w['gpu']}:{w['frames_completed']})"
                    for w in st["workers"]
                )
                eta = f"ETA {st['eta_sec']}s" if st["eta_sec"] is not None else "ETA --"
                line = (
                    f"[progress] {st['completed']}/{st['total']} done | "
                    f"active={st['in_progress']} pending={st['pending']} "
                    f"reclaimed={st['reclaimed']} failed={st['failed']} | {eta}"
                    + (f" | {workers_summary}" if workers_summary else "")
                )
                if line != last_line:
                    print(line, flush=True)
                    last_line = line
                if st["done"]:
                    break
                time.sleep(args.poll_interval)

            for t in local_threads:
                t.join(timeout=10.0)

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
            print("\n[blendfleet] Interrupted; shutting down coordinator.", flush=True)
            return 130
        finally:
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except subprocess.CalledProcessError as exc:
        print(f"[blendfleet] Command failed (exit {exc.returncode})", file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
