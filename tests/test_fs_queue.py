"""Tests for SharedFSQueue (2-phase modulo sharding + stale-lease reclamation)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from blendfleet.fs_queue import SharedFSQueue

VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 48


def test_shared_fs_two_workers_disjoint_and_complete(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    frames = [1, 2, 3, 4, 5, 6]

    q0 = SharedFSQueue(out_dir, frames, worker_id="w0", worker_index=0, total_workers=2)
    q1 = SharedFSQueue(out_dir, frames, worker_id="w1", worker_index=1, total_workers=2)

    f0, done0 = q0.claim_next()
    f1, done1 = q1.claim_next()
    assert not done0 and not done1
    assert f0 == 1
    assert f1 == 2

    # Complete all frames alternating workers
    for q, frame in ((q0, f0), (q1, f1)):
        local = scratch / f"local_{frame}.png"
        local.write_bytes(VALID_PNG)
        q.complete_frame(frame, local)

    while True:
        f, done = q0.claim_next()
        if done:
            break
        assert f is not None
        local = scratch / f"local_{f}.png"
        local.write_bytes(VALID_PNG)
        q0.complete_frame(f, local)

    f_end, all_done = q1.claim_next()
    assert f_end is None
    assert all_done is True


def test_shared_fs_reclaims_stale_lease_from_dead_worker(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    dead = SharedFSQueue(
        out_dir, [1, 2], worker_id="dead-t4", worker_index=0, total_workers=2, lease_ttl=0.2
    )
    survivor = SharedFSQueue(
        out_dir, [1, 2], worker_id="m3-max", worker_index=1, total_workers=2, lease_ttl=0.2
    )

    f_dead, _ = dead.claim_next()
    assert f_dead == 1

    # Survivor takes frame 2 and completes it.
    f_surv, _ = survivor.claim_next()
    assert f_surv == 2
    p2 = scratch / "f2.png"
    p2.write_bytes(VALID_PNG)
    survivor.complete_frame(2, p2)

    # Frame 1 is still actively leased by dead-t4 -> survivor gets (None, False) to wait.
    f_wait, done_wait = survivor.claim_next()
    assert f_wait is None
    assert done_wait is False

    # Backdate lease file to simulate dead Colab tab exceeding lease_ttl.
    lpath = dead.lease_path(1)
    lease_data = json.loads(lpath.read_text(encoding="utf-8"))
    lease_data["heartbeat_at"] = time.time() - 10.0
    lpath.write_text(json.dumps(lease_data), encoding="utf-8")

    # Survivor immediately reclaims frame 1 and finishes the job.
    f_reclaimed, done_reclaimed = survivor.claim_next()
    assert f_reclaimed == 1
    assert done_reclaimed is False
    p1 = scratch / "f1.png"
    p1.write_bytes(VALID_PNG)
    survivor.complete_frame(1, p1)

    assert survivor.claim_next() == (None, True)
