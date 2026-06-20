"""OKF conformance validator + the committed sample bundle (plan Phase 6)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tessera.cli.__main__ import _build_parser
from tessera.migration import bootstrap
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt
from tessera.vault.okf import OKF_VERSION, TESSERA_EXTERNAL_ID, parse_concept, validate_bundle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SAMPLE = _REPO_ROOT / "docs" / "okf-sample-bundle"
_RESERVED = {"index.md", "log.md"}
_LINK_RE = re.compile(r"\]\((/[^)]+\.md)\)")


# --------------------------------------------------------------------------
# The committed sample bundle is a living conformant example.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_sample_bundle_passes_validator() -> None:
    report = validate_bundle(_SAMPLE)
    assert report.conformant, [(issue.path, issue.message) for issue in report.issues]
    assert report.concept_count == 3


@pytest.mark.integration
def test_sample_bundle_passes_phase2_conformance_assertions() -> None:
    # Mirror the Stack B Phase-2 export conformance assertions (SPEC §9):
    # parseable frontmatter + non-empty type + tessera_external_id on every
    # non-reserved concept, root index.md declares okf_version, and the
    # compiled-notebook citations resolve to real concept files.
    root = parse_concept((_SAMPLE / "index.md").read_text(encoding="utf-8"))
    assert root.frontmatter["okf_version"] == OKF_VERSION

    concepts = [path for path in _SAMPLE.rglob("*.md") if path.name not in _RESERVED]
    assert concepts
    for path in concepts:
        parsed = parse_concept(path.read_text(encoding="utf-8"))
        assert parsed.frontmatter["type"]
        assert parsed.frontmatter[TESSERA_EXTERNAL_ID]

    compiled = (_SAMPLE / "compiled_notebook" / "swcr-brief.md").read_text(encoding="utf-8")
    links = _LINK_RE.findall(compiled)
    assert links
    for link in links:
        assert (_SAMPLE / link.lstrip("/")).is_file()


# --------------------------------------------------------------------------
# The docs/api.md recipes run as written against the sample bundle.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_cli_validate_recipe_runs() -> None:
    parser = _build_parser()
    args = parser.parse_args(["okf", "validate", str(_SAMPLE)])
    assert args.handler(args) == 0


@pytest.mark.integration
def test_cli_import_recipe_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_path = tmp_path / "vault.db"
    passphrase = "okf sample"
    _seed_vault(vault_path, passphrase)
    monkeypatch.setenv("TESSERA_PASSPHRASE", passphrase)
    parser = _build_parser()
    args = parser.parse_args(["import-okf", "--vault", str(vault_path), "--input", str(_SAMPLE)])
    assert args.handler(args) == 0


# --------------------------------------------------------------------------
# Validator negative cases.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_validator_flags_missing_type(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "preference").mkdir(parents=True)
    (bundle / "preference" / "notype.md").write_text(
        '---\ntitle: "x"\n---\n\nbody\n', encoding="utf-8"
    )
    report = validate_bundle(bundle)
    assert not report.conformant
    assert any("type" in issue.message for issue in report.issues)


@pytest.mark.integration
def test_validator_flags_nonroot_index_frontmatter(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "preference").mkdir(parents=True)
    (bundle / "preference" / "index.md").write_text(
        '---\nokf_version: "0.1"\n---\n\n# Preference\n', encoding="utf-8"
    )
    report = validate_bundle(bundle)
    assert any("index.md" in issue.message for issue in report.issues)


@pytest.mark.integration
def test_validator_flags_malformed_frontmatter(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "preference").mkdir(parents=True)
    (bundle / "preference" / "bad.md").write_text(
        "---\ntype: Preference\n\nno closing fence\n", encoding="utf-8"
    )
    report = validate_bundle(bundle)
    assert any("malformed" in issue.message for issue in report.issues)


def _seed_vault(vault_path: Path, passphrase: str) -> None:
    salt = new_salt()
    save_salt(vault_path, salt)
    with derive_key(bytearray(passphrase.encode("utf-8")), salt) as key:
        bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) "
                "VALUES ('01SAMPLEAGENT', 'sample', 1_000_000)"
            )
