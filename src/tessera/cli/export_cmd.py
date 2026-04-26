"""``tessera export`` and ``tessera import-vault`` — data portability.

Per ``docs/release-spec.md §v0.1 DoD`` the vault must be exportable in
three formats (JSON canonical, Markdown per-facet-type, SQLite plain
decrypted copy) with an ``--include-deleted`` flag. ``tessera
import-vault`` consumes a JSON export, completing the DoD's round-trip
requirement.

``import-vault`` is a separate subcommand rather than ``tessera export
--reverse`` because export and import are semantically distinct
operations with different safety envelopes: export is read-only on the
source vault; import writes and can fail a UNIQUE constraint.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from tessera.cli._common import (
    CliError,
    fail,
    open_vault,
    resolve_passphrase,
    resolve_vault_path,
)
from tessera.cli._ui import EMOJI, kv_panel, status, success
from tessera.vault.connection import VaultConnection
from tessera.vault.export import (
    ExportSummary,
    export_json,
    export_markdown,
    export_sqlite,
    import_json,
)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    export_parser = subparsers.add_parser("export", help="export the vault to a portable format")
    export_parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db",
    )
    export_parser.add_argument("--passphrase", default=None)
    export_parser.add_argument(
        "--format",
        choices=("json", "md", "sqlite"),
        required=True,
        help=(
            "json: single-file canonical export (byte-equivalent round-trip). "
            "md: one file per facet type written under --output. "
            "sqlite: plain-text decrypted copy of the vault."
        ),
    )
    export_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help=(
            "path to write to. json/sqlite produce a single file; md "
            "expects a directory and writes one .md per facet type."
        ),
    )
    export_parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="include soft-deleted facets (is_deleted=1). Default: omit them.",
    )
    export_parser.set_defaults(handler=_cmd_export)

    import_parser = subparsers.add_parser(
        "import-vault", help="import a JSON export into the vault"
    )
    import_parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db",
    )
    import_parser.add_argument("--passphrase", default=None)
    import_parser.add_argument("--input", type=Path, required=True, help="path to a JSON export")
    import_parser.add_argument(
        "--agent-external-id",
        default=None,
        help=(
            "re-home every imported facet onto this agent. If omitted, "
            "exported agents are matched by external_id (existing agents "
            "keep their rows; new ones are created)."
        ),
    )
    import_parser.set_defaults(handler=_cmd_import)


def _cmd_export(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    with (
        status(f"exporting vault to {args.format}", emoji=EMOJI["export"]),
        open_vault(vault_path, passphrase) as vault,
    ):
        try:
            summary = _dispatch_export(
                args.format, vault, args.output, include_deleted=args.include_deleted
            )
        except ValueError as exc:
            return fail(str(exc))
    _render_summary(summary, action_emoji=EMOJI["export"])
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    if not args.input.is_file():
        return fail(f"input not found: {args.input}")
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    with (
        status(f"importing from {args.input}", emoji=EMOJI["import"]),
        open_vault(vault_path, passphrase) as vault,
    ):
        try:
            summary = import_json(
                vault,
                document_path=args.input,
                agent_external_id=args.agent_external_id,
            )
        except (ValueError, KeyError) as exc:
            return fail(f"import failed: {exc}")
    _render_summary(summary, action_emoji=EMOJI["import"])
    return 0


def _dispatch_export(
    format_name: str,
    vault: VaultConnection,
    output_path: Path,
    *,
    include_deleted: bool,
) -> ExportSummary:
    """Route to the format-specific exporter and validate output-path shape."""

    if format_name == "json":
        if output_path.is_dir():
            raise ValueError(f"--format json requires a file path, got a directory: {output_path}")
        return export_json(
            vault,
            output_path=output_path,
            include_deleted=include_deleted,
            now_epoch=int(datetime.now(UTC).timestamp()),
        )
    if format_name == "md":
        if output_path.exists() and not output_path.is_dir():
            raise ValueError(f"--format md requires a directory path, got a file: {output_path}")
        return export_markdown(vault, output_dir=output_path, include_deleted=include_deleted)
    if format_name == "sqlite":
        if output_path.is_dir():
            raise ValueError(
                f"--format sqlite requires a file path, got a directory: {output_path}"
            )
        return export_sqlite(vault, output_path=output_path, include_deleted=include_deleted)
    raise ValueError(f"unknown format: {format_name!r}")


def _render_summary(summary: ExportSummary, *, action_emoji: str) -> None:
    by_type = ", ".join(f"{t}={c}" for t, c in sorted(summary.facets_by_type.items())) or "none"
    success(f"{summary.format} at {summary.output_path}", emoji=action_emoji)
    kv_panel(
        f"{summary.format} summary",
        {
            "agents": str(summary.agents),
            "facets": str(summary.facets),
            "by_type": by_type,
            "path": str(summary.output_path),
        },
        emoji=action_emoji,
    )
