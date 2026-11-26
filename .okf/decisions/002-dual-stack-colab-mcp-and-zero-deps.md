---
type: Decision
title: Zero-Dependency Stdlib Core & Dual-Stack Colab Bridge
description: Keep core blendfleet runtime 100% Python stdlib and pre-allocate a single port for IPv4+IPv6 localhost bindings.
status: stable
tags:
  - decision
  - colab
  - packaging
generated:
  by: agent/antigravity
  at: "2026-11-26T18:05:00+07:00"
sources:
  - id: pyproject
    resource: /pyproject.toml
    title: Package Dependencies
  - id: colab-bridge
    resource: /src/blendfleet/colab_bridge.py
    title: Dual-Stack Port Allocator
---

# Decision

1. Keep `dependencies = []` in `pyproject.toml` so `blendfleet` uses only Python's standard library for HTTP coordination, shared-FS locking, worker launching, verification, and FFmpeg stitching.[^pyproject]
2. Allocate one free port via `allocate_dual_stack_port()` before binding `localhost` WebSocket servers for Colab MCP bridges.[^colab-bridge]

# Rationale

- Ephemeral Google Colab and Kaggle VMs already ship incompatible pinned versions of `httpx`, `pydantic`, `websockets`, and `uvicorn`. A zero-dependency stdlib package installs in <1s with zero version conflicts.
- On macOS, binding `websockets.serve(..., host="localhost", port=0)` assigns different ephemeral ports to `127.0.0.1` and `[::1]`, breaking Chrome's `ws://localhost:<port>` connection when IPv6 resolves first. Pre-allocating one port ensures both stacks listen on the same port.

[^pyproject]: Declared in `pyproject.toml`.
[^colab-bridge]: Implemented in `src/blendfleet/colab_bridge.py`.
