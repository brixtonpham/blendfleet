"""Unit tests for frame parsing, modulo sharding, and header integrity checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from blendfleet.frames import (
    frame_filename,
    order_frames_for_worker,
    parse_frame_spec,
    validate_image_bytes,
    verify_frame_sequence,
)

VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_parse_frame_spec_ranges_and_steps() -> None:
    assert parse_frame_spec("1-5") == [1, 2, 3, 4, 5]
    assert parse_frame_spec("1-3, 10, 20-26:2, 2") == [1, 2, 3, 10, 20, 22, 24, 26]
    assert parse_frame_spec("42") == [42]


def test_parse_frame_spec_rejects_bad_input() -> None:
    for bad in ("", "10-5", "1-10:0", "abc", "1--3"):
        with pytest.raises(ValueError):
            parse_frame_spec(bad)


def test_order_frames_for_worker_disjoint_primary_slices() -> None:
    frames = list(range(1, 13))
    w0 = order_frames_for_worker(frames, worker_index=0, total_workers=3)
    w1 = order_frames_for_worker(frames, worker_index=1, total_workers=3)
    w2 = order_frames_for_worker(frames, worker_index=2, total_workers=3)

    # First 4 items of each worker are strictly disjoint modulo shards.
    assert w0[:4] == [1, 4, 7, 10]
    assert w1[:4] == [2, 5, 8, 11]
    assert w2[:4] == [3, 6, 9, 12]
    # Every worker still sees the full frame set for tail-sweep recovery.
    assert set(w0) == set(frames)
    assert set(w1) == set(frames)
    assert set(w2) == set(frames)


def test_frame_filename_formatting() -> None:
    assert frame_filename(7) == "frame_0007.png"
    assert frame_filename(7, prefix="shot_##", ext="exr") == "shot_07.exr"


def test_verify_frame_sequence_catches_missing_and_corrupted(tmp_path: Path) -> None:
    (tmp_path / "frame_0001.png").write_bytes(VALID_PNG)
    (tmp_path / "frame_0002.png").write_bytes(b"")  # 0-byte corrupted
    (tmp_path / "frame_0003.png").write_bytes(b"NOT_A_PNG_HEADER")
    # frame 4 missing

    report = verify_frame_sequence(tmp_path, [1, 2, 3, 4])
    assert not report.ok
    assert report.present_frames == [1]
    assert report.missing_frames == [4]
    assert set(report.corrupted_frames.keys()) == {2, 3}

    ok, _ = validate_image_bytes(VALID_PNG, "png")
    assert ok
    bad_ok, _ = validate_image_bytes(b"nope", "png")
    assert not bad_ok
