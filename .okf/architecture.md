---
type: Architecture
title: blendfleet System Architecture
description: Component map and execution flow for persistent multi-GPU Blender rendering across local and ephemeral Colab/Kaggle workers.
tags:
  - architecture
  - blender
  - colab
  - distributed
generated:
  by: agent/antigravity
  at: "2026-11-26T18:05:00+07:00"
sources:
  - id: src-coordinator
    resource: /src/blendfleet/coordinator.py
    title: HTTP Work-Stealing Coordinator
  - id: src-fs-queue
    resource: /src/blendfleet/fs_queue.py
    title: Zero-Server Shared-FS Frame Queue
  - id: src-blender-worker
    resource: /src/blendfleet/blender_worker.py
    title: Persistent bpy Worker Loop
  - id: src-colab-bridge
    resource: /src/blendfleet/colab_bridge.py
    title: Multi-Account Colab Bridge
---

# Overview

`blendfleet` coordinates heterogeneous Blender workers (local Apple Silicon / NVIDIA GPUs + ephemeral Google Colab and Kaggle GPUs) without restarting Blender per frame or requiring external server infrastructure.[^src-coordinator]

## Components

| Module | Responsibility |
|---|---|
| `frames.py` | Parses Blender frame ranges (`1-10, 20, 35-55:5`), computes 2-phase modulo worker ordering, and validates PNG/EXR/JPEG magic bytes. |
| `coordinator.py` | Stdlib `ThreadingHTTPServer` serving `.blend` assets once, managing frame leases with TTL/heartbeats, reclaiming frames from dead Colab tabs, and exposing via optional `cloudflared` Quick Tunnel.[^src-coordinator] |
| `fs_queue.py` | Zero-server shared-storage queue using interleaved modulo priority + atomic `O_CREAT \| O_EXCL` lease files and stale-lease reclamation.[^src-fs-queue] |
| `blender_worker.py` | Runs inside Blender's embedded Python (`blender -b scene.blend -P blender_worker.py`), loads `.blend` once, selects `OPTIX` / `CUDA` / `METAL` / `EEVEE` / `CPU`, and loops over frames.[^src-blender-worker] |
| `worker.py` | Outer launcher that caches Linux Blender binaries (`download.blender.org` + `ftp.nluug.nl`), preloads `libtcmalloc_minimal`, and stages `.blend`/`.zip` onto local NVMe. |
| `colab_bridge.py` | Dual-stack (`127.0.0.1` + `::1`) port allocator for Colab MCP bridges, `bsk` multi-profile launcher, and Colab/Kaggle `.ipynb` generator.[^src-colab-bridge] |
| `stitch.py` | Verifies frame completeness and stitches MP4s with `h264_videotoolbox`, `h264_nvenc`, or `libx264` (plus Linear EXR gamma). |

## Related Decisions

- [Persistent bpy Work-Stealing Loop](/decisions/001-persistent-bpy-work-stealing.md)
- [Zero-Dependency Stdlib Core & Dual-Stack Colab Bridge](/decisions/002-dual-stack-colab-mcp-and-zero-deps.md)

[^src-coordinator]: Implementation in `src/blendfleet/coordinator.py`.
[^src-fs-queue]: Implementation in `src/blendfleet/fs_queue.py`.
[^src-blender-worker]: Implementation in `src/blendfleet/blender_worker.py`.
[^src-colab-bridge]: Implementation in `src/blendfleet/colab_bridge.py`.
