"""Frame range parsing, interleaved sharding, and output file integrity checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAGIC_HEADERS: dict[str, tuple[bytes, ...]] = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "exr": (b"\x76\x2f\x31\x01",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
}

BLENDER_FORMAT_EXT: dict[str, str] = {
    "PNG": "png",
    "OPEN_EXR": "exr",
    "OPEN_EXR_MULTILAYER": "exr",
    "JPEG": "jpg",
    "WEBP": "webp",
}


def parse_frame_spec(spec: str) -> list[int]:
    """Parse a Blender-style frame specification into a sorted deduplicated frame list.

    Supports single frames, inclusive ranges, and optional step sizes:
    e.g. ``"1-10, 20, 35-55:5"`` -> ``[1, ..., 10, 20, 35, 40, 45, 50, 55]``.
    """
    if not spec or not spec.strip():
        raise ValueError("Frame specification cannot be empty.")

    frames: set[int] = set()
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(-?\d+)(?:-(-?\d+)(?::(\d+))?)?", part)
        if not match:
            raise ValueError(f"Invalid frame specification segment: {part!r}")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        step = int(match.group(3)) if match.group(3) is not None else 1
        if step <= 0:
            raise ValueError(f"Frame step must be positive: {part!r}")
        if end < start:
            raise ValueError(f"Frame range end ({end}) precedes start ({start}): {part!r}")
        frames.update(range(start, end + 1, step))

    if not frames:
        raise ValueError(f"No frames parsed from specification: {spec!r}")
    return sorted(frames)


def order_frames_for_worker(
    frames: list[int],
    worker_index: int = 0,
    total_workers: int = 1,
) -> list[int]:
    """Order frames using 2-phase interleaved modulo sharding.

    Phase 1 yields frames assigned to ``worker_index`` (``idx % total_workers == worker_index``),
    eliminating lock contention across workers on high-latency shared mounts (like Google Drive).
    Phase 2 appends the remaining frames (rotated by ``worker_index``) so surviving workers
    automatically steal straggler frames if another worker disconnects early.
    """
    if total_workers <= 1:
        return list(frames)
    if worker_index < 0 or worker_index >= total_workers:
        raise ValueError(
            f"worker_index ({worker_index}) must be in [0, {total_workers - 1}]"
        )

    primary = [f for i, f in enumerate(frames) if i % total_workers == worker_index]
    secondary = [f for i, f in enumerate(frames) if i % total_workers != worker_index]
    if secondary:
        offset = (worker_index * max(1, len(secondary) // total_workers)) % len(secondary)
        secondary = secondary[offset:] + secondary[:offset]
    return primary + secondary


def frame_filename(frame: int, prefix: str = "frame_", ext: str = "png", pad: int = 4) -> str:
    """Format the canonical output filename for a frame number."""
    clean_ext = ext.lstrip(".").lower()
    if "#" in prefix:
        hashes = re.search(r"#+", prefix)
        assert hashes is not None
        width = len(hashes.group(0))
        stem = prefix[: hashes.start()] + f"{frame:0{width}d}" + prefix[hashes.end() :]
        return f"{stem}.{clean_ext}"
    return f"{prefix}{frame:0{pad}d}.{clean_ext}"


def validate_image_bytes(data: bytes, ext: str) -> tuple[bool, str]:
    """Verify rendered frame payload is non-empty and matches its expected file magic bytes."""
    if not data:
        return False, "empty payload (0 bytes)"
    clean_ext = ext.lstrip(".").lower()
    expected = MAGIC_HEADERS.get(clean_ext)
    if expected and not any(data.startswith(sig) for sig in expected):
        return False, f"invalid {clean_ext.upper()} header bytes: {data[:8]!r}"
    return True, "ok"


def validate_image_file(path: Path, ext: str | None = None) -> tuple[bool, str]:
    """Validate an image file on disk for non-zero size and header magic bytes."""
    if not path.is_file():
        return False, "file missing"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"stat failed: {exc}"
    if size == 0:
        return False, "0-byte file"

    inferred_ext = (ext or path.suffix.lstrip(".")).lower()
    expected = MAGIC_HEADERS.get(inferred_ext)
    if not expected:
        return True, "ok"
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return False, f"read failed: {exc}"
    if not any(head.startswith(sig) for sig in expected):
        return False, f"corrupted {inferred_ext.upper()} header"
    return True, "ok"


@dataclass(frozen=True)
class VerificationReport:
    expected_frames: list[int]
    present_frames: list[int]
    missing_frames: list[int]
    corrupted_frames: dict[int, str]

    @property
    def ok(self) -> bool:
        return not self.missing_frames and not self.corrupted_frames


def verify_frame_sequence(
    output_dir: Path,
    frames: list[int],
    prefix: str = "frame_",
    ext: str = "png",
    pad: int = 4,
) -> VerificationReport:
    """Audit all expected frames in ``output_dir`` for completeness and header integrity."""
    present: list[int] = []
    missing: list[int] = []
    corrupted: dict[int, str] = {}

    for frame in frames:
        fpath = output_dir / frame_filename(frame, prefix=prefix, ext=ext, pad=pad)
        if not fpath.exists():
            missing.append(frame)
            continue
        valid, reason = validate_image_file(fpath, ext=ext)
        if valid:
            present.append(frame)
        else:
            corrupted[frame] = reason

    return VerificationReport(
        expected_frames=list(frames),
        present_frames=present,
        missing_frames=missing,
        corrupted_frames=corrupted,
    )
