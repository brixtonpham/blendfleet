"""Headless multi-account Google Colab credential and profile manager.

Implements Google's OAuth2 remote-code flow using 100% Python stdlib so users can
authenticate multiple Google accounts once without installing ``gcloud`` CLI. Each
account gets its own isolated ADC credentials file (``0o600``) and ``google-colab-cli``
session state file under ``~/.config/blendfleet/accounts/<alias>/``.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Official Google Colab CLI public OAuth2 client (googlecolab/google-colab-cli v0.8.0).
CLIENT_ID = "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"
CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO_URI = "https://www.googleapis.com/oauth2/v2/userinfo"
REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
)

DEFAULT_ACCOUNTS_ROOT = Path.home() / ".config" / "blendfleet" / "accounts"
DEFAULT_CHROME_LOCAL_STATE = (
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Local State"
)

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CCU_RE = re.compile(
    r"(?:balance|available|remaining|compute\s*units?|ccu)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def accounts_root(accounts_dir: Path | None = None) -> Path:
    """Return the root directory storing isolated blendfleet account configs."""
    if accounts_dir is not None:
        return Path(accounts_dir).expanduser()
    env_dir = os.environ.get("BLENDFLEET_ACCOUNTS_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return DEFAULT_ACCOUNTS_ROOT


def validate_alias(alias: str) -> str:
    """Validate and normalize an account alias."""
    cleaned = alias.strip()
    if not cleaned:
        raise ValueError("Account alias cannot be empty.")
    if cleaned.lower() in {"all", "none"}:
        raise ValueError(f"Account alias {cleaned!r} is reserved.")
    if not _ALIAS_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid account alias {cleaned!r}. Use 1-64 characters: letters, digits, '.', '_', '-'."
        )
    return cleaned


def account_dir(alias: str, *, accounts_dir: Path | None = None) -> Path:
    """Return the directory for a specific account alias."""
    return accounts_root(accounts_dir) / validate_alias(alias)


def credentials_path(alias: str, *, accounts_dir: Path | None = None) -> Path:
    """Return the ADC Authorized User JSON path for ``alias``."""
    return account_dir(alias, accounts_dir=accounts_dir) / "credentials.json"


def sessions_path(alias: str, *, accounts_dir: Path | None = None) -> Path:
    """Return the isolated ``google-colab-cli`` sessions.json path for ``alias``."""
    return account_dir(alias, accounts_dir=accounts_dir) / "sessions.json"


def meta_path(alias: str, *, accounts_dir: Path | None = None) -> Path:
    """Return the metadata JSON path for ``alias``."""
    return account_dir(alias, accounts_dir=accounts_dir) / "meta.json"


def account_env(alias: str, *, accounts_dir: Path | None = None) -> dict[str, str]:
    """Return environment variables isolating ``google-colab-cli`` for ``alias``."""
    return {
        "GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path(alias, accounts_dir=accounts_dir)),
        "COLAB_CONFIG": str(sessions_path(alias, accounts_dir=accounts_dir)),
    }


def generate_pkce_pair() -> tuple[str, str]:
    """Generate an RFC 7636 PKCE ``(code_verifier, code_challenge)`` pair (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _pending_pkce_path(alias: str, *, accounts_dir: Path | None = None) -> Path:
    return account_dir(alias, accounts_dir=accounts_dir) / ".pending_pkce.json"


def build_auth_url(
    *,
    redirect_uri: str = REDIRECT_URI,
    code_challenge: str | None = None,
    state: str | None = None,
    login_hint: str | None = None,
    token_usage: str | None = "remote",
) -> str:
    """Build Google's OAuth2 authorization URL with PKCE (S256) for Colab ADC credentials."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if token_usage:
        params["token_usage"] = token_usage
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URI}?{urllib.parse.urlencode(params)}"


def exchange_auth_code(
    code: str,
    *,
    redirect_uri: str = REDIRECT_URI,
    code_verifier: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Exchange a Google OAuth2 authorization code (with optional PKCE verifier) for tokens."""
    cleaned = code.strip()
    if not cleaned:
        raise ValueError("Authorization code cannot be empty.")

    form: dict[str, str] = {
        "code": cleaned,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    payload = urllib.parse.urlencode(form).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URI,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OAuth token exchange failed (HTTP {exc.code}): {body}") from exc

    if not data.get("refresh_token"):
        raise RuntimeError(
            "Google did not return a refresh_token. Re-run authorization and grant offline consent."
        )
    return data


def fetch_userinfo(access_token: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch the authenticated Google user's profile/email using an access token."""
    req = urllib.request.Request(
        USERINFO_URI,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return dict(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Failed to fetch Google userinfo (HTTP {exc.code}): {body}") from exc


def open_auth_url(url: str, *, chrome_profile: str | None = None) -> None:
    """Open the OAuth URL in a specific Chrome profile (macOS/Linux) or default browser."""
    if chrome_profile:
        if sys.platform == "darwin":
            subprocess.Popen(
                [
                    "open",
                    "-na",
                    "Google Chrome",
                    "--args",
                    f"--profile-directory={chrome_profile}",
                    url,
                ]
            )
            return
        chrome_bin = shutil.which("google-chrome") or shutil.which("chromium")
        if chrome_bin:
            subprocess.Popen([chrome_bin, f"--profile-directory={chrome_profile}", url])
            return
    webbrowser.open(url)


def save_account_credentials(
    alias: str,
    *,
    refresh_token: str,
    email: str = "",
    chrome_profile: str | None = None,
    accounts_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist ADC Authorized User JSON (0o600) and metadata JSON for ``alias``."""
    valid_alias = validate_alias(alias)
    acc_dir = account_dir(valid_alias, accounts_dir=accounts_dir)
    acc_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(acc_dir, 0o700)

    creds_file = credentials_path(valid_alias, accounts_dir=accounts_dir)
    creds_payload = {
        "type": "authorized_user",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "quota_project_id": "colab-cli",
        "scopes": list(SCOPES),
    }
    creds_file.write_text(json.dumps(creds_payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(creds_file, 0o600)

    added_at = datetime.now(timezone.utc).isoformat()
    meta_payload: dict[str, Any] = {
        "alias": valid_alias,
        "email": email,
        "added_at": added_at,
        "chrome_profile": chrome_profile,
    }
    meta_file = meta_path(valid_alias, accounts_dir=accounts_dir)
    meta_file.write_text(json.dumps(meta_payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(meta_file, 0o600)

    return {
        **meta_payload,
        "credentials_path": str(creds_file),
        "sessions_path": str(sessions_path(valid_alias, accounts_dir=accounts_dir)),
    }


def _run_loopback_oauth(
    alias: str,
    *,
    chrome_profile: str | None = None,
    timeout: float = 180.0,
    print_fn: Callable[[str], None] = print,
) -> tuple[str, str, str]:
    """Run a 1-shot ``http://localhost:<port>/`` PKCE OAuth flow (zero manual copy-paste)."""
    verifier, challenge = generate_pkce_pair()
    expected_state = secrets.token_urlsafe(24)
    captured: dict[str, str] = {}

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if "error" in qs:
                captured["error"] = qs["error"][0]
                body = (
                    b"<html><body style='font-family:system-ui;padding:40px'>"
                    b"<h2>Authorization declined</h2><p>You may close this tab.</p>"
                    b"</body></html>"
                )
                self.send_response(400)
            elif "code" in qs:
                captured["code"] = qs["code"][0]
                captured["state"] = qs.get("state", [""])[0]
                body = (
                    "<html><body style='font-family:system-ui;padding:40px'>"
                    "<h2>&#10003; blendfleet authorized!</h2>"
                    f"<p>Account <b>{alias}</b> is connected. You can close this tab.</p>"
                    "</body></html>"
                ).encode()
                self.send_response(200)
            else:
                self.send_response(204)
                self.end_headers()
                return
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    httpd.timeout = timeout
    port = int(httpd.server_address[1])
    redirect_uri = f"http://localhost:{port}/"
    auth_url = build_auth_url(
        redirect_uri=redirect_uri,
        code_challenge=challenge,
        state=expected_state,
        token_usage=None,
    )

    print_fn(f"Authorize Google Colab account '{alias}' (browser will redirect back automatically):\n\n  {auth_url}\n")
    open_auth_url(auth_url, chrome_profile=chrome_profile)

    try:
        while "code" not in captured and "error" not in captured:
            httpd.handle_request()
            if not captured:
                raise TimeoutError(f"Timed out after {int(timeout)}s waiting for OAuth callback.")
    finally:
        httpd.server_close()

    if "error" in captured:
        raise RuntimeError(f"Google OAuth returned error: {captured['error']}")
    if captured.get("state") != expected_state:
        raise RuntimeError("OAuth state mismatch; aborting authorization.")
    return captured["code"], redirect_uri, verifier


def add_account(
    alias: str,
    *,
    code: str | None = None,
    chrome_profile: str | None = None,
    no_browser: bool = False,
    accounts_dir: Path | None = None,
    input_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the login-once OAuth2 PKCE flow and save ADC credentials for ``alias``.

    Supports three modes:
    - Interactive browser login (default or ``--chrome-profile``): uses a 1-shot
      ``http://localhost:<port>/`` PKCE callback so the user only clicks **Allow**
      (zero copy-paste required).
    - Headless/agent 2-step flow: ``--no-browser`` saves a PKCE verifier, prints the
      remote auth URL, and exits; pass ``--code <auth_code>`` to complete non-interactively.
    - Custom ``input_fn`` (tests / TTY remote flow): prompts for the pasted code.
    """
    valid_alias = validate_alias(alias)
    auth_code = code.strip() if code else ""
    redirect_uri = REDIRECT_URI
    code_verifier: str | None = None

    if auth_code:
        # Load saved PKCE verifier if created by a prior `--no-browser` step.
        pkce_file = _pending_pkce_path(valid_alias, accounts_dir=accounts_dir)
        if pkce_file.is_file():
            try:
                pending = json.loads(pkce_file.read_text(encoding="utf-8"))
                code_verifier = pending.get("code_verifier")
                redirect_uri = str(pending.get("redirect_uri") or REDIRECT_URI)
            except (OSError, json.JSONDecodeError):
                pass
    elif not no_browser and input_fn is None:
        auth_code, redirect_uri, code_verifier = _run_loopback_oauth(
            valid_alias,
            chrome_profile=chrome_profile,
            print_fn=print_fn,
        )
    else:
        code_verifier, challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(24)
        auth_url = build_auth_url(
            redirect_uri=REDIRECT_URI,
            code_challenge=challenge,
            state=state,
            token_usage="remote",
        )
        print_fn(f"Authorize Google Colab account '{valid_alias}' by opening:\n\n  {auth_url}\n")
        if not no_browser:
            open_auth_url(auth_url, chrome_profile=chrome_profile)

        if no_browser and input_fn is None and not sys.stdin.isatty():
            acc_dir = account_dir(valid_alias, accounts_dir=accounts_dir)
            acc_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(acc_dir, 0o700)
            pkce_file = _pending_pkce_path(valid_alias, accounts_dir=accounts_dir)
            pkce_file.write_text(
                json.dumps({"code_verifier": code_verifier, "redirect_uri": REDIRECT_URI}),
                encoding="utf-8",
            )
            os.chmod(pkce_file, 0o600)
            print_fn(
                f"After signing in, copy the authorization code and run:\n"
                f"  blendfleet account add {valid_alias} --code <AUTH_CODE>"
            )
            return {
                "status": "awaiting_code",
                "alias": valid_alias,
                "auth_url": auth_url,
            }

        reader = input_fn or input
        auth_code = reader("Paste authorization code: ").strip()
        if not auth_code:
            raise ValueError("Authorization code cannot be empty.")

    tokens = exchange_auth_code(
        auth_code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )
    pkce_file = _pending_pkce_path(valid_alias, accounts_dir=accounts_dir)
    if pkce_file.exists():
        pkce_file.unlink(missing_ok=True)

    email = ""
    access_token = tokens.get("access_token")
    if access_token:
        userinfo = fetch_userinfo(str(access_token))
        email = str(userinfo.get("email", ""))

    saved = save_account_credentials(
        valid_alias,
        refresh_token=str(tokens["refresh_token"]),
        email=email,
        chrome_profile=chrome_profile,
        accounts_dir=accounts_dir,
    )
    return {"status": "saved", **saved}


def list_accounts(*, accounts_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return metadata for all saved accounts sorted by alias."""
    root = accounts_root(accounts_dir)
    if not root.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        creds = entry / "credentials.json"
        if not creds.is_file():
            continue
        meta_file = entry / "meta.json"
        meta: dict[str, Any] = {}
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        rows.append(
            {
                "alias": str(meta.get("alias") or entry.name),
                "email": str(meta.get("email") or ""),
                "added_at": meta.get("added_at"),
                "chrome_profile": meta.get("chrome_profile"),
                "credentials_path": str(creds),
                "sessions_path": str(entry / "sessions.json"),
            }
        )
    return rows


def remove_account(alias: str, *, accounts_dir: Path | None = None) -> bool:
    """Delete stored credentials, metadata, and session state for ``alias``."""
    target = account_dir(alias, accounts_dir=accounts_dir)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def resolve_accounts(spec: str, *, accounts_dir: Path | None = None) -> list[str]:
    """Resolve an ``--accounts`` selector (``all``, ``none``, or ``a,b,c``) into aliases."""
    cleaned = spec.strip()
    if not cleaned or cleaned.lower() == "none":
        return []
    if cleaned.lower() == "all":
        return [row["alias"] for row in list_accounts(accounts_dir=accounts_dir)]

    aliases: list[str] = []
    missing: list[str] = []
    for raw in cleaned.split(","):
        part = raw.strip()
        if not part:
            continue
        alias = validate_alias(part)
        if alias not in aliases:
            aliases.append(alias)
        if not credentials_path(alias, accounts_dir=accounts_dir).is_file():
            missing.append(alias)

    if missing:
        joined = ", ".join(missing)
        raise FileNotFoundError(
            f"Unknown blendfleet account(s): {joined}. Run `blendfleet account list` or "
            f"`blendfleet account add <alias>` first."
        )
    return aliases


def _profile_sort_key(profile_dir: str) -> tuple[int, int, str]:
    if profile_dir == "Default":
        return (0, 0, profile_dir)
    match = re.fullmatch(r"Profile (\d+)", profile_dir)
    if match:
        return (1, int(match.group(1)), profile_dir)
    return (2, 0, profile_dir)


def list_chrome_profiles(local_state_path: Path | None = None) -> list[dict[str, str]]:
    """List local Google Chrome profiles from ``Local State`` (``profile.info_cache``)."""
    state_file = Path(local_state_path).expanduser() if local_state_path else DEFAULT_CHROME_LOCAL_STATE
    if not state_file.is_file():
        return []

    data = json.loads(state_file.read_text(encoding="utf-8"))
    info_cache = data.get("profile", {}).get("info_cache", {})
    if not isinstance(info_cache, dict):
        return []

    profiles: list[dict[str, str]] = []
    for prof_dir in sorted(info_cache.keys(), key=_profile_sort_key):
        info = info_cache.get(prof_dir)
        if not isinstance(info, dict):
            continue
        profiles.append(
            {
                "profile_directory": str(prof_dir),
                "name": str(info.get("name") or ""),
                "gaia_name": str(info.get("gaia_name") or ""),
                "email": str(info.get("user_name") or ""),
            }
        )
    return profiles


def find_colab_binary() -> str:
    """Locate the ``colab`` CLI executable from ``google-colab-cli``."""
    found = shutil.which("colab")
    if found:
        return found
    for cand in (
        Path.home() / ".local" / "bin" / "colab",
        Path.home() / ".local" / "share" / "mise" / "shims" / "colab",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise FileNotFoundError(
        "google-colab-cli (`colab`) not found on PATH. Install it with "
        "`pip install 'blendfleet[colab]'` or `uv tool install google-colab-cli`."
    )


def _extract_email(text: str, fallback: str = "") -> str:
    for line in text.splitlines():
        if "email" in line.lower():
            match = _EMAIL_RE.search(line)
            if match:
                return match.group(0)
    match = _EMAIL_RE.search(text)
    if match:
        return match.group(0)
    return fallback


def _extract_ccu_balance(text: str) -> str | None:
    match = _CCU_RE.search(text)
    if match:
        return match.group(1)
    return None


def check_account(
    alias: str,
    *,
    accounts_dir: Path | None = None,
    colab_bin: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Probe live authentication identity and Colab compute-unit balance for ``alias``."""
    valid_alias = validate_alias(alias)
    creds = credentials_path(valid_alias, accounts_dir=accounts_dir)
    sess = sessions_path(valid_alias, accounts_dir=accounts_dir)
    if not creds.is_file():
        return {
            "alias": valid_alias,
            "ok": False,
            "status": "missing",
            "email": "",
            "ccu_balance": None,
            "usage": "",
            "whoami": "",
            "error": f"Credentials file not found: {creds}",
        }

    saved_email = ""
    meta_file = meta_path(valid_alias, accounts_dir=accounts_dir)
    if meta_file.is_file():
        try:
            saved_email = str(json.loads(meta_file.read_text(encoding="utf-8")).get("email") or "")
        except (OSError, json.JSONDecodeError):
            saved_email = ""

    exe = colab_bin or find_colab_binary()
    env = {**os.environ, **account_env(valid_alias, accounts_dir=accounts_dir)}
    base_cmd = [exe, "--auth=adc", "--config", str(sess)]

    try:
        whoami_proc = subprocess.run(
            [*base_cmd, "whoami"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "alias": valid_alias,
            "ok": False,
            "status": "timeout",
            "email": saved_email,
            "ccu_balance": None,
            "usage": "",
            "whoami": "",
            "error": f"`colab whoami` timed out after {timeout:.0f}s",
        }

    whoami_out = (whoami_proc.stdout or "").strip()
    whoami_err = (whoami_proc.stderr or "").strip()
    if whoami_proc.returncode != 0:
        return {
            "alias": valid_alias,
            "ok": False,
            "status": "auth_error",
            "email": saved_email,
            "ccu_balance": None,
            "usage": "",
            "whoami": whoami_out,
            "error": whoami_err or whoami_out or f"colab whoami exited {whoami_proc.returncode}",
        }

    email = _extract_email(whoami_out, fallback=saved_email)

    try:
        usage_proc = subprocess.run(
            [*base_cmd, "usage"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "alias": valid_alias,
            "ok": False,
            "status": "timeout",
            "email": email,
            "ccu_balance": None,
            "usage": "",
            "whoami": whoami_out,
            "error": f"`colab usage` timed out after {timeout:.0f}s",
        }

    usage_out = (usage_proc.stdout or "").strip()
    usage_err = (usage_proc.stderr or "").strip()
    if usage_proc.returncode != 0:
        return {
            "alias": valid_alias,
            "ok": False,
            "status": "error",
            "email": email,
            "ccu_balance": None,
            "usage": usage_out,
            "whoami": whoami_out,
            "error": usage_err or usage_out or f"colab usage exited {usage_proc.returncode}",
        }

    return {
        "alias": valid_alias,
        "ok": True,
        "status": "ok",
        "email": email,
        "ccu_balance": _extract_ccu_balance(usage_out),
        "usage": usage_out,
        "whoami": whoami_out,
        "error": None,
    }


def check_all_accounts(
    aliases: list[str] | None = None,
    *,
    accounts_dir: Path | None = None,
    colab_bin: str | None = None,
    max_workers: int = 8,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Check live identity and CCU balance across multiple accounts in parallel."""
    target_aliases = (
        list(aliases)
        if aliases is not None
        else [row["alias"] for row in list_accounts(accounts_dir=accounts_dir)]
    )
    if not target_aliases:
        return []

    exe = colab_bin or find_colab_binary()
    pool_size = max(1, min(max_workers, len(target_aliases)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = [
            pool.submit(
                check_account,
                alias,
                accounts_dir=accounts_dir,
                colab_bin=exe,
                timeout=timeout,
            )
            for alias in target_aliases
        ]
        return [fut.result() for fut in futures]
