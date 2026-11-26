# Security Policy

## Supported Versions

Security fixes are applied to the latest minor release on `main`:

| Version | Supported |
|---|---|
| `0.2.x` | Yes |
| `< 0.2.0` | No |

## Reporting a Vulnerability

Do **not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately via [GitHub Security Advisories](https://github.com/brixtonpham/blendfleet/security/advisories/new) on `brixtonpham/blendfleet`. Include:

- A clear description of the issue and affected component (`coordinator`, `accounts`, `worker`, `tunnel`, etc.)
- Minimal reproduction steps or proof of concept
- Any suggested fix or mitigation

You can expect an acknowledgement within 72 hours.

## Credential & Local Runtime Security

`blendfleet` handles Google Colab OAuth tokens and exposes a temporary local coordinator tunnel during distributed renders. Follow these practices:

1. **Account credential storage (`0600`):**
   - Multi-account OAuth credentials live exclusively under `~/.config/blendfleet/accounts/<alias>/` (including `credentials.json`).
   - Account directories are restricted to the local user (`0700` directory, `0600` credential files). Verify permissions if you migrate files between machines (`chmod 600 ~/.config/blendfleet/accounts/*/credentials.json`).
2. **Never commit credentials:**
   - Never copy `credentials.json`, OAuth refresh tokens, or `~/.config/blendfleet/` contents into any git repository, `.blend` archive, or shared Google Drive folder.
   - If a token is accidentally exposed, revoke it immediately at [Google Account Permissions](https://myaccount.google.com/permissions) and re-run `blendfleet account add <alias>`.
3. **Coordinator tunnel bearer tokens:**
   - `blendfleet fly` and `blendfleet serve --tunnel` generate a fresh high-entropy bearer token per session to authenticate workers connecting over Cloudflare Quick Tunnels.
   - Treat printed Colab worker cells and tunnel URLs as session secrets; do not paste them into public logs or shared notebooks.
4. **Scene file safety:**
   - Blender `.blend` files can embed Python auto-run scripts. Only render `.blend` scenes from trusted sources; workers run headless with standard Blender CLI invocation.
