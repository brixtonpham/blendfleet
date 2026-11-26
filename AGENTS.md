# blendfleet

Heterogeneous distributed Blender render farm that shunts frames across ephemeral Google Colab GPUs (`T4`, `L4`, `G4`), Kaggle GPUs, and local Apple Silicon / NVIDIA machines in one persistent `bpy` work-stealing loop.

Project knowledge lives in `.okf/`. Update it in the same change as the code it describes, and run the validator.

## Layout

- `src/blendfleet/frames.py` — frame range parsing (`1-10, 20, 35-55`), interleaved modulo sharding, output format/magic-byte verification.
- `src/blendfleet/coordinator.py` — stdlib HTTP work-stealing coordinator (`ThreadingHTTPServer`) with lease TTL, heartbeat, automatic stale-lease reclamation, `.blend` asset serving, atomic frame upload, and optional `cloudflared` tunnel wrapper.
- `src/blendfleet/fs_queue.py` — zero-server shared-storage frame queue (Google Drive / `rclone` / NFS / local FS) using 2-phase modulo stride + atomic `O_CREAT|O_EXCL` stale-lease tail-sweep.
- `src/blendfleet/blender_worker.py` — persistent script executed *inside* Blender (`blender -b scene.blend -P blender_worker.py`) that loads the scene once, auto-configures `OPTIX` / `CUDA` / `METAL` / `EEVEE` / `CPU`, and loops over frames from either HTTP or FS queues.
- `src/blendfleet/worker.py` — outer worker launcher handling Blender Linux binary auto-download/cache (`download.blender.org` + `ftp.nluug.nl`), `libtcmalloc` preload on Colab, local NVMe staging, and launching `blender_worker.py`.
- `src/blendfleet/colab_bridge.py` — multi-account Google Colab orchestrator combining a dual-stack IPv4+IPv6 (`127.0.0.1` + `[::1]`) WebSocket MCP proxy with `bsk` multi-Chrome-profile launch and one-liner bootstrap generation.
- `src/blendfleet/stitch.py` — frame completeness verifier and hardware-accelerated FFmpeg video stitcher (`h264_videotoolbox`, `h264_nvenc`, `libx264`, Linear EXR gamma).
- `src/blendfleet/cli.py` — `blendfleet` CLI (`serve`, `worker`, `render`, `colab-launch`, `verify`, `stitch`, `notebook`).
- `notebooks/blendfleet_worker.ipynb` — standalone 1-click Google Colab / Kaggle worker notebook.
- `tests/` — unit tests, HTTP/FS multi-worker simulation tests, and real headless Blender E2E smoke test.

## Commands

- `mise run setup` — install dependencies with `uv`.
- `mise run lint` — run `ruff check`.
- `mise run test` — run `pytest`.
- `mise run e2e-local` — run real multi-worker headless Blender smoke test (`mise x blender@5.2.3`).
- `mise run check` — lint + tests + strict OKF validation.

## Rules

- Keep core `src/blendfleet/` runtime 100% Python stdlib so ephemeral Colab/Kaggle VMs bootstrap in <1s without dependency conflicts.
- Never restart the `blender` binary per frame; keep scene loading and OptiX/Metal shader compilation inside one persistent `bpy` session.
- Never write rendered frames directly to final target paths; write to `.part`, validate non-zero size & header bytes, then `os.replace`.
