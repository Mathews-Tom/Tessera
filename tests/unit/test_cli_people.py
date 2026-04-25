"""``tessera people`` parser + handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tessera.cli.__main__ import _build_parser
from tessera.migration import bootstrap
from tessera.vault import people as vault_people
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt


@pytest.fixture
def initialized_vault(tmp_path: Path, passphrase: bytearray) -> Path:
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


def _seed_person(
    vault_path: Path,
    passphrase: bytearray,
    *,
    canonical_name: str,
    aliases: list[str] | None = None,
) -> str:
    salt_bytes = (vault_path.parent / (vault_path.name + ".salt")).read_bytes()
    key = derive_key(passphrase, salt_bytes)
    with VaultConnection.open(vault_path, key) as vc:
        aid_row = vc.connection.execute(
            "SELECT id FROM agents WHERE external_id = '01A'"
        ).fetchone()
        external_id, _ = vault_people.insert(
            vc.connection,
            agent_id=int(aid_row[0]),
            canonical_name=canonical_name,
            aliases=aliases,
        )
    key.wipe()
    return external_id


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.unit
def test_people_list_routes_to_list_people(monkeypatch: pytest.MonkeyPatch) -> None:
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
    args = parser.parse_args(["people", "list", "--limit", "20"])
    assert args.handler(args) == 0
    assert seen["body"]["method"] == "list_people"
    assert seen["body"]["args"]["limit"] == 20


@pytest.mark.unit
def test_people_list_passes_since_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
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
    args = parser.parse_args(["people", "list", "--since", "1700000000"])
    assert args.handler(args) == 0
    assert seen["body"]["args"]["since"] == 1700000000


@pytest.mark.unit
def test_people_show_routes_to_resolve_person(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_post(
        url: str, *, headers: dict[str, str], json: Any, timeout: float
    ) -> _DummyResponse:
        del url, headers, timeout
        seen["body"] = json
        return _DummyResponse(200, {"ok": True, "result": {"matches": [], "is_exact": False}})

    monkeypatch.setattr("tessera.cli._http.httpx.post", _fake_post)
    monkeypatch.setenv("TESSERA_TOKEN", "t")
    parser = _build_parser()
    args = parser.parse_args(["people", "show", "Sarah"])
    assert args.handler(args) == 0
    assert seen["body"]["method"] == "resolve_person"
    assert seen["body"]["args"] == {"mention": "Sarah"}


@pytest.mark.unit
def test_people_subparser_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["people"])
    err = capsys.readouterr().err
    assert "people_command" in err or "required" in err


@pytest.mark.unit
def test_people_merge_collapses_two_rows(
    monkeypatch: pytest.MonkeyPatch,
    initialized_vault: Path,
    passphrase: bytearray,
) -> None:
    _seed_agent(initialized_vault, passphrase)
    primary = _seed_person(
        initialized_vault, passphrase, canonical_name="Sarah Johnson", aliases=["Sarah"]
    )
    secondary = _seed_person(
        initialized_vault, passphrase, canonical_name="S. Johnson", aliases=["SJ"]
    )

    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase.decode("utf-8"))
    parser = _build_parser()
    args = parser.parse_args(
        [
            "people",
            "merge",
            "--primary",
            primary,
            "--secondary",
            secondary,
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 0

    salt_bytes = (initialized_vault.parent / (initialized_vault.name + ".salt")).read_bytes()
    key = derive_key(passphrase, salt_bytes)
    with VaultConnection.open(initialized_vault, key) as vc:
        assert vault_people.get(vc.connection, secondary) is None
    key.wipe()


@pytest.mark.unit
def test_people_merge_surfaces_people_error_on_self_merge(
    monkeypatch: pytest.MonkeyPatch,
    initialized_vault: Path,
    passphrase: bytearray,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_agent(initialized_vault, passphrase)
    eid = _seed_person(initialized_vault, passphrase, canonical_name="Sarah")

    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase.decode("utf-8"))
    parser = _build_parser()
    args = parser.parse_args(
        [
            "people",
            "merge",
            "--primary",
            eid,
            "--secondary",
            eid,
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "into itself" in err


@pytest.mark.unit
def test_people_split_extracts_aliases(
    monkeypatch: pytest.MonkeyPatch,
    initialized_vault: Path,
    passphrase: bytearray,
) -> None:
    _seed_agent(initialized_vault, passphrase)
    eid = _seed_person(
        initialized_vault,
        passphrase,
        canonical_name="Sarah",
        aliases=["Sarah J", "Sarah Johnson"],
    )

    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase.decode("utf-8"))
    parser = _build_parser()
    args = parser.parse_args(
        [
            "people",
            "split",
            "--person",
            eid,
            "--canonical",
            "Sarah Johnson",
            "--aliases",
            "Sarah J",
            "--vault",
            str(initialized_vault),
        ]
    )
    rc = args.handler(args)
    assert rc == 0

    salt_bytes = (initialized_vault.parent / (initialized_vault.name + ".salt")).read_bytes()
    key = derive_key(passphrase, salt_bytes)
    with VaultConnection.open(initialized_vault, key) as vc:
        new_p = vault_people.get_by_canonical_name(
            vc.connection, agent_id=1, canonical_name="Sarah Johnson"
        )
        assert new_p is not None
        assert "Sarah J" in new_p.aliases
    key.wipe()
