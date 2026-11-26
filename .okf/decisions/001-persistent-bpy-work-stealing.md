---
type: Decision
title: Persistent bpy Work-Stealing Loop Over Per-Frame Subprocess Restarts
description: Run frame claiming inside one persistent Blender process rather than invoking a fresh blender subprocess per frame.
status: stable
tags:
  - decision
  - blender
  - performance
generated:
  by: agent/antigravity
  at: "2026-11-26T18:05:00+07:00"
sources:
  - id: blender-worker
    resource: /src/blendfleet/blender_worker.py
    title: Persistent bpy Worker Implementation
---

# Decision

Keep a single `blender -b scene.blend -P blender_worker.py` process alive per worker and pull frames dynamically inside Blender's embedded Python loop (`scene.frame_set(f); bpy.ops.render.render(write_still=True)`).[^blender-worker]

# Rationale

1. **Zero per-frame reload tax:** Restarting Blender per frame (the pattern used by existing multi-Colab notebooks) reloads `.blend`, unpacks textures, and rebuilds BVH/OptiX shader caches every frame (4–10s wasted per frame).
2. **Heterogeneous load balancing:** An Apple M3 Max (Metal RT) renders ~2.8× faster than a Colab `T4`, and a Colab `G4` renders ~9.5× faster. Work-stealing keeps every GPU busy until the final frame instead of stranding fast GPUs behind slow static slices.
3. **Fault tolerance:** Background heartbeat threads extend active frame leases; if a free Colab tab disconnects, surviving workers reclaim the frame automatically once `lease_ttl` expires.

# Trade-offs

- `blender_worker.py` must depend only on Python stdlib + `bpy` so it runs cleanly inside Blender's bundled Python without pip installs inside Blender.

[^blender-worker]: Implemented in `src/blendfleet/blender_worker.py`.
