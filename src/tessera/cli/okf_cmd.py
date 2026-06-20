"""``tessera okf validate <dir>`` — consumer-side OKF conformance check.

A read-only tool that checks a directory against OKF v0.1 conformance
(SPEC §9): every non-reserved ``.md`` carries parseable frontmatter with a
non-empty ``type``, and the reserved ``index.md`` / ``log.md`` files follow
§6/§7. It opens no vault and makes no network calls — useful for validating a
hand-authored bundle before importing or sharing it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from tessera.cli._common import fail
from tessera.cli._ui import EMOJI, error, success
from tessera.vault.okf import validate_bundle


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    okf_parser = subparsers.add_parser("okf", help="OKF interchange tooling")
    okf_sub = okf_parser.add_subparsers(dest="okf_command")
    validate = okf_sub.add_parser(
        "validate", help="check a directory for OKF v0.1 conformance (SPEC §9)"
    )
    validate.add_argument("bundle_dir", type=Path, help="path to an OKF bundle directory")
    validate.set_defaults(handler=_cmd_validate)
    okf_parser.set_defaults(handler=_print_okf_help(okf_parser))


def _print_okf_help(parser: argparse.ArgumentParser) -> Callable[[argparse.Namespace], int]:
    def _handler(_args: argparse.Namespace) -> int:
        parser.print_help()
        return 2

    return _handler


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate_bundle(args.bundle_dir)
    if report.conformant:
        success(
            f"OKF v0.1 conformant: {report.concept_count} concept(s) in {report.bundle_dir}",
            emoji=EMOJI["doctor"],
        )
        return 0
    for issue in report.issues:
        error(f"{issue.path}: {issue.message}")
    return fail(f"{len(report.issues)} OKF conformance issue(s) in {report.bundle_dir}")


__all__ = ["register"]
