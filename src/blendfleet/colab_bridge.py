"""Multi-account Google Colab orchestrator, dual-stack MCP port allocator, and notebook builder.

Solves two real-world multi-account Colab pain points:
1. Pre-allocates a single free port for BOTH IPv4 (127.0.0.1) and IPv6 (::1) so Colab's
   ``ws://localhost:<port>`` MCP bridge never hits the macOS dual-stack port-mismatch bug.
2. Launches parallel Colab sessions across multiple Chrome profiles via ``bsk`` (BrowserSkill)
   or generates a standalone 1-click Colab/Kaggle worker notebook.
"""

from __future__ import annotations

import json
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

COLAB_SCRATCH_URL = "https://colab.research.google.com/notebooks/empty.ipynb"


def allocate_dual_stack_port() -> int:
    """Reserve an ephemeral TCP port usable across both 127.0.0.1 and ::1 on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build_colab_mcp_url(port: int | None = None, token: str | None = None) -> tuple[str, int, str]:
    """Build a Colab browser-bridge URL carrying a dual-stack port and access token."""
    chosen_port = port or allocate_dual_stack_port()
    chosen_token = token or secrets.token_urlsafe(16)
    url = f"{COLAB_SCRATCH_URL}#mcpProxyToken={chosen_token}&mcpProxyPort={chosen_port}"
    return url, chosen_port, chosen_token


def build_colab_cell_snippet(
    coordinator_url: str,
    token: str,
    *,
    repo_url: str = "https://github.com/brixtonpham/blendfleet.git",
    blender_version: str = "5.2.3",
    cache_dir: str = "/content/drive/MyDrive/blendfleet_cache",
    mount_drive: bool = False,
    device: str = "auto",
) -> str:
    """Generate a single copy-pasteable Colab/Kaggle cell that joins the live render pool."""
    lines = [
        "# blendfleet Colab / Kaggle GPU worker (persistent bpy work-stealing loop)",
        "!nvidia-smi --query-gpu=name,memory.total --format=csv,noheader",
        "!apt-get -qq update && apt-get -qq install -y libtcmalloc-minimal4 >/dev/null 2>&1 || true",
        f"!pip -q install git+{repo_url}",
    ]
    if mount_drive:
        lines.extend(
            [
                "from google.colab import drive",
                "drive.mount('/content/drive')",
            ]
        )
        eff_cache = cache_dir
    else:
        eff_cache = "/content/blendfleet_cache"

    lines.append(
        f"!blendfleet worker --mode http "
        f"--coordinator-url {coordinator_url!r} "
        f"--token {token!r} "
        f"--blender-version {blender_version!r} "
        f"--cache-dir {eff_cache!r} "
        f"--device {device!r}"
    )
    return "\n".join(lines) + "\n"


def generate_worker_notebook(
    *,
    coordinator_url: str = "",
    token: str = "",
    repo_url: str = "https://github.com/brixtonpham/blendfleet.git",
    blender_version: str = "5.2.3",
) -> dict[str, Any]:
    """Return a clean Jupyter `.ipynb` dict runnable on both Google Colab and Kaggle."""
    setup_cell = "\n".join(
        [
            "#@title 1. Configure blendfleet Worker { display-mode: 'form' }",
            'MODE = "http" #@param ["http", "fs"]',
            f'COORDINATOR_URL = "{coordinator_url}" #@param {{type:"string"}}',
            f'TOKEN = "{token}" #@param {{type:"string"}}',
            f'BLENDER_VERSION = "{blender_version}" #@param ["4.2.23", "4.5.14", "5.2.2", "5.2.3"] {{allow-input: true}}',
            'DEVICE = "auto" #@param ["auto", "optix", "cuda", "cpu"]',
            'USE_DRIVE_CACHE = False #@param {type:"boolean"}',
            'DRIVE_CACHE_DIR = "/content/drive/MyDrive/blendfleet_cache" #@param {type:"string"}',
            "#@markdown ---",
            "#@markdown ### Shared Google Drive Mode Only (`MODE = 'fs'`)",
            'SHARED_SCENE_PATH = "/content/drive/MyDrive/blendfleet_job/scene.blend" #@param {type:"string"}',
            'SHARED_OUTPUT_DIR = "/content/drive/MyDrive/blendfleet_job/frames" #@param {type:"string"}',
            'FRAMES = "1-240" #@param {type:"string"}',
            'ENGINE = "CYCLES" #@param ["CYCLES", "EEVEE"]',
            'SAMPLES = 0 #@param {type:"integer"}',
            'WORKER_INDEX = 0 #@param {type:"integer"}',
            'TOTAL_WORKERS = 1 #@param {type:"integer"}',
        ]
    )

    install_cell = "\n".join(
        [
            "#@title 2. Check GPU, Install tcmalloc & blendfleet",
            "import os, subprocess",
            "!nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || echo 'CPU runtime'",
            "!apt-get -qq update && apt-get -qq install -y libtcmalloc-minimal4 >/dev/null 2>&1 || true",
            f"!pip -q install --upgrade git+{repo_url}",
            "if USE_DRIVE_CACHE or MODE == 'fs':",
            "    from google.colab import drive",
            "    drive.mount('/content/drive')",
        ]
    )

    run_cell = "\n".join(
        [
            "#@title 3. Launch Persistent Blender Work-Stealing Loop",
            "import shlex, subprocess",
            "cache_dir = DRIVE_CACHE_DIR if (USE_DRIVE_CACHE or MODE == 'fs') else '/content/blendfleet_cache'",
            "if MODE == 'http':",
            "    assert COORDINATOR_URL and TOKEN, 'Set COORDINATOR_URL and TOKEN in Cell 1.'",
            "    cmd = [",
            "        'blendfleet', 'worker', '--mode', 'http',",
            "        '--coordinator-url', COORDINATOR_URL,",
            "        '--token', TOKEN,",
            "        '--blender-version', BLENDER_VERSION,",
            "        '--cache-dir', cache_dir,",
            "        '--device', DEVICE,",
            "    ]",
            "else:",
            "    cmd = [",
            "        'blendfleet', 'worker', '--mode', 'fs',",
            "        '--scene', SHARED_SCENE_PATH,",
            "        '--out', SHARED_OUTPUT_DIR,",
            "        '--frames', FRAMES,",
            "        '--engine', ENGINE,",
            "        '--device', DEVICE,",
            "        '--blender-version', BLENDER_VERSION,",
            "        '--cache-dir', cache_dir,",
            "        '--worker-index', str(WORKER_INDEX),",
            "        '--total-workers', str(TOTAL_WORKERS),",
            "    ]",
            "    if SAMPLES > 0:",
            "        cmd.extend(['--samples', str(SAMPLES)])",
            "print('Running:', shlex.join(cmd))",
            "subprocess.run(cmd, check=True)",
        ]
    )

    def _code_cell(src: str) -> dict[str, Any]:
        return {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [line + "\n" for line in src.splitlines()],
        }

    md_intro = {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "# blendfleet — Persistent Multi-GPU Blender Worker\n",
            "\n",
            "Connects this Google Colab / Kaggle GPU (`T4`, `L4`, `G4`) to a `blendfleet` render job.\n",
            "- Loads `.blend` **once** per worker (zero per-frame restart overhead).\n",
            "- Auto-selects **OptiX** on `T4` / `L4` / `G4` (hardware RT cores) and **CUDA** on `A100` / `H100`.\n",
            "- Preloads `libtcmalloc_minimal` to prevent Colab glibc OOM crashes.\n",
            "- Automatically recovers frames if another worker tab disconnects.\n",
        ],
    }

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "colab": {"name": "blendfleet_worker.ipynb", "provenance": []},
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "accelerator": "GPU",
        },
        "cells": [md_intro, _code_cell(setup_cell), _code_cell(install_cell), _code_cell(run_cell)],
    }


def write_worker_notebook(out_path: Path, **kwargs: Any) -> Path:
    """Serialize the Colab/Kaggle worker notebook to disk."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    nb = generate_worker_notebook(**kwargs)
    out.write_text(json.dumps(nb, indent=2) + "\n", encoding="utf-8")
    return out


def launch_colab_profiles(
    profiles: list[str],
    notebook_url: str = COLAB_SCRATCH_URL,
) -> list[dict[str, str]]:
    """Open Colab tabs across multiple Chrome profiles via BrowserSkill (`bsk`)."""
    bsk = shutil.which("bsk")
    if not bsk:
        raise RuntimeError(
            "bsk CLI not found on PATH. Install BrowserSkill (`mise use -g github:Tencent/BrowserSkill`) "
            "or open notebooks/blendfleet_worker.ipynb manually in each Chrome profile."
        )

    launched: list[dict[str, str]] = []
    for prof in profiles:
        start = subprocess.run(
            [bsk, "session", "start", "--browser", prof, "--no-focus"],
            capture_output=True,
            text=True,
            check=False,
        )
        if start.returncode != 0:
            launched.append(
                {
                    "profile": prof,
                    "status": "error",
                    "detail": (start.stderr or start.stdout).strip(),
                }
            )
            continue
        session_id = start.stdout.strip().split()[0]
        nav = subprocess.run(
            [bsk, "navigate", notebook_url, "--session", session_id],
            capture_output=True,
            text=True,
            check=False,
        )
        launched.append(
            {
                "profile": prof,
                "session_id": session_id,
                "status": "open" if nav.returncode == 0 else "nav_error",
                "url": notebook_url,
            }
        )
    return launched
