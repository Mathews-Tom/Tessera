"""Stub CLI for ``tessera vault [repair-embeds]``.

The full vault CLI is P9; this module is the slice P3 needs to reset
``failed`` facets so the embed worker picks them up again. Invocation:
``python -m tessera.cli.vault repair-embeds --vault PATH --passphrase ...``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import sqlcipher3

from tessera.cli._ui import EMOJI, error, status, success
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, load_salt
from tessera.vault.facets import V0_1_FACET_TYPES


def run(argv: list[str] | None = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    if args.command == "repair-embeds":
        return _cmd_repair_embeds(args)
    parser.print_help()
    return 2


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tessera vault")
    sub = parser.add_subparsers(dest="command", required=True)

    repair = sub.add_parser(
        "repair-embeds",
        help="Reset facets in 'failed' embed state so the next worker pass retries them.",
    )
    repair.add_argument("--vault", type=Path, required=True)
    repair.add_argument("--passphrase", required=True)
    repair.add_argument(
        "--facet-type",
        choices=sorted(V0_1_FACET_TYPES),
        help="Limit the reset to a single facet type (default: all).",
    )
    return parser


def _cmd_repair_embeds(args: argparse.Namespace) -> int:
    passphrase = args.passphrase.encode("utf-8")
    try:
        salt = load_salt(args.vault)
    except FileNotFoundError:
        error(f"no KDF salt sidecar for {args.vault}; initialise the vault first")
        return 1
    with (
        status("resetting failed embeds", emoji=EMOJI["repair"]),
        derive_key(passphrase, salt) as key,
        VaultConnection.open(args.vault, key) as vc,
    ):
        updated = repair_embeds(vc.connection, facet_type=args.facet_type)
    success(f"reset {updated} facet(s) from 'failed' to 'pending'", emoji=EMOJI["repair"])
    return 0


def repair_embeds(conn: sqlcipher3.Connection, *, facet_type: str | None = None) -> int:
    """Flip ``embed_status`` from ``failed`` to ``pending``.

    Clears ``embed_attempts``, ``embed_last_error``, and
    ``embed_last_attempt_at`` so the next :func:`~tessera.retrieval.embed_worker.run_pass`
    sees the facet as a fresh candidate. Returns the number of rows updated.
    """

    if facet_type is None:
        cur = conn.execute(
            """
            UPDATE facets
            SET embed_status = 'pending',
                embed_attempts = 0,
                embed_last_error = NULL,
                embed_last_attempt_at = NULL
            WHERE embed_status = 'failed' AND is_deleted = 0
            """
        )
    else:
        if facet_type not in V0_1_FACET_TYPES:
            raise ValueError(f"unsupported facet_type: {facet_type!r}")
        cur = conn.execute(
            """
            UPDATE facets
            SET embed_status = 'pending',
                embed_attempts = 0,
                embed_last_error = NULL,
                embed_last_attempt_at = NULL
            WHERE embed_status = 'failed' AND is_deleted = 0 AND facet_type = ?
            """,
            (facet_type,),
        )
    return int(cur.rowcount)


if __name__ == "__main__":
    raise SystemExit(run())
