"""Tests for HTTP work-stealing CoordinatorServer, lease reclamation, and colab_bridge."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from blendfleet.blender_worker import HTTPQueueClient, pick_cycles_backend
from blendfleet.colab_bridge import (
    allocate_dual_stack_port,
    build_colab_cell_snippet,
    generate_worker_notebook,
)
from blendfleet.coordinator import CoordinatorServer, JobState

VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_http_coordinator_work_stealing_and_reclaim(tmp_path: Path) -> None:
    scene = tmp_path / "demo.blend"
    scene.write_bytes(b"BLENDER-v502dummy_scene_bytes")
    out_dir = tmp_path / "frames"

    state = JobState(
        scene_path=scene,
        output_dir=out_dir,
        frames=[1, 2, 3],
        token="secret-tok",
        lease_ttl=0.25,
    )

    with CoordinatorServer(state, host="127.0.0.1", port=0) as srv:
        # Unauthorized request rejected
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{srv.url}/status")
        assert exc.value.code == 401

        w_fast = HTTPQueueClient(srv.url, "secret-tok", "m3-max", "Apple M3 Max (Metal)")
        w_colab = HTTPQueueClient(srv.url, "secret-tok", "colab-t4-1", "Tesla T4")

        # Worker 1 claims frame 1 and submits valid PNG
        c1 = w_fast.claim()
        assert c1["status"] == "claimed" and c1["frame"] == 1
        tmp_png = tmp_path / "f1.png"
        tmp_png.write_bytes(VALID_PNG)
        up1 = w_fast.upload_frame(1, c1["lease_id"], tmp_png, duration_sec=1.2)
        assert up1["ok"] is True

        # Colab worker claims frame 2 and then "dies" without uploading
        c2 = w_colab.claim()
        assert c2["status"] == "claimed" and c2["frame"] == 2

        # Fast worker claims frame 3 and submits it
        c3 = w_fast.claim()
        assert c3["status"] == "claimed" and c3["frame"] == 3
        up3 = w_fast.upload_frame(3, c3["lease_id"], tmp_png, duration_sec=1.1)
        assert up3["ok"] is True

        # Before lease expiry, fast worker is told to wait
        c_wait = w_fast.claim()
        assert c_wait["status"] == "wait"

        # Wait for Colab worker's short lease to expire -> fast worker reclaims frame 2!
        time.sleep(0.32)
        c2_reclaimed = w_fast.claim()
        assert c2_reclaimed["status"] == "claimed" and c2_reclaimed["frame"] == 2
        up2 = w_fast.upload_frame(2, c2_reclaimed["lease_id"], tmp_png, duration_sec=1.3)
        assert up2["ok"] is True

        # All done!
        assert w_fast.claim()["status"] == "done"
        st = state.status()
        assert st["done"] is True
        assert st["completed"] == 3
        assert st["reclaimed"] == 1
        assert (out_dir / "frame_0001.png").is_file()
        assert (out_dir / "frame_0002.png").is_file()
        assert (out_dir / "frame_0003.png").is_file()


def test_http_coordinator_rejects_corrupted_upload(tmp_path: Path) -> None:
    scene = tmp_path / "demo.blend"
    scene.write_bytes(b"BLENDER_DATA")
    state = JobState(scene, tmp_path / "out", [1], token="tok")

    with CoordinatorServer(state, host="127.0.0.1", port=0) as srv:
        w = HTTPQueueClient(srv.url, "tok", "w1", "T4")
        c = w.claim()
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"corrupted-not-a-png")
        with pytest.raises(urllib.error.HTTPError) as exc:
            w.upload_frame(1, c["lease_id"], bad, duration_sec=0.5)
        assert exc.value.code == 400
        assert not (tmp_path / "out/frame_0001.png").exists()


def test_gpu_backend_selection_rules() -> None:
    assert pick_cycles_backend("auto", "Tesla T4") == "OPTIX"
    assert pick_cycles_backend("auto", "NVIDIA L4") == "OPTIX"
    assert pick_cycles_backend("auto", "NVIDIA RTX PRO 6000 Blackwell") == "OPTIX"
    assert pick_cycles_backend("auto", "NVIDIA A100-SXM4-40GB") == "CUDA"
    assert pick_cycles_backend("auto", "NVIDIA H100 80GB HBM3") == "CUDA"
    assert pick_cycles_backend("cpu", "Tesla T4") == "CPU"


def test_colab_bridge_helpers() -> None:
    port = allocate_dual_stack_port()
    assert 1024 <= port <= 65535
    snippet = build_colab_cell_snippet("https://demo.trycloudflare.com", "abc123")
    assert "libtcmalloc-minimal4" in snippet
    assert "blendfleet worker --mode http" in snippet
    nb = generate_worker_notebook(coordinator_url="https://demo.trycloudflare.com", token="abc123")
    assert nb["nbformat"] == 4
    assert len(nb["cells"]) == 4
    assert "abc123" in json.dumps(nb)
