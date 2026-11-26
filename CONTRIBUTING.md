# Contributing to blendfleet

Contributions are welcome — bug fixes, GPU backend tweaks, Colab/Kaggle runtime improvements, tests, and documentation updates.

## Setup

`blendfleet` uses `mise` and `uv` for local development:

```bash
git clone https://github.com/brixtonpham/blendfleet.git
cd blendfleet
mise run setup
```

Without `mise`:

```bash
uv sync --extra dev
```

## Core Architecture Invariants

Keep these rules intact across every PR:

1. **Zero external core dependencies.** `pyproject.toml` `dependencies` stays `[]`. Core runtime modules in `src/blendfleet/` must use Python's standard library only so ephemeral Colab and Kaggle VMs install in under a second without breaking pre-pinned packages.
2. **One persistent `bpy` process per worker.** Never spawn a fresh `blender -b` subprocess per frame. Frame leasing, heartbeats, and rendering happen inside `src/blendfleet/blender_worker.py`.
3. **Atomic frame writes + magic-byte validation.** Never write directly to final frame paths. Render to a `.part` file, verify non-zero size and PNG/EXR/JPEG header bytes, and atomically commit with `os.replace`.
4. **Strict credential permissions.** Stored account credentials under `~/.config/blendfleet/accounts/` must be created with `0600` permissions and must never be logged or written inside repository directories.

## Testing & Quality Checks

Run these locally before opening a pull request:

```bash
mise run lint       # ruff check
mise run test       # unit & multi-worker integration tests
mise run e2e-local  # real 2-worker headless Blender + FFmpeg smoke test
mise run check      # full lint + pytest + OKF validation
```

When fixing a bug, include a regression test that fails before your fix and passes after it.

## Pull Request Checklist

- [ ] `mise run check` passes cleanly.
- [ ] New CLI commands or flags are documented in `README.md` (and `skills/blendfleet/SKILL.md` if relevant).
- [ ] `.okf/` documentation is updated when architectural behavior changes.
- [ ] No real Google tokens, `credentials.json`, or large `.blend` binary fixtures are committed.
