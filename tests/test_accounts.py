"""Unit tests for headless multi-account Google Colab OAuth & profile manager."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from blendfleet import accounts, cli


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_build_auth_url_contains_expected_oauth_params() -> None:
    url = accounts.build_auth_url(login_hint="artist@example.com")
    parsed = urllib.parse.urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == accounts.AUTH_URI

    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["client_id"] == [accounts.CLIENT_ID]
    assert qs["redirect_uri"] == [accounts.REDIRECT_URI]
    assert qs["response_type"] == ["code"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["token_usage"] == ["remote"]
    assert qs["login_hint"] == ["artist@example.com"]
    assert set(qs["scope"][0].split()) == set(accounts.SCOPES)


def test_validate_alias_rules() -> None:
    assert accounts.validate_alias("acc-1") == "acc-1"
    assert accounts.validate_alias("  triad.dev_02  ") == "triad.dev_02"
    for bad in ("", "   ", "all", "ALL", "none", "../escape", "a/b", "has space"):
        with pytest.raises(ValueError):
            accounts.validate_alias(bad)


def test_save_list_env_resolve_and_remove_account(tmp_path: Path) -> None:
    root = tmp_path / "accounts"
    saved = accounts.save_account_credentials(
        "gpu1",
        refresh_token="1//fake-refresh-token",
        email="gpu1@example.com",
        chrome_profile="Profile 19",
        accounts_dir=root,
    )
    assert saved["alias"] == "gpu1"
    assert saved["email"] == "gpu1@example.com"
    assert saved["chrome_profile"] == "Profile 19"

    creds_file = Path(saved["credentials_path"])
    assert creds_file.is_file()
    mode = stat.S_IMODE(creds_file.stat().st_mode)
    assert mode == 0o600

    creds_json = json.loads(creds_file.read_text(encoding="utf-8"))
    assert creds_json == {
        "type": "authorized_user",
        "client_id": accounts.CLIENT_ID,
        "client_secret": accounts.CLIENT_SECRET,
        "refresh_token": "1//fake-refresh-token",
        "quota_project_id": "colab-cli",
        "scopes": list(accounts.SCOPES),
    }

    env = accounts.account_env("gpu1", accounts_dir=root)
    assert env == {
        "GOOGLE_APPLICATION_CREDENTIALS": str(root / "gpu1" / "credentials.json"),
        "COLAB_CONFIG": str(root / "gpu1" / "sessions.json"),
    }

    accounts.save_account_credentials(
        "gpu2",
        refresh_token="1//second-refresh",
        email="gpu2@example.com",
        accounts_dir=root,
    )

    listed = accounts.list_accounts(accounts_dir=root)
    assert [row["alias"] for row in listed] == ["gpu1", "gpu2"]
    assert accounts.resolve_accounts("all", accounts_dir=root) == ["gpu1", "gpu2"]
    assert accounts.resolve_accounts("gpu2, gpu1, gpu2", accounts_dir=root) == ["gpu2", "gpu1"]
    assert accounts.resolve_accounts("none", accounts_dir=root) == []

    with pytest.raises(FileNotFoundError, match="ghost"):
        accounts.resolve_accounts("gpu1,ghost", accounts_dir=root)

    assert accounts.remove_account("gpu1", accounts_dir=root) is True
    assert accounts.remove_account("gpu1", accounts_dir=root) is False
    assert [row["alias"] for row in accounts.list_accounts(accounts_dir=root)] == ["gpu2"]


def test_add_account_noninteractive_with_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_urlopen(req: Any, timeout: float = 30.0) -> _FakeHTTPResponse:
        calls.append((req.full_url, req.get_method()))
        if req.full_url == accounts.TOKEN_URI:
            body = urllib.parse.parse_qs(req.data.decode("utf-8"))
            assert body["code"] == ["4/auth-code-123"]
            assert body["client_id"] == [accounts.CLIENT_ID]
            assert body["client_secret"] == [accounts.CLIENT_SECRET]
            assert body["redirect_uri"] == [accounts.REDIRECT_URI]
            assert body["grant_type"] == ["authorization_code"]
            return _FakeHTTPResponse(
                {"access_token": "ya29.test-access", "refresh_token": "1//test-refresh"}
            )
        if req.full_url == accounts.USERINFO_URI:
            assert req.get_header("Authorization") == "Bearer ya29.test-access"
            return _FakeHTTPResponse({"email": "triadmakers.dev@gmail.com"})
        raise AssertionError(f"Unexpected URL: {req.full_url}")

    monkeypatch.setattr(accounts.urllib.request, "urlopen", fake_urlopen)

    opened = False

    def fail_open(*args: Any, **kwargs: Any) -> None:
        nonlocal opened
        opened = True

    monkeypatch.setattr(accounts, "open_auth_url", fail_open)

    result = accounts.add_account(
        "triad",
        code="4/auth-code-123",
        chrome_profile="Profile 19",
        no_browser=True,
        accounts_dir=tmp_path,
    )
    assert not opened
    assert result["status"] == "saved"
    assert result["email"] == "triadmakers.dev@gmail.com"
    assert calls == [(accounts.TOKEN_URI, "POST"), (accounts.USERINFO_URI, "GET")]


def test_add_account_no_browser_non_tty_returns_awaiting_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(accounts.sys, "stdin", io.StringIO(""))
    messages: list[str] = []
    result = accounts.add_account(
        "headless1",
        no_browser=True,
        accounts_dir=tmp_path,
        print_fn=messages.append,
    )
    assert result["status"] == "awaiting_code"
    assert result["alias"] == "headless1"
    assert accounts.AUTH_URI in result["auth_url"]
    assert any("--code <AUTH_CODE>" in m for m in messages)


def test_add_account_opens_macos_chrome_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched_cmds: list[list[str]] = []

    def fake_popen(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        launched_cmds.append(cmd)
        return None

    monkeypatch.setattr(accounts.sys, "platform", "darwin")
    monkeypatch.setattr(accounts.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        accounts,
        "exchange_auth_code",
        lambda code: {"access_token": "tok", "refresh_token": "1//rf"},
    )
    monkeypatch.setattr(accounts, "fetch_userinfo", lambda token: {"email": "u@example.com"})

    res = accounts.add_account(
        "acc-chrome",
        chrome_profile="Profile 19",
        accounts_dir=tmp_path,
        input_fn=lambda _: "4/pasted-code",
        print_fn=lambda _: None,
    )
    assert res["status"] == "saved"
    assert len(launched_cmds) == 1
    assert launched_cmds[0][:5] == [
        "open",
        "-na",
        "Google Chrome",
        "--args",
        "--profile-directory=Profile 19",
    ]
    assert launched_cmds[0][5].startswith(accounts.AUTH_URI)


def test_exchange_auth_code_error_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        accounts.urllib.request,
        "urlopen",
        lambda req, timeout=30.0: _FakeHTTPResponse({"access_token": "only-access"}),
    )
    with pytest.raises(RuntimeError, match="refresh_token"):
        accounts.exchange_auth_code("4/no-refresh")

    def raise_http(req: Any, timeout: float = 30.0) -> Any:
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}')
        )

    monkeypatch.setattr(accounts.urllib.request, "urlopen", raise_http)
    with pytest.raises(RuntimeError, match="invalid_grant"):
        accounts.exchange_auth_code("4/expired")


def test_list_chrome_profiles_parses_and_sorts(tmp_path: Path) -> None:
    local_state = tmp_path / "Local State"
    local_state.write_text(
        json.dumps(
            {
                "profile": {
                    "info_cache": {
                        "Profile 19": {
                            "name": "Triad-Makers",
                            "gaia_name": "Triad Makers",
                            "user_name": "triadmakers.dev@gmail.com",
                        },
                        "Profile 2": {
                            "name": "Render Two",
                            "gaia_name": "Two",
                            "user_name": "two@example.com",
                        },
                        "Default": {
                            "name": "Personal",
                            "gaia_name": "Sam",
                            "user_name": "sam@example.com",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profiles = accounts.list_chrome_profiles(local_state)
    assert [p["profile_directory"] for p in profiles] == ["Default", "Profile 2", "Profile 19"]
    assert profiles[2] == {
        "profile_directory": "Profile 19",
        "name": "Triad-Makers",
        "gaia_name": "Triad Makers",
        "email": "triadmakers.dev@gmail.com",
    }
    assert accounts.list_chrome_profiles(tmp_path / "missing") == []


def test_check_account_and_check_all_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "accounts"
    accounts.save_account_credentials(
        "a1", refresh_token="1//r1", email="a1@example.com", accounts_dir=root
    )
    accounts.save_account_credentials(
        "a2", refresh_token="1//r2", email="a2@example.com", accounts_dir=root
    )

    seen_calls: list[tuple[list[str], str]] = []

    def fake_run(cmd: list[str], *, env: dict[str, str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_calls.append((cmd, env["GOOGLE_APPLICATION_CREDENTIALS"]))
        sub = cmd[-1]
        if "a2" in env["GOOGLE_APPLICATION_CREDENTIALS"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Token has been expired or revoked")
        if sub == "whoami":
            return subprocess.CompletedProcess(cmd, 0, stdout="email: live-a1@example.com\nexpires: 3500s\n", stderr="")
        if sub == "usage":
            return subprocess.CompletedProcess(cmd, 0, stdout="Compute units balance: 94.25\nRate: 1.96/hr\n", stderr="")
        raise AssertionError(f"Unexpected cmd: {cmd}")

    monkeypatch.setattr(accounts.subprocess, "run", fake_run)

    results = accounts.check_all_accounts(["a1", "a2"], accounts_dir=root, colab_bin="/fake/colab")
    assert len(results) == 2

    r1, r2 = results
    assert r1["alias"] == "a1"
    assert r1["ok"] is True
    assert r1["status"] == "ok"
    assert r1["email"] == "live-a1@example.com"
    assert r1["ccu_balance"] == "94.25"

    assert r2["alias"] == "a2"
    assert r2["ok"] is False
    assert r2["status"] == "auth_error"
    assert "expired" in (r2["error"] or "")

    # Verify strict --auth=adc and isolated --config per account.
    for cmd, gac in seen_calls:
        assert cmd[0] == "/fake/colab"
        assert cmd[1] == "--auth=adc"
        assert cmd[2] == "--config"
        assert Path(cmd[3]).name == "sessions.json"
        assert Path(gac).parent == Path(cmd[3]).parent


def test_cli_account_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setenv("BLENDFLEET_ACCOUNTS_DIR", str(root))
    accounts.save_account_credentials(
        "studio1", refresh_token="1//r1", email="studio1@example.com", accounts_dir=root
    )

    rc = cli.main(["account", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["alias"] == "studio1"

    rc = cli.main(["account", "remove", "studio1"])
    assert rc == 0
    assert "Removed account 'studio1'" in capsys.readouterr().out
    assert not (root / "studio1").exists()

    rc_missing = cli.main(["account", "remove", "studio1"])
    assert rc_missing == 1
    assert os.environ.get("BLENDFLEET_ACCOUNTS_DIR") == str(root)
