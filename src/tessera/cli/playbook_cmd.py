"""``tessera playbook`` — compiler-orchestration CLI for compiled artifacts.

Per the V0.5 compiled-Playbooks plan §Phase 5, this command group is a
thin orchestration surface around the existing storage-only API in
``tessera.vault.compiled``. The boundary is non-negotiable: this CLI
does not compile. It enumerates targets, lists eligible source facets,
emits a deterministic Markdown scaffold, wraps
``register_compiled_artifact`` for the write path, and lists stale
artifacts with the audit-derived cause. Compilation itself happens in
whichever caller-side runner the user picks (Claude Code, a local LLM,
manual authoring) per ADR 0019 §Boundary statement — Tessera stores;
the caller compiles.

The five subcommands map one-for-one to the plan's "proposed commands"
list:

* ``tessera playbook targets`` — scan ``workflow``/``skill`` facets for
  well-formed compile target descriptors.
* ``tessera playbook sources <target>`` — list ``compile_into``-tagged
  source facets eligible to feed ``<target>``.
* ``tessera playbook scaffold <target> --out <path>`` — write a
  deterministic Markdown brief covering target, task, sources, and the
  required output sections so an external compiler has a stable
  starting point.
* ``tessera playbook register <target> --content <path>
  --compiler-version <version>`` — pair-write a registered artifact
  via :func:`tessera.vault.compiled.register_compiled_artifact`,
  defaulting source membership to the target's
  ``list_for_compilation`` enumeration.
* ``tessera playbook stale`` — surface stale artifacts plus the most
  recent ``compiled_artifact_marked_stale`` audit row's
  ``source_external_id`` + ``source_op`` so the user can trace the
  triggering mutation without a second query per row.

There is intentionally no ``tessera playbook compile`` until a
concrete external compiler integration design exists (plan §Phase 5
design constraints).
"""

from __future__ import annotations

import argparse
import contextlib
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import sqlcipher3

from tessera.cli._common import (
    CliError,
    fail,
    open_vault,
    resolve_agent_id,
    resolve_passphrase,
    resolve_vault_path,
)
from tessera.cli._ui import EMOJI, console, info, raw, report_table, success, warn
from tessera.vault import compiled as vault_compiled

_HELP_DESCRIPTION: Final[str] = (
    "Orchestrate compiled-artifact (Playbook) workflow.\n\n"
    "These commands wrap the storage-only API in tessera.vault.compiled.\n"
    "They do not call an LLM; the caller compiles with whatever runner\n"
    "they choose and registers the result through `playbook register`.\n\n"
    "Subcommands: targets | sources | scaffold | register | stale"
)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``tessera playbook`` command tree on ``subparsers``."""

    parser = subparsers.add_parser(
        "playbook",
        help="orchestrate compiled-artifact (Playbook) workflow",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="playbook_command")

    targets_p = sub.add_parser(
        "targets",
        help="list compile target descriptors (workflow/skill facets carrying the contract)",
    )
    _add_vault_args(targets_p)
    targets_p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the table layout",
    )
    targets_p.set_defaults(handler=_cmd_targets)

    sources_p = sub.add_parser(
        "sources",
        help="list source facets tagged metadata.compile_into = [target]",
    )
    sources_p.add_argument("target", help="compile target identifier (matches compile_into entry)")
    _add_vault_args(sources_p)
    sources_p.add_argument(
        "--limit",
        type=int,
        default=64,
        help="maximum sources returned (default: 64; matches list_for_compilation)",
    )
    sources_p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the table layout",
    )
    sources_p.set_defaults(handler=_cmd_sources)

    scaffold_p = sub.add_parser(
        "scaffold",
        help="write a deterministic Markdown compile brief for the target",
    )
    scaffold_p.add_argument("target", help="compile target identifier")
    scaffold_p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="path to write the scaffold Markdown",
    )
    scaffold_p.add_argument(
        "--force",
        action="store_true",
        help="overwrite --out if it already exists (default: refuse)",
    )
    _add_vault_args(scaffold_p)
    scaffold_p.set_defaults(handler=_cmd_scaffold)

    register_p = sub.add_parser(
        "register",
        help="pair-write a compiled artifact through register_compiled_artifact",
    )
    register_p.add_argument("target", help="compile target identifier")
    register_p.add_argument(
        "--content",
        type=Path,
        required=True,
        help="path to the compiled Markdown artifact body",
    )
    register_p.add_argument(
        "--compiler-version",
        required=True,
        help="compiler runner identifier; recommended shape: <runner>/<recipe>@<version>",
    )
    register_p.add_argument(
        "--source-id",
        action="append",
        default=None,
        help=(
            "explicit source facet external_id; repeat to claim multiple sources; "
            "default: enumerate via list_for_compilation(target)"
        ),
    )
    register_p.add_argument(
        "--source-tool",
        default="cli",
        help="source_tool tag persisted on the paired compiled_notebook facet (default: cli)",
    )
    register_p.add_argument(
        "--artifact-type",
        default=None,
        help=(
            "override artifact_type written to compiled_artifacts; "
            "default: read from the target descriptor (else 'playbook')"
        ),
    )
    _add_vault_args(register_p)
    register_p.set_defaults(handler=_cmd_register)

    stale_p = sub.add_parser(
        "stale",
        help="list compiled artifacts with is_stale=1 and the cascade cause",
    )
    _add_vault_args(stale_p)
    stale_p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum stale artifacts returned (default: 100)",
    )
    stale_p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the table layout",
    )
    stale_p.set_defaults(handler=_cmd_stale)

    parser.set_defaults(handler=_print_help_when_no_subcommand(parser))


def _print_help_when_no_subcommand(
    parser: argparse.ArgumentParser,
) -> Callable[[argparse.Namespace], int]:
    def _handler(_args: argparse.Namespace) -> int:
        parser.print_help()
        return 2

    return _handler


def _add_vault_args(parser: argparse.ArgumentParser) -> None:
    """Attach the standard ``--vault``/``--passphrase``/``--agent-id`` triple.

    The same surface every direct-vault subcommand uses (see
    ``tessera.cli.skills_cmd`` and ``tessera.cli.audit_cmd``); kept
    here so each subparser exposes the flags consistently.
    """

    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db",
    )
    parser.add_argument(
        "--passphrase",
        default=None,
        help="vault passphrase; falls back to $TESSERA_PASSPHRASE",
    )
    parser.add_argument(
        "--agent-id",
        type=int,
        default=None,
        help="agent id; auto-selected when the vault has exactly one agent",
    )


def _cmd_targets(args: argparse.Namespace) -> int:
    try:
        with _vault_session(args) as (conn, agent_id):
            descriptors = vault_compiled.list_targets(conn, agent_id=agent_id)
    except CliError as exc:
        return fail(str(exc))
    if args.json or not console.is_terminal:
        raw(_targets_to_json(descriptors))
        return 0
    if not descriptors:
        info("no compile target descriptors found", emoji=EMOJI["recall"])
        info(
            "tag a workflow or skill facet with target/task/artifact_type/quality_bar metadata",
            emoji=EMOJI["info"],
        )
        return 0
    table = report_table(
        "compile targets",
        ["target", "artifact_type", "task", "quality_bar", "descriptor"],
        emoji=EMOJI["recall"],
    )
    for desc in descriptors:
        table.add_row(
            desc.target,
            desc.artifact_type,
            _truncate(desc.task, 60),
            _truncate(desc.quality_bar, 60),
            f"{desc.descriptor_facet_type}:{desc.descriptor_external_id}",
        )
    console.print(table)
    return 0


def _cmd_sources(args: argparse.Namespace) -> int:
    try:
        with _vault_session(args) as (conn, agent_id):
            sources = vault_compiled.list_for_compilation(
                conn, agent_id=agent_id, target=args.target, limit=args.limit
            )
    except CliError as exc:
        return fail(str(exc))
    if args.json or not console.is_terminal:
        raw(_sources_to_json(args.target, sources))
        return 0
    if not sources:
        info(
            f"no sources tagged compile_into=[{args.target!r}]",
            emoji=EMOJI["recall"],
        )
        return 0
    table = report_table(
        f"sources for target {args.target!r}",
        ["external_id", "facet_type", "captured_at", "compile_role", "snippet"],
        emoji=EMOJI["recall"],
    )
    for src in sources:
        compile_role = src.metadata.get("compile_role", "")
        table.add_row(
            src.external_id,
            src.facet_type,
            _format_epoch(src.captured_at),
            str(compile_role) if isinstance(compile_role, str) else "",
            _truncate(src.content, 60),
        )
    console.print(table)
    return 0


def _cmd_scaffold(args: argparse.Namespace) -> int:
    out_path: Path = args.out
    if out_path.exists() and not args.force:
        return fail(f"refusing to overwrite {out_path}; pass --force to replace")
    try:
        with _vault_session(args) as (conn, agent_id):
            descriptor = vault_compiled.get_target(conn, agent_id=agent_id, target=args.target)
            sources = vault_compiled.list_for_compilation(
                conn, agent_id=agent_id, target=args.target
            )
    except CliError as exc:
        return fail(str(exc))
    if descriptor is None and not sources:
        return fail(
            f"target {args.target!r} has no descriptor and no compile_into sources; "
            "create a workflow/skill descriptor first"
        )
    body = _render_scaffold(target=args.target, descriptor=descriptor, sources=sources)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    success(
        f"wrote scaffold for {args.target!r} → {out_path} ({len(sources)} source(s))",
        emoji=EMOJI["repair"],
    )
    if descriptor is None:
        warn(
            "no target descriptor found; scaffold uses placeholder task/quality_bar lines",
            emoji=EMOJI["warn"],
        )
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    content_path: Path = args.content
    try:
        content = content_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fail(f"content file not found: {content_path}")
    except OSError as exc:
        return fail(f"failed to read {content_path}: {exc}")
    if not content.strip():
        return fail(f"content file {content_path} is empty")
    try:
        with _vault_session(args) as (conn, agent_id):
            descriptor = vault_compiled.get_target(conn, agent_id=agent_id, target=args.target)
            source_ids = _resolve_source_ids(
                conn,
                agent_id=agent_id,
                target=args.target,
                explicit=args.source_id,
            )
            artifact_type = _resolve_artifact_type(
                explicit=args.artifact_type, descriptor=descriptor
            )
            external_id = vault_compiled.register_compiled_artifact(
                conn,
                agent_id=agent_id,
                content=content,
                source_facets=source_ids,
                artifact_type=artifact_type,
                compiler_version=args.compiler_version,
                source_tool=args.source_tool,
            )
            conn.commit()
    except CliError as exc:
        return fail(str(exc))
    except vault_compiled.InvalidCompiledArtifactError as exc:
        return fail(str(exc))
    except vault_compiled.DuplicateCompiledArtifactError as exc:
        return fail(str(exc))
    success(
        f"registered compiled artifact {external_id} for target {args.target!r} "
        f"(artifact_type={artifact_type}, sources={len(source_ids)}, "
        f"compiler_version={args.compiler_version})",
        emoji=EMOJI["repair"],
    )
    return 0


def _cmd_stale(args: argparse.Namespace) -> int:
    try:
        with _vault_session(args) as (conn, agent_id):
            records = vault_compiled.list_stale_artifacts(conn, agent_id=agent_id, limit=args.limit)
    except CliError as exc:
        return fail(str(exc))
    if args.json or not console.is_terminal:
        raw(_stale_to_json(records))
        return 0
    if not records:
        info("no stale compiled artifacts", emoji=EMOJI["ok"])
        return 0
    table = report_table(
        "stale compiled artifacts",
        ["external_id", "artifact_type", "compiled_at", "last_source_op", "last_source_id"],
        emoji=EMOJI["warn"],
    )
    for record in records:
        table.add_row(
            record.artifact.external_id,
            record.artifact.artifact_type,
            _format_epoch(record.artifact.compiled_at),
            record.last_source_op or "",
            record.last_source_external_id or "",
        )
    console.print(table)
    return 0


@contextlib.contextmanager
def _vault_session(
    args: argparse.Namespace,
) -> Iterator[tuple[sqlcipher3.Connection, int]]:
    """Open the vault and resolve the agent id for one CLI call.

    Yields ``(conn, agent_id)``. The context manager owns vault
    unlock and close; raises :class:`CliError` on resolution failures
    so the caller can return a uniform exit code through :func:`fail`.
    """

    vault_path = resolve_vault_path(args.vault)
    passphrase = resolve_passphrase(args.passphrase)
    with open_vault(vault_path, passphrase) as vc:
        agent_id = resolve_agent_id(vc.connection, args.agent_id)
        yield vc.connection, agent_id


def _resolve_source_ids(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    target: str,
    explicit: list[str] | None,
) -> list[str]:
    """Pick the source-facet list for a register call.

    When ``explicit`` is provided, trust the caller verbatim — the
    ``register_compiled_artifact`` write path validates each ULID
    against the agent's live facets, so a typo or cross-agent ULID
    surfaces as an :class:`InvalidCompiledArtifactError` rather than
    silently shrinking the list. When ``explicit`` is absent,
    enumerate via :func:`list_for_compilation` so the default register
    path uses exactly the rows the user tagged with
    ``metadata.compile_into = [target]``.
    """

    if explicit:
        return list(explicit)
    sources = vault_compiled.list_for_compilation(conn, agent_id=agent_id, target=target)
    if not sources:
        raise CliError(
            f"target {target!r} has no compile_into sources; "
            "tag at least one source facet's metadata.compile_into=[target] "
            "or pass --source-id explicitly"
        )
    return [s.external_id for s in sources]


def _resolve_artifact_type(
    *,
    explicit: str | None,
    descriptor: vault_compiled.CompileTarget | None,
) -> str:
    """Pick the artifact_type for a register call.

    Order of resolution: ``--artifact-type`` flag, descriptor's
    ``artifact_type`` field, the ``register_compiled_artifact``
    default of ``playbook``. The default in the storage layer is the
    same string; we resolve it here so the success line can echo the
    chosen value even when the caller did not pass the flag.
    """

    if explicit is not None:
        return explicit
    if descriptor is not None:
        return descriptor.artifact_type
    return "playbook"


def _render_scaffold(
    *,
    target: str,
    descriptor: vault_compiled.CompileTarget | None,
    sources: list[vault_compiled.CompileSource],
) -> str:
    """Build the deterministic compile brief for a target.

    Output is plain Markdown with stable section headings so an
    external compiler (Claude Code, a local LLM, manual authoring)
    has a consistent contract. Required output sections come from
    plan §Phase 5 / §Phase 8: purpose, supported tasks, source
    inventory, synthesized operating model, known gaps, eval summary,
    provenance notes.
    """

    task = descriptor.task if descriptor is not None else "TODO: describe the recurring task"
    artifact_type = descriptor.artifact_type if descriptor is not None else "playbook"
    quality_bar = (
        descriptor.quality_bar
        if descriptor is not None
        else "TODO: describe the caller's acceptance criterion"
    )
    expected_refresh = descriptor.expected_refresh if descriptor is not None else None
    descriptor_ref = (
        f"{descriptor.descriptor_facet_type}:{descriptor.descriptor_external_id}"
        if descriptor is not None
        else "missing"
    )

    lines: list[str] = [
        f"# Compile brief: {target}",
        "",
        "## Target",
        "",
        f"- target: `{target}`",
        f"- artifact_type: `{artifact_type}`",
        f"- descriptor: `{descriptor_ref}`",
        f"- expected_refresh: `{expected_refresh}`"
        if expected_refresh
        else "- expected_refresh: not set",
        "",
        "## Task",
        "",
        task,
        "",
        "## Quality bar",
        "",
        quality_bar,
        "",
        "## Source facets",
        "",
    ]
    if not sources:
        lines.extend(
            [
                '_No sources are tagged `metadata.compile_into = ["' + target + '"]` yet._',
                "",
            ]
        )
    else:
        lines.append("| external_id | facet_type | compile_role | compile_priority | captured_at |")
        lines.append("|---|---|---|---:|---|")
        for src in sources:
            role = src.metadata.get("compile_role", "")
            priority = src.metadata.get("compile_priority", "")
            lines.append(
                "| `"
                + src.external_id
                + "` | "
                + src.facet_type
                + " | "
                + (str(role) if isinstance(role, str) else "")
                + " | "
                + (str(priority) if isinstance(priority, int | str) else "")
                + " | "
                + _format_epoch(src.captured_at)
                + " |"
            )
        lines.append("")
    lines.extend(
        [
            "## Required output sections",
            "",
            "Compile the artifact body so the registered Markdown answers each section. ",
            "The plan §Phase 8 minimum-sections list is binding for serious Playbooks:",
            "",
            "1. Purpose — what task the artifact accelerates and why a Playbook is the right shape.",
            "2. Supported tasks — concrete recurring questions the artifact must answer well.",
            "3. Source inventory — list every facet ULID the compile read; cross-check against the source-facet table above.",
            "4. Synthesized operating model — the Playbook content itself.",
            "5. Known gaps — what the sources do not cover; surface honest limits instead of inventing detail.",
            "6. Eval summary — the eval-set pass/fail counts; record `must` failures verbatim.",
            "7. Provenance notes — claim-to-source backing for the highest-stakes statements.",
            "",
            "## Provenance expectations",
            "",
            "- Every claim that affects a release decision, security posture, or audit answer should cite at least one source facet by external_id.",
            "- Optional `field_provenance` metadata on the registered artifact should reuse the source-facet ULIDs above (a subset of `compiled_artifacts.source_facets`).",
            "- `source_refs` for repo-local files use the same compact path/section/symbol convention as source metadata; do not paste large quotes into metadata.",
            "",
            "## Eval questions",
            "",
            "Eval entries are caller-owned per the V0.5 plan §Phase 2. Add representative questions, expected_claims, optional required_source_refs, and a severity (`must`/`should`/`exploratory`). Tessera does not execute evals; record the pass/fail summary in the registered artifact's metadata so future readers can audit the run.",
            "",
            "---",
            "",
            f"_Generated by `tessera playbook scaffold` at {_now_iso()}._",
            "",
        ]
    )
    return "\n".join(lines)


def _targets_to_json(descriptors: Iterable[vault_compiled.CompileTarget]) -> str:
    payload = [
        {
            "target": d.target,
            "task": d.task,
            "artifact_type": d.artifact_type,
            "quality_bar": d.quality_bar,
            "expected_refresh": d.expected_refresh,
            "descriptor_external_id": d.descriptor_external_id,
            "descriptor_facet_type": d.descriptor_facet_type,
        }
        for d in descriptors
    ]
    return json.dumps(payload, indent=2, sort_keys=True)


def _sources_to_json(target: str, sources: Iterable[vault_compiled.CompileSource]) -> str:
    payload = {
        "target": target,
        "sources": [
            {
                "external_id": s.external_id,
                "facet_type": s.facet_type,
                "captured_at": s.captured_at,
                "metadata": s.metadata,
                "snippet": s.content[:200],
            }
            for s in sources
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _stale_to_json(records: Iterable[vault_compiled.StaleArtifactRecord]) -> str:
    payload = [
        {
            "external_id": r.artifact.external_id,
            "artifact_type": r.artifact.artifact_type,
            "compiled_at": r.artifact.compiled_at,
            "compiler_version": r.artifact.compiler_version,
            "source_facets": list(r.artifact.source_facets),
            "last_source_external_id": r.last_source_external_id,
            "last_source_op": r.last_source_op,
            "last_marked_at": r.last_marked_at,
        }
        for r in records
    ]
    return json.dumps(payload, indent=2, sort_keys=True)


def _format_epoch(value: int) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: max(0, width - 1)] + "…"


__all__ = ["register"]
