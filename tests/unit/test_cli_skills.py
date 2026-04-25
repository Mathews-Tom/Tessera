"""``tessera skills`` parser + handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tessera.cli.__main__ import _build_parser
from tessera.migration import bootstrap
from tessera.vault import skills as vault_skills
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt


@pytest.fixture
def initialized_vault(tmp_path: Path, passphrase: bytearray) -> Path:
    """Bootstrap a vault with a persisted salt sidecar.

    The shared ``open_vault`` fixture keeps the salt in-memory and
    skips ``save_salt``; the CLI's :func:`tessera.cli._common.open_vault`
    calls :func:`load_salt`, so direct-vault subcommands need the
    sidecar present. This fixture re-bootstraps with that on disk.
    """

    vault_path = tmp_path / "vault.db"
    salt = new_salt()
    save_salt(vault_path, salt)
    key = derive_key(passphrase, salt)
    bootstrap(vault_path, key)
    key.wipe()
    return vault_path


def _seed_agent(vault_path: Path, passphrase: bytearray) -> int:
    salt_bytes = (vault_path.parent / (vault_path.name + ".salt")).read_bytes()
    key = derive_key(passphrase, salt_bytes)
    with VaultConnection.open(vault_path, key) as vc:
        cur = vc.connection.execute(
            "INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'tom', 1)"
        )
        agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    key.wipe()
    return agent_id


def _seed_skill(
    vault_path: Path,
    passphrase: bytearray,
    *,
    name: str,
    procedure_md: str,
) -> None:
    salt_bytes = (vault_path.parent / (vault_path.name + ".salt")).read_bytes()
    key = derive_key(passphrase, salt_bytes)
    with VaultConnection.open(vault_path, key) as vc:
        aid_row = vc.connection.execute(
            "SELECT id FROM agents WHERE external_id = '01A'"
        ).fetchone()
        vault_skills.create_skill(
            vc.connection,
            agent_id=int(aid_row[0]),
            name=name,
            description="d",
            procedure_md=procedure_md,
            source_tool="seed",
        )
    key.wipe()


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.unit
def test_skills_list_calls_list_skills_with_active_only_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _fake_post(
        url: str, *, headers: dict[str, str], json: Any, timeout: float
    ) -> _DummyResponse:
        del url, headers, timeout
        seen["body"] = json
        return _DummyResponse(
            200, {"ok": True, "result": {"items": [], "truncated": False, "total_tokens": 0}}
        )

    monkeypatch.setattr("tessera.cli._http.httpx.post", _fake_post)
    monkeypatch.setenv("TESSERA_TOKEN", "tessera_session_AAAAAAAAAAAAAAAAAAAAAAAA")
    parser = _build_parser()
    args = parser.parse_args(["skills", "list"])
    assert args.handler(args) == 0
    assert seen["body"]["method"] == "list_skills"
    assert seen["body"]["args"]["active_only"] is True


@pytest.mark.unit
def test_skills_list_all_flag_inverts_active_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_post(
        url: str, *, headers: dict[str, str], json: Any, timeout: float
    ) -> _DummyResponse:
        del url, headers, timeout
        seen["body"] = json
        return _DummyResponse(
            200, {"ok": True, "result": {"items": [], "truncated": False, "total_tokens": 0}}
        )

    monkeypatch.setattr("tessera.cli._http.httpx.post", _fake_post)
    monkeypatch.setenv("TESSERA_TOKEN", "t")
    parser = _build_parser()
    args = parser.parse_args(["skills", "list", "--all"])
    assert args.handler(args) == 0
    assert seen["body"]["args"]["active_only"] is False


@pytest.mark.unit
def test_skills_show_invokes_get_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_post(
        url: str, *, headers: dict[str, str], json: Any, timeout: float
    ) -> _DummyResponse:
        del url, headers, timeout
        seen["body"] = json
        return _DummyResponse(200, {"ok": True, "result": {"skill": None}})

    monkeypatch.setattr("tessera.cli._http.httpx.post", _fake_post)
    monkeypatch.setenv("TESSERA_TOKEN", "t")
    parser = _build_parser()
    args = parser.parse_args(["skills", "show", "git-rebase"])
    assert args.handler(args) == 0
    assert seen["body"]["method"] == "get_skill"
    assert seen["body"]["args"] == {"name": "git-rebase"}


@pytest.mark.unit
def test_skills_subparser_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["skills"])
    err = capsys.readouterr().err
    assert "skills_command" in err or "required" in err


@pytest.mark.unit
def test_skills_sync_to_disk_writes_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialized_vault: Path,
    passphrase: bytearray,
) -> None:
    """sync-to-disk opens the vault directly and writes one .md per skill."""

    _seed_agent(initialized_vault, passphrase)
    _seed_skill(initialized_vault, passphrase, name="git rebase", procedure_md="alpha")

    out_dir = tmp_path / "skills-out"
    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase.decode("utf-8"))
    parser = _build_parser()
    args = parser.parse_args(
        [
            "skills",
            "sync-to-disk",
            str(out_dir),
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    assert (out_dir / "git-rebase.md").read_text(encoding="utf-8") == "alpha"


@pytest.mark.unit
def test_skills_sync_from_disk_imports_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialized_vault: Path,
    passphrase: bytearray,
) -> None:
    _seed_agent(initialized_vault, passphrase)

    in_dir = tmp_path / "skills-in"
    in_dir.mkdir()
    (in_dir / "git-rebase.md").write_text("rebase procedure", encoding="utf-8")

    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase.decode("utf-8"))
    parser = _build_parser()
    args = parser.parse_args(
        [
            "skills",
            "sync-from-disk",
            str(in_dir),
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 0


@pytest.mark.unit
def test_skills_sync_to_disk_requires_passphrase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialized_vault: Path,
) -> None:
    """No --passphrase and no $TESSERA_PASSPHRASE → exit 1, no traceback."""

    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    parser = _build_parser()
    args = parser.parse_args(
        [
            "skills",
            "sync-to-disk",
            str(tmp_path / "out"),
            "--vault",
            str(initialized_vault),
        ]
    )
    assert args.handler(args) == 1
