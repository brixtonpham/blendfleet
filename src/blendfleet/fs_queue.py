"""Zero-server shared-filesystem frame queue (Google Drive / rclone / NFS / local FS).

Combines 2-phase interleaved modulo sharding (to avoid Drive FUSE race conditions) with
atomic ``O_CREAT | O_EXCL`` lease files and stale-lease reclamation when a Colab tab dies.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from blendfleet.frames import frame_filename, order_frames_for_worker, validate_image_file


class SharedFSQueue:
    """Coordinate multi-worker Blender rendering over any shared directory."""

    def __init__(
        self,
        output_dir: Path,
        frames: list[int],
        worker_id: str,
        worker_index: int = 0,
        total_workers: int = 1,
        prefix: str = "frame_",
        ext: str = "png",
        pad: int = 4,
        lease_ttl: float = 300.0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.lease_dir = self.output_dir / ".blendfleet_leases"
        self.frames = list(frames)
        self.ordered_frames = order_frames_for_worker(
            frames, worker_index=worker_index, total_workers=total_workers
        )
        self.worker_id = worker_id
        self.prefix = prefix
        self.ext = ext.lstrip(".").lower()
        self.pad = pad
        self.lease_ttl = lease_ttl

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lease_dir.mkdir(parents=True, exist_ok=True)

    def frame_path(self, frame: int) -> Path:
        return self.output_dir / frame_filename(
            frame, prefix=self.prefix, ext=self.ext, pad=self.pad
        )

    def lease_path(self, frame: int) -> Path:
        return self.lease_dir / f"frame_{frame:0{self.pad}d}.lease"

    def _is_frame_done(self, frame: int) -> bool:
        valid, _ = validate_image_file(self.frame_path(frame), ext=self.ext)
        return valid

    def _is_lease_stale(self, lease_file: Path, now: float) -> bool:
        try:
            data = json.loads(lease_file.read_text(encoding="utf-8"))
            heartbeat = float(data.get("heartbeat_at", data.get("claimed_at", 0.0)))
            ttl = float(data.get("lease_ttl", self.lease_ttl))
            return (now - heartbeat) > ttl
        except (OSError, ValueError, TypeError):
            try:
                mtime = lease_file.stat().st_mtime
            except OSError:
                return False
            return (now - mtime) > self.lease_ttl

    def _try_acquire_lease(self, frame: int, now: float) -> bool:
        lpath = self.lease_path(frame)
        if lpath.exists() and self._is_lease_stale(lpath, now):
            try:
                lpath.unlink()
            except OSError:
                return False

        payload = json.dumps(
            {
                "frame": frame,
                "worker_id": self.worker_id,
                "claimed_at": now,
                "heartbeat_at": now,
                "lease_ttl": self.lease_ttl,
            }
        ).encode("utf-8")

        try:
            fd = os.open(str(lpath), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True

    def claim_next(self) -> tuple[int | None, bool]:
        """Claim the next available frame.

        Returns ``(frame, all_done)``.
        - ``(frame, False)``: caller owns ``frame`` and should render it.
        - ``(None, True)``: every frame in ``self.frames`` is verified complete.
        - ``(None, False)``: remaining frames are actively leased by other workers; caller
          should sleep briefly and retry in case a worker crashes.
        """
        now = time.time()
        saw_active_lease = False

        for frame in self.ordered_frames:
            if self._is_frame_done(frame):
                self.release_lease(frame)
                continue
            if self._try_acquire_lease(frame, now):
                # Re-check after lock acquisition in case another worker finished just prior.
                if self._is_frame_done(frame):
                    self.release_lease(frame)
                    continue
                return frame, False
            saw_active_lease = True

        return None, (not saw_active_lease)

    def heartbeat(self, frame: int) -> None:
        """Refresh the heartbeat timestamp on an active lease."""
        lpath = self.lease_path(frame)
        if not lpath.exists():
            return
        now = time.time()
        payload = json.dumps(
            {
                "frame": frame,
                "worker_id": self.worker_id,
                "claimed_at": now,
                "heartbeat_at": now,
                "lease_ttl": self.lease_ttl,
            }
        )
        tmp = lpath.with_suffix(".lease.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, lpath)
        except OSError:
            pass

    def complete_frame(self, frame: int, rendered_local_path: Path) -> Path:
        """Atomically publish a validated local frame into ``output_dir`` and clear its lease."""
        valid, reason = validate_image_file(rendered_local_path, ext=self.ext)
        if not valid:
            raise ValueError(f"Refusing to publish invalid frame {frame}: {reason}")

        dest = self.frame_path(frame)
        part = dest.with_name(f".{dest.name}.{self.worker_id}.part")
        part.write_bytes(rendered_local_path.read_bytes())
        os.replace(part, dest)
        self.release_lease(frame)
        return dest

    def release_lease(self, frame: int) -> None:
        """Release a frame lease (e.g. on completion or clean shutdown)."""
        lpath = self.lease_path(frame)
        try:
            lpath.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
