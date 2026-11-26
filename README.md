# blendfleet

**Heterogeneous distributed Blender render farm for ephemeral Google Colab, Kaggle, and local GPUs.**

`blendfleet` pools free or paid Google Colab GPUs (`T4`, `L4`, `G4`), Kaggle GPUs, and your local Apple Silicon (`Metal`) or NVIDIA (`OptiX`/`CUDA`) machine into one live work-stealing render pool — with **zero external Python dependencies** in its core runtime.

---

## Why `blendfleet`?

Existing multi-account Blender Colab notebooks break in three predictable ways:

1. **Per-frame Blender restarts** — spawning a fresh `blender -b` subprocess per frame wastes 4–10 seconds reloading the `.blend` file and recompiling OptiX shaders on *every single frame*.
2. **Drive FUSE lock races & OAuth popups** — mounting Google Drive across 4 accounts requires 4 interactive OAuth consent clicks per session, and Drive's 2–10s cross-account sync lag causes `.tmp` lock files to race.
3. **Idle fast workers** — static range splits (`1-60`, `61-120`) leave a fast local Apple Silicon Mac or Colab `G4` sitting idle while a slow `T4` grinds through its slice or disconnects at minute 89.

`blendfleet` fixes all three:

- **Persistent `bpy` work-stealing loop:** Launches Blender **once** per worker (`blender -b scene.blend -P blender_worker.py`). The `.blend` file loads once and OptiX/Metal shaders compile once; frames stream through the live `bpy` process back-to-back.
- **Zero-dependency HTTP work-stealing coordinator (`blendfleet serve`):** Serves the `.blend` scene once over `localhost`, Tailscale, or `cloudflared --tunnel`, hands out frames with TTL leases and heartbeats, reclaims frames automatically when a Colab tab disconnects, and validates PNG/EXR magic bytes atomically before committing each frame.
- **Zero-server Shared Drive fallback (`--mode fs`):** Uses 2-phase interleaved modulo sharding (`frame % N == worker_index`) to eliminate Drive FUSE lock contention, followed by atomic `O_CREAT | O_EXCL` stale-lease tail-sweeping if a worker dies.
- **GPU-aware backend auto-selection:**
  - `T4` / `L4` / `G4 (RTX PRO 6000 Blackwell)` / `RTX` → **`OPTIX`** (hardware RT cores)
  - `A100` / `H100` / `V100` / `P100` → **`CUDA`** (no RT cores on compute-only datacenter cards)
  - Apple Silicon (`M1`–`M4`) → **`METAL`** (hardware Metal Ray Tracing)
  - Linux Colab VMs → automatic **`libtcmalloc_minimal`** preload to prevent glibc OOM crashes
- **Hardware-accelerated FFmpeg stitcher:** Verifies every frame's presence and header bytes before encoding with `h264_videotoolbox` (macOS), `h264_nvenc` (NVIDIA), or `libx264` (including Linear EXR → sRGB gamma).

---

## Architecture

```text
                    ┌──────────────────────────────────────────┐
                    │   blendfleet serve scene.blend --tunnel    │
                    │  • Streams .blend once (SHA-256 cached)  │
                    │  • Dynamic frame leases + heartbeats     │
                    │  • Auto-reclaims frames from dead tabs   │
                    │  • Atomic PNG/EXR validation -> ./out    │
                    └──────┬─────────────┬─────────────┬───────┘
                           │             │             │
              localhost    │   HTTPS     │   HTTPS     │   HTTPS
                           ▼             ▼             ▼
                 ┌──────────────┐ ┌────────────┐ ┌────────────┐
                 │ Local Worker │ │ Colab #1   │ │ Colab #2..N│
                 │ Apple M3 Max │ │ Free T4    │ │ T4 / L4/G4 │
                 │ Metal RT     │ │ OptiX      │ │ OptiX      │
                 │ 1x bpy proc  │ │ 1x bpy proc│ │ 1x bpy proc│
                 └──────────────┘ └────────────┘ └────────────┘
```

---

## Quick Start

### Install

```bash
pip install git+https://github.com/brixtonpham/blendfleet.git
# or with uv
uv tool install git+https://github.com/brixtonpham/blendfleet.git
```

### 1. Start the Coordinator (Hybrid Local + Remote Colab Pool)

Start a coordinator on your machine, attach 1 local GPU worker, expose it to Colab via a Cloudflare Quick Tunnel (`cloudflared`), and stitch the final MP4 automatically when all frames finish:

```bash
blendfleet serve scene.blend \
  --frames 1-240 \
  --out ./renders/shot_01 \
  --local-workers 1 \
  --tunnel \
  --stitch-out ./renders/shot_01.mp4 \
  --fps 24
```

`blendfleet serve` prints a ready-to-paste 1-cell Colab/Kaggle snippet containing the tunnel URL and bearer token:

```python
!nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
!apt-get -qq update && apt-get -qq install -y libtcmalloc-minimal4 >/dev/null 2>&1 || true
!pip -q install git+https://github.com/brixtonpham/blendfleet.git
!blendfleet worker --mode http \
  --coordinator-url 'https://<random>.trycloudflare.com' \
  --token '<token>' \
  --blender-version '5.2.3'
```

Paste that cell into as many Google Colab or Kaggle accounts as you want — or open [`notebooks/blendfleet_worker.ipynb`](notebooks/blendfleet_worker.ipynb). Workers join dynamically mid-job and drop out cleanly if disconnected.

### 2. Zero-Server Shared Google Drive Mode (`--mode fs`)

Prefer no HTTP tunnel? Share one Google Drive folder across $N$ Colab accounts and pass `--worker-index` (`0 .. N-1`) with `--total-workers N`:

```bash
# Account 0
blendfleet worker --mode fs \
  --scene /content/drive/MyDrive/blendfleet_job/scene.blend \
  --out /content/drive/MyDrive/blendfleet_job/frames \
  --frames 1-240 --worker-index 0 --total-workers 4

# Account 1..3 use --worker-index 1, 2, 3
```

Each worker stages `.blend` onto local `/content/` NVMe, renders its disjoint modulo shard first (`1, 5, 9…` vs `2, 6, 10…`), and then automatically steals any leftover or stale-leased frames from workers that disconnected early.

### 3. Verify & Stitch Frames

```bash
blendfleet verify ./renders/shot_01 --frames 1-240
blendfleet stitch ./renders/shot_01 --frames 1-240 --fps 24 --out ./renders/shot_01.mp4
```

### 4. Multi-Profile Chrome Launch (`bsk`)

If you use [BrowserSkill (`bsk`)](https://github.com/Tencent/BrowserSkill) with multiple logged-in Chrome profiles, open Colab across all of them in one command:

```bash
blendfleet colab-launch \
  --profiles profile-a,profile-b,profile-c \
  --coordinator-url https://<random>.trycloudflare.com \
  --token <token>
```

---

## Colab GPU Guide for Blender Cycles

Blender Cycles uses **OptiX hardware Ray Tracing cores**, not Tensor cores:

| Runtime | Typical Cost | Hardware RT Cores | Recommendation |
|---|---|---|---|
| **Apple M3 Max (Local)** | Free | Metal RT | ~2.8× faster than a single Colab `T4` — run `--local-workers 1` alongside Colab workers. |
| **Colab `T4` (16 GB)** | Free (0 CCU) | Turing RT (`OPTIX`) | Great when sharded across 3–5 accounts or to keep local RAM/GPU free. |
| **Colab `L4` (24 GB)** | ~1.71 CCU/hr | Ada RT (`OPTIX`) | Best CCU efficiency (~580 hrs per 1,000 CCU). |
| **Colab `G4` (96 GB)** | ~8.8 CCU/hr | Blackwell RT (`OPTIX`) | Best for massive 4K/heavy scenes (~3.5× faster than M3 Max). |
| **Colab `A100` / `H100`** | 5.4–18.0 CCU/hr | **None** (`CUDA` only) | Avoid for Blender Cycles; poor ray-tracing performance per CCU. |

---

## Development

```bash
mise run setup      # install dev & test dependencies with uv
mise run test       # run unit & multi-worker integration tests
mise run e2e-local  # run real 2-worker headless Blender + FFmpeg smoke test
mise run check      # ruff lint + pytest + OKF validation
```

---

## Inspirations & Prior Art

`blendfleet` builds on lessons learned from the open-source Blender-on-Colab ecosystem:

- [`ynshung/blender-colab`](https://github.com/ynshung/blender-colab) — `libtcmalloc` preload and Blender release mirror fallback (`ftp.nluug.nl`).
- [`cgmaniac/blender-colab-render-suite`](https://github.com/cgmaniac/blender-colab-render-suite) — local NVMe staging, frame-range expressions, and Linear EXR gamma stitching.
- [`sufianahamd/Blender-Multi-Account-Rendering`](https://github.com/sufianahamd/Blender-Multi-Account-Rendering) — multi-account shared-folder rendering pattern (upgraded here to persistent `bpy` processes + atomic leases).
- [`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) — browser WebSocket bridge (upgraded with dual-stack IPv4+IPv6 port allocation).
- [`MUKILAN0608/ColabCluster`](https://github.com/MUKILAN0608/ColabCluster) — heartbeat + lease reconciliation model for ephemeral Colab runtimes.

## License

[MIT](LICENSE)
