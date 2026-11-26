---
type: Decision
title: Headless Multi-Account Colab Fleet via Isolated ADC Credentials
description: Isolate per-account OAuth2 credentials under ~/.config/blendfleet/accounts/<alias>/ and invoke google-colab-cli with --auth=adc and --config for zero-browser multi-account GPU orchestration.
status: stable
tags:
  - decision
  - colab
  - auth
  - multi-account
generated:
  by: agent/antigravity
  at: "2026-11-26T18:28:00+07:00"
sources:
  - id: accounts-py
    resource: /src/blendfleet/accounts.py
    title: Multi-Account Credential Manager
  - id: fleet-py
    resource: /src/blendfleet/fleet.py
    title: Headless Fleet Orchestrator
---

# Decision

Store each Google account as an isolated `0600` Authorized User JSON (`~/.config/blendfleet/accounts/<alias>/credentials.json`) minted via Google's official remote copy-paste OAuth2 flow without requiring `gcloud`, and drive headless Colab runtimes via `colab --auth=adc --config <sessions.json> run --timeout 0 --gpu <GPU>` with `GOOGLE_APPLICATION_CREDENTIALS` scoped per subprocess.[^accounts-py][^fleet-py]

# Rationale

1. **Zero browser tabs during renders:** Running 5–10 Chrome tabs eats gigabytes of local RAM and breaks on idle tab throttling. Headless `google-colab-cli` keeps the VM alive while the kernel executes and tears it down automatically on exit.
2. **Zero keyring collisions across accounts:** `google-colab-cli`'s default `oauth2` mode writes to a single OS keyring entry (`google-colab-cli/oauth_token`). Passing `GOOGLE_APPLICATION_CREDENTIALS` + `--auth=adc` + `--config` isolates N concurrent Google accounts cleanly in parallel subprocesses.
3. **Zero `gcloud` installation required:** Python stdlib `urllib` performs the OAuth2 code exchange directly into the standard `authorized_user` JSON schema read by `google.auth.default()`.

[^accounts-py]: Implemented in `src/blendfleet/accounts.py`.
[^fleet-py]: Implemented in `src/blendfleet/fleet.py`.
