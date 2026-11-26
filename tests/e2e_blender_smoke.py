"""Real end-to-end smoke test: generates a tiny animated .blend scene, spins up a
blendfleet HTTP coordinator + 2 concurrent persistent Blender workers, renders real PNG
frames, verifies sequence integrity, and stitches them into an MP4 with FFmpeg.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

from blendfleet.coordinator import CoordinatorServer, JobState
from blendfleet.frames import verify_frame_sequence
from blendfleet.stitch import stitch_frames
from blendfleet.worker import find_local_blender, run_worker

CREATE_SCENE_PY = """
import bpy, math, sys
out_blend = sys.argv[-1]
bpy.ops.wm.read_factory_settings(use_empty=False)
scene = bpy.context.scene
scene.render.resolution_x = 320
scene.render.resolution_y = 180
scene.render.resolution_percentage = 100
scene.render.engine = 'CYCLES'
scene.cycles.samples = 4
scene.cycles.use_denoising = False
cube = bpy.data.objects.get('Cube')
if cube:
    for f in range(1, 7):
        scene.frame_set(f)
        cube.rotation_euler = (0.0, 0.0, (f - 1) * math.pi / 6.0)
        cube.keyframe_insert(data_path='rotation_euler', frame=f)
bpy.ops.wm.save_as_mainfile(filepath=out_blend)
"""


def main() -> int:
    blender = find_local_blender()
    if blender is None:
        print("SKIP: No local Blender binary found.")
        return 0
    print(f"[e2e] Using local Blender: {blender}", flush=True)

    tmp_base = Path.home() / ".cache/blendfleet/tmp"
    tmp_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blendfleet_e2e_", dir=tmp_base) as tmp:
        root = Path(tmp)
        gen_py = root / "make_scene.py"
        gen_py.write_text(CREATE_SCENE_PY, encoding="utf-8")
        blend_path = root / "anim.blend"

        subprocess.run(
            [str(blender), "-b", "--python-exit-code", "1", "-P", str(gen_py), "--", str(blend_path)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        assert blend_path.is_file(), "Failed to create test .blend scene"

        frames = [1, 2, 3, 4, 5, 6]
        out_dir = root / "frames"
        state = JobState(
            scene_path=blend_path,
            output_dir=out_dir,
            frames=frames,
            engine="CYCLES",
            samples=4,
            device="auto",
        )

        errors: list[int] = []

        def _spawn(wid: str) -> None:
            rc = run_worker(
                mode="http",
                worker_id=wid,
                blender_bin=str(blender),
                coordinator_url=srv.url,
                token=state.token,
                work_dir=root / f"work_{wid}",
            )
            if rc != 0:
                errors.append(rc)

        with CoordinatorServer(state, host="127.0.0.1", port=0) as srv:
            t1 = threading.Thread(target=_spawn, args=("local-a",))
            t2 = threading.Thread(target=_spawn, args=("local-b",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        assert not errors, f"Worker exited non-zero: {errors}"
        st = state.status()
        print(f"[e2e] Coordinator final status: {st}", flush=True)
        assert st["done"] is True
        assert st["completed"] == 6

        report = verify_frame_sequence(out_dir, frames)
        assert report.ok, f"Verification failed: {report}"

        mp4 = stitch_frames(out_dir, frames, root / "smoke.mp4", fps=24)
        assert mp4.is_file() and mp4.stat().st_size > 1000
        print(f"[e2e] PASS: 6 frames rendered across 2 persistent Blender workers -> {mp4.stat().st_size} bytes MP4", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
