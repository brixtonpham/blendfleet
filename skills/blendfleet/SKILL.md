---
name: blendfleet
description: Render Blender (.blend) animations across multiple free or paid Google Colab accounts and local Apple Silicon / NVIDIA GPUs using blendfleet. Use when rendering .blend frame sequences, onboarding or checking Google Colab accounts and CCU balances, routing jobs across local and cloud GPUs, verifying frame integrity, or stitching frame sequences into MP4 video.
---

# blendfleet — Multi-Account Colab + Local Blender GPU Fleet

Use `blendfleet` to turn multiple Google Colab accounts (`T4`, `L4`, `G4`) plus the user's local Apple Silicon (`Metal`) or NVIDIA (`OptiX`/`CUDA`) machine into one persistent `bpy` work-stealing render pool — with zero per-frame Blender restarts.

## When to Activate

- Rendering a `.blend` (or `.zip` Blender project) animation sequence across local and/or Google Colab GPUs.
- Onboarding Google accounts into `blendfleet` or checking Colab Compute Unit (CCU) balances across accounts.
- Verifying rendered frame sequences (`PNG`, `EXR`, `JPEG`) for missing or corrupted frames and re-rendering gaps.
- Stitching rendered frames into hardware-accelerated MP4 video.

---

## Step 1 — Pre-Flight Check

Run a single shell check before starting any onboarding or render:

```bash
blendfleet --version
command -v colab && colab --help | head -n 5 || echo "MISSING: colab (google-colab-cli)"
cloudflared --version || echo "MISSING: cloudflared"
blendfleet account list || true
```

If tools are missing, install them before proceeding:

```bash
# Core CLI
uv tool install git+https://github.com/brixtonpham/blendfleet.git

# Required for headless multi-account Colab fleet (blendfleet fly)
uv tool install google-colab-cli
brew install cloudflared
```

---

## Step 2 — One-Time Account Onboarding

Each Google account only needs to authenticate **once**. Credentials are saved locally under `~/.config/blendfleet/accounts/<alias>/` with `0600` file permissions. Never print, copy, or commit `credentials.json` or OAuth tokens.

1. List local Chrome profiles so the user can match each Google account to its Chrome profile:
   ```bash
   blendfleet account chrome-profiles
   ```
2. Add each Google account with a short alias (`acc1`, `acc2`, `work`, `personal`):
   ```bash
   blendfleet account add acc1 --chrome-profile "Profile 1"
   blendfleet account add acc2 --chrome-profile "Profile 2"
   ```
   - For non-interactive agent onboarding (or when using `bsk` to open the OAuth URL in the matching Chrome profile), use the 2-step flow:
     ```bash
     blendfleet account add acc1 --no-browser   # prints the OAuth URL
     blendfleet account add acc1 --code "<auth-code>"
     ```
     Open the printed URL in the right Chrome profile and let the user approve Google consent (never attempt to bypass Google login/CAPTCHA gates).
3. Verify all configured accounts and check CCU balances in parallel:
   ```bash
   blendfleet account check
   ```

---

## Step 3 — Smart Hardware & GPU Routing

Blender Cycles renders on **hardware Ray Tracing cores** (`OPTIX` on NVIDIA Turing/Ada/Blackwell, `METAL` on Apple Silicon) — not Tensor cores. Pick the right fleet shape before running:

| Scenario | Recommended Strategy | Command Flags |
|---|---|---|
| Local Apple Silicon (M1–M4) or RTX GPU present **and** estimated total render `< 10 min` | Run locally (no tunnel/VM startup overhead) or hybrid | `blendfleet serve scene.blend --frames 1-120 --out ./out --local-workers 1 --stitch-out final.mp4` |
| Heavy Cycles animation `> 20 min`, or user wants local RAM/GPU free | Pool all free Colab `T4` accounts (0 CCU) | `blendfleet fly scene.blend --frames 1-240 --out ./out --accounts all --gpu T4` |
| Fastest wall-clock speed on free tier | Hybrid local + all free Colab `T4` accounts | `blendfleet fly scene.blend --frames 1-240 --out ./out --accounts all --gpu T4 --local-workers 1` |
| Accounts have CCU balance + tight deadline | Colab `L4` (24 GB, ~1.71 CCU/hr — best CCU efficiency) | `--gpu L4` |
| Massive 4K / high-VRAM scene (> 16–24 GB) + CCU available | Colab `G4` (96 GB Blackwell RT, ~8.8 CCU/hr) | `--gpu G4` |
| `A100` / `H100` | **Never pick for Cycles** — compute-only (`CUDA`, no RT cores), poor speed per CCU | Avoid `--gpu A100` / `--gpu H100` |

Rule of thumb:
- Check `blendfleet account check` first to confirm which accounts have CCU before requesting `L4` or `G4`.
- On macOS, check local memory headroom before adding `--local-workers 1` on heavy scenes.

---

## Step 4 — Run & Monitor the Render

### Standard Headless Multi-Account Fleet (`blendfleet fly`)

```bash
blendfleet fly scene.blend \
  --frames 1-240 \
  --out ./out \
  --accounts all \
  --gpu T4 \
  --local-workers 1 \
  --stitch-out final.mp4 \
  --fps 24
```

Key flags:
- `--frames`: supports ranges, lists, and steps (e.g. `1-240`, `1-10, 20, 35-55`, `1-240:2`).
- `--accounts`: `all`, comma-separated aliases (`acc1,acc2`), or mixed per-account GPU tiers (`pro1:L4,free1:T4,free2:T4`).
- `--samples`, `--engine CYCLES|EEVEE`, `--format PNG|OPEN_EXR`, `--blender-version 5.2.3`, `--stagger 1.5`, `--drive-cache`, `--no-persistent-data`.
- Re-running against the same `--out` directory automatically skips already-verified frames and renders only missing or corrupted ones.
- If a prior run was hard-killed (`SIGKILL`) mid-allocation and Google returns `Allocation refused`, run `blendfleet account clean-sessions` to inspect/release orphaned server-side Colab runtimes.

### Fallback Modes

- **Manual Colab / Kaggle tabs (`blendfleet serve --tunnel`):** Use if `google-colab-cli` is unavailable or when attaching Kaggle GPUs. Prints a 1-cell snippet to paste into any Colab/Kaggle notebook (or open `notebooks/blendfleet_worker.ipynb`).
- **Zero-server Shared Google Drive (`--mode fs`):** Use when no HTTP tunnel is allowed. Assign `--worker-index 0 .. N-1` with `--total-workers N` on a shared Drive folder.

---

## Step 5 — Verification & Cleanup Checklist

Never declare a render complete until every item below passes:

1. **Frame integrity check:**
   ```bash
   blendfleet verify ./out --frames 1-240
   ```
   Confirm exit code `0`, `missing: []`, and `corrupted: []`. If any frames failed, re-run `blendfleet fly` (or `blendfleet serve`) pointing at the same `--out` directory — completed frames are skipped automatically.
2. **MP4 output verification (if stitching):**
   - If not stitched during `fly`/`serve`, run:
     ```bash
     blendfleet stitch ./out --frames 1-240 --fps 24 --out final.mp4
     ```
   - Verify the output file exists, is non-empty, and reports the expected stream/duration via `ffprobe -v error -show_entries format=duration -of default=nw=1 final.mp4`.
3. **Runtime & tunnel cleanup:**
   - Confirm `blendfleet fly` exited cleanly and terminated all spawned Colab runtimes (especially on paid `L4`/`G4` GPUs so no idle CCU burn continues).
   - Confirm no stray local `cloudflared` or `blender` worker processes from this job remain running.
4. **Credential hygiene:**
   - Confirm `~/.config/blendfleet/accounts/` permissions remain restricted (`0600`) and no credentials or tokens were written inside the project repository.
