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
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

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
    "Subcommands: targets | sources | scaffold | register | stale | inspect"
)

_DEFAULT_SNIPPET_CHARS: Final[int] = 400
_ULID_LENGTH: Final[int] = 26
_ULID_ALPHABET: Final[frozenset[str]] = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


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

    inspect_p = sub.add_parser(
        "inspect",
        help="read a single artifact, optionally narrowed to one or more fields",
        description=(
            "Look up a compiled artifact by target name or ULID and emit a "
            "bounded view of its content and provenance.\n\n"
            "Target lookup picks the most recent fresh artifact whose "
            "source_facets are a non-empty subset of "
            "list_compile_sources(target). ULID lookup resolves directly and "
            "may return a stale artifact unless --require-fresh is set.\n\n"
            "Field selectors match a Markdown heading (## or ### Name, "
            "case-insensitive) inside the artifact body or a key under "
            "metadata.field_provenance. Pass --field repeatedly for multiple "
            "fields. Without --field, the command emits the full artifact "
            "summary."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    inspect_p.add_argument(
        "target_or_ulid",
        help=(
            "compile target identifier (resolves to the latest fresh artifact) "
            "or a compiled-artifact ULID"
        ),
    )
    inspect_p.add_argument(
        "--field",
        dest="fields",
        action="append",
        default=None,
        help=(
            "field name; matches a Markdown heading or metadata.field_provenance key. "
            "Pass --field repeatedly to request multiple fields."
        ),
    )
    inspect_p.add_argument(
        "--provenance",
        action="store_true",
        help=(
            "include metadata.field_provenance entries for the requested fields "
            "(or for the artifact-level summary when no --field is given)"
        ),
    )
    inspect_p.add_argument(
        "--require-fresh",
        action="store_true",
        help="fail loudly when the resolved artifact carries is_stale=1",
    )
    inspect_p.add_argument(
        "--max-snippet",
        type=int,
        default=_DEFAULT_SNIPPET_CHARS,
        help=(
            "snippet character cap per field/section (default: "
            f"{_DEFAULT_SNIPPET_CHARS}; pass 0 for the full content)"
        ),
    )
    inspect_p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the formatted layout",
    )
    _add_vault_args(inspect_p)
    inspect_p.set_defaults(handler=_cmd_inspect)

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


def _cmd_inspect(args: argparse.Namespace) -> int:
    try:
        requested_fields = _normalize_fields(args.fields)
    except CliError as exc:
        return fail(str(exc))
    max_snippet = args.max_snippet
    if max_snippet < 0:
        return fail("--max-snippet must be >= 0 (0 means no truncation)")
    try:
        with _vault_session(args) as (conn, agent_id):
            artifact, resolved_target = _resolve_artifact(
                conn,
                agent_id=agent_id,
                target_or_ulid=args.target_or_ulid,
                require_fresh=args.require_fresh,
            )
            stale_cause = (
                _lookup_stale_cause(conn, agent_id=agent_id, external_id=artifact.external_id)
                if artifact.is_stale
                else None
            )
    except CliError as exc:
        return fail(str(exc))
    sections = _extract_sections(artifact.content)
    field_provenance = _extract_field_provenance(artifact.metadata)
    if requested_fields:
        try:
            field_views = _build_field_views(
                requested_fields,
                sections=sections,
                field_provenance=field_provenance,
                include_provenance=args.provenance,
                max_snippet=max_snippet,
            )
        except CliError as exc:
            return fail(str(exc))
    else:
        field_views = []
    artifact_view = _ArtifactView(
        artifact=artifact,
        resolved_target=resolved_target,
        stale_cause=stale_cause,
        field_views=field_views,
        include_artifact_provenance=args.provenance and not requested_fields,
        artifact_provenance=field_provenance,
        max_snippet=max_snippet,
    )
    if args.json or not console.is_terminal:
        # Bypass Rich for JSON output: artifact bodies routinely exceed
        # the console's 80-column wrap budget, and Rich's word-wrap turns
        # one valid JSON document into several lines with embedded raw
        # newlines that ``json.loads`` rejects.
        sys.stdout.write(_inspect_to_json(artifact_view, query=args.target_or_ulid))
        sys.stdout.write("\n")
        return 0
    _render_inspect_table(artifact_view, query=args.target_or_ulid)
    if artifact.is_stale:
        warn(
            "artifact is stale; treat content as non-authoritative",
            emoji=EMOJI["warn"],
        )
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


# ---- inspect helpers ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FieldView:
    """One requested field's resolved view.

    Either the field matched a Markdown heading in the artifact body
    (``section_heading`` set, ``snippet`` populated) or only its
    ``field_provenance`` entry exists (``snippet`` is ``None``). The
    ``provenance`` slot is set when ``--provenance`` was requested and a
    matching ``metadata.field_provenance.<name>`` entry exists.
    """

    name: str
    section_heading: str | None
    snippet: str | None
    snippet_truncated: bool
    provenance: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _ArtifactView:
    """Aggregated inspect result feeding the JSON / TTY renderers."""

    artifact: vault_compiled.CompiledArtifact
    resolved_target: str | None
    stale_cause: tuple[str | None, str | None] | None
    field_views: list[_FieldView]
    include_artifact_provenance: bool
    artifact_provenance: dict[str, dict[str, Any]]
    max_snippet: int


def _normalize_fields(raw_fields: list[str] | None) -> list[str]:
    """Strip empties and preserve order while deduplicating field names.

    ``argparse`` collects ``--field foo --field bar`` into a list. The
    inspect contract treats whitespace-only entries as user errors but
    silently drops the duplicate of an already-requested name so
    ``--field retrieval --field retrieval`` does not double-render.
    """

    if not raw_fields:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw_fields:
        cleaned = entry.strip()
        if not cleaned:
            raise CliError("--field value must be non-empty")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _looks_like_ulid(value: str) -> bool:
    """True when ``value`` is shaped like a Crockford-base32 ULID.

    Used as the disambiguation switch in :func:`_resolve_artifact`.
    Matches the ULID written by :class:`ulid.ULID` (uppercase, 26 chars,
    no padding). A close-but-wrong string falls through to target
    resolution where the user gets a clear "no artifact found" error
    with the candidate target list.
    """

    if len(value) != _ULID_LENGTH:
        return False
    return all(ch in _ULID_ALPHABET for ch in value)


def _resolve_artifact(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    target_or_ulid: str,
    require_fresh: bool,
) -> tuple[vault_compiled.CompiledArtifact, str | None]:
    """Resolve the inspect query to one artifact + the matched target.

    ULID lookup goes through :func:`vault_compiled.get`, then re-checks
    ``artifact.agent_id`` because the storage helper does not filter by
    agent id (per ADR 0019, cross-agent isolation belongs to the calling
    boundary). Target lookup picks the most recent fresh
    ``compiled_notebook`` whose ``source_facets`` form a non-empty
    subset of ``list_compile_sources(target)`` — the smallest
    well-defined "this artifact serves target T" predicate that fits the
    existing storage shape without inventing a new column.

    ``require_fresh`` runs after resolution so callers get an artifact-
    specific error message when a found artifact is stale rather than a
    blanket "not found".
    """

    if _looks_like_ulid(target_or_ulid):
        artifact = vault_compiled.get(conn, external_id=target_or_ulid)
        if artifact is None or artifact.agent_id != agent_id:
            raise CliError(f"no compiled artifact with external_id {target_or_ulid!r}")
        if require_fresh and artifact.is_stale:
            raise CliError(
                f"compiled artifact {artifact.external_id} is stale; "
                "drop --require-fresh to inspect anyway or recompile first"
            )
        return artifact, None
    target = target_or_ulid
    eligible = _eligible_source_ulids(conn, agent_id=agent_id, target=target)
    if not eligible:
        raise CliError(
            f"target {target!r} has no compile_into sources; "
            "tag at least one source facet's metadata.compile_into=[target] or "
            "pass a compiled-artifact ULID directly"
        )
    candidates = vault_compiled.list_for_agent(conn, agent_id=agent_id, limit=200)
    fresh_match: vault_compiled.CompiledArtifact | None = None
    stale_match: vault_compiled.CompiledArtifact | None = None
    for candidate in candidates:
        if not candidate.source_facets:
            continue
        if not set(candidate.source_facets).issubset(eligible):
            continue
        if candidate.is_stale:
            if stale_match is None:
                stale_match = candidate
        else:
            fresh_match = candidate
            break
    if fresh_match is not None:
        return fresh_match, target
    if stale_match is not None:
        if require_fresh:
            raise CliError(
                f"target {target!r} has no fresh artifact; only stale candidate "
                f"{stale_match.external_id} matches "
                "(drop --require-fresh to inspect the stale artifact)"
            )
        return stale_match, target
    raise CliError(
        f"target {target!r} has no compiled artifact whose source_facets are a "
        "subset of the target's compile_into sources; "
        "register one with `tessera playbook register` or check the source tags"
    )


def _eligible_source_ulids(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    target: str,
) -> set[str]:
    """Return every source ULID tagged ``compile_into=[target]`` for the agent.

    Diverges from :func:`vault_compiled.list_for_compilation` on one
    point: includes soft-deleted facets. The inspect surface needs to
    resolve a stale artifact whose source got soft-deleted, and the
    artifact's stored ``source_facets`` ULIDs preserve the link even
    after the facet is tombstoned. The metadata column survives the
    ``is_deleted=1`` flip so the ``compile_into`` membership remains
    queryable for resolution.
    """

    rows = conn.execute(
        """
        SELECT external_id
        FROM facets
        WHERE agent_id = ?
              AND facet_type IN ('agent_profile', 'project', 'skill', 'verification_checklist')
              AND EXISTS (
                  SELECT 1
                  FROM json_each(json_extract(metadata, '$.compile_into'))
                  WHERE json_each.value = ?
              )
        """,
        (agent_id, target),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _lookup_stale_cause(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    external_id: str,
) -> tuple[str | None, str | None]:
    """Read the most recent staleness audit row for an artifact.

    Mirrors the ``compiled_artifact_marked_stale`` lookup in
    :func:`vault_compiled.list_stale_artifacts` but for one artifact at
    a time so the inspect render can echo "stale because <op> on
    <source>" without listing every stale artifact in the vault.
    """

    row = conn.execute(
        """
        SELECT payload
        FROM audit_log
        WHERE op = 'compiled_artifact_marked_stale'
              AND target_external_id = ?
              AND agent_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (external_id, agent_id),
    ).fetchone()
    if row is None:
        return None, None
    try:
        payload = json.loads(str(row[0]) if row[0] is not None else "{}")
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    source_id = payload.get("source_external_id")
    source_op = payload.get("source_op")
    return (
        source_id if isinstance(source_id, str) else None,
        source_op if isinstance(source_op, str) else None,
    )


def _extract_sections(content: str) -> dict[str, tuple[str, str]]:
    """Index Markdown ``##``/``###`` headings to their body.

    Returns a map ``normalized_name -> (raw_heading, body)``. The body
    is everything between the heading and the next heading at the same
    or higher level, with surrounding whitespace stripped. The
    normalized name is lower-cased and trimmed so a ``--field`` value
    matches headings case-insensitively. Duplicate headings keep the
    first occurrence — Phase 7 deliberately rejects "find me every
    section called X" semantics until dogfood proves it useful.
    """

    sections: dict[str, tuple[str, str]] = {}
    lines = content.splitlines()
    current_name: str | None = None
    current_heading: str = ""
    current_body: list[str] = []

    def _flush() -> None:
        if current_name is not None and current_name not in sections:
            sections[current_name] = (current_heading, "\n".join(current_body).strip())

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            _flush()
            heading_text = stripped.lstrip("#").strip()
            current_name = heading_text.casefold()
            current_heading = heading_text
            current_body = []
            continue
        if current_name is not None:
            current_body.append(line)
    _flush()
    return sections


def _extract_field_provenance(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pull ``metadata.field_provenance`` while keeping only dict entries.

    The Phase 3 contract permits caller-defined field names but expects
    the value to be a dict of ``source_facets`` / ``source_refs`` /
    ``confidence`` / ``notes``. Anything else is filtered out so a
    malformed entry cannot crash the renderer.
    """

    raw = metadata.get("field_provenance")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            out[key] = value
    return out


def _build_field_views(
    requested: list[str],
    *,
    sections: dict[str, tuple[str, str]],
    field_provenance: dict[str, dict[str, Any]],
    include_provenance: bool,
    max_snippet: int,
) -> list[_FieldView]:
    """Materialize one ``_FieldView`` per requested field.

    Raises :class:`CliError` listing every missing field at once so the
    user fixes a typo in one shot rather than discovering them one by
    one. Per Phase 7 design constraints, a missing field is a hard
    error — no fallback content, no inferred match.
    """

    views: list[_FieldView] = []
    missing: list[str] = []
    provenance_keys_lower = {k.casefold(): k for k in field_provenance}
    for name in requested:
        normalized = name.casefold()
        section = sections.get(normalized)
        prov_key = provenance_keys_lower.get(normalized)
        if section is None and prov_key is None:
            missing.append(name)
            continue
        snippet: str | None
        truncated = False
        heading: str | None = None
        if section is not None:
            heading, body = section
            if max_snippet == 0 or len(body) <= max_snippet:
                snippet = body
            else:
                snippet = body[:max_snippet]
                truncated = True
        else:
            snippet = None
        prov_entry = (
            field_provenance[prov_key] if include_provenance and prov_key is not None else None
        )
        views.append(
            _FieldView(
                name=name,
                section_heading=heading,
                snippet=snippet,
                snippet_truncated=truncated,
                provenance=prov_entry,
            )
        )
    if missing:
        available_sections = sorted({h for h, _ in sections.values()})
        available_provenance = sorted(field_provenance.keys())
        suggestions = ", ".join(available_sections) or "(no Markdown headings)"
        prov_list = ", ".join(available_provenance) or "(no field_provenance entries)"
        raise CliError(
            "field(s) not found in artifact: "
            + ", ".join(missing)
            + f"; available sections: {suggestions}; available provenance keys: {prov_list}"
        )
    return views


def _inspect_to_json(view: _ArtifactView, *, query: str) -> str:
    """Render the inspect result as deterministic JSON.

    The shape stays narrow on purpose (Phase 7 design constraints): one
    artifact summary, one entry per requested field, one optional
    provenance map. Sorting keys keeps the output diff-stable for tests
    and downstream tooling.
    """

    artifact = view.artifact
    payload: dict[str, Any] = {
        "query": query,
        "external_id": artifact.external_id,
        "artifact_type": artifact.artifact_type,
        "compiled_at": _format_epoch(artifact.compiled_at),
        "compiler_version": artifact.compiler_version,
        "is_stale": artifact.is_stale,
        "source_facets": list(artifact.source_facets),
        "resolved_target": view.resolved_target,
        "fields": [
            {
                "name": fv.name,
                "section_heading": fv.section_heading,
                "snippet": fv.snippet,
                "snippet_truncated": fv.snippet_truncated,
                "provenance": fv.provenance,
            }
            for fv in view.field_views
        ],
    }
    if view.stale_cause is not None:
        source_id, source_op = view.stale_cause
        payload["stale_cause"] = {
            "source_external_id": source_id,
            "source_op": source_op,
        }
    if not view.field_views:
        if view.max_snippet == 0 or len(artifact.content) <= view.max_snippet:
            payload["content"] = artifact.content
            payload["content_truncated"] = False
        else:
            payload["content"] = artifact.content[: view.max_snippet]
            payload["content_truncated"] = True
    if view.include_artifact_provenance:
        payload["field_provenance"] = view.artifact_provenance
    return json.dumps(payload, indent=2, sort_keys=True)


def _render_inspect_table(view: _ArtifactView, *, query: str) -> None:
    """Render the inspect result for the TTY branch.

    Two-table layout: a ``kv`` block for the artifact header (so the
    user always sees ``is_stale`` and ``compiled_at``) and a row per
    requested field with section heading, snippet, and provenance
    indicator. Long snippets render verbatim; truncation is signalled
    in the cell text so a piped paste downstream still says it was cut.
    """

    artifact = view.artifact
    header = f"playbook inspect: {query}"
    table = report_table(
        header,
        ["field", "value"],
        emoji=EMOJI["recall"],
    )
    table.add_row("external_id", artifact.external_id)
    table.add_row("artifact_type", artifact.artifact_type)
    table.add_row("resolved_target", view.resolved_target or "(by ULID)")
    table.add_row("compiled_at", _format_epoch(artifact.compiled_at))
    table.add_row("compiler_version", artifact.compiler_version)
    table.add_row("is_stale", "yes" if artifact.is_stale else "no")
    table.add_row("source_facets", ", ".join(artifact.source_facets) or "(none)")
    if view.stale_cause is not None:
        cause_id, cause_op = view.stale_cause
        table.add_row("stale_cause", f"{cause_op or '?'} on {cause_id or '?'}")
    console.print(table)
    if view.field_views:
        fields_table = report_table(
            "fields",
            ["name", "section_heading", "snippet", "provenance"],
            emoji=EMOJI["recall"],
        )
        for fv in view.field_views:
            snippet = fv.snippet or "(metadata-only)"
            if fv.snippet_truncated:
                snippet = snippet + " …"
            provenance_repr = "yes" if fv.provenance is not None else "no"
            fields_table.add_row(
                fv.name,
                fv.section_heading or "(no heading)",
                snippet,
                provenance_repr,
            )
        console.print(fields_table)
    elif view.max_snippet == 0 or len(artifact.content) <= view.max_snippet:
        console.print(artifact.content)
    else:
        console.print(artifact.content[: view.max_snippet] + " …")
    if view.include_artifact_provenance and view.artifact_provenance:
        prov_table = report_table(
            "field_provenance",
            ["field", "source_facets", "source_refs"],
            emoji=EMOJI["recall"],
        )
        for key in sorted(view.artifact_provenance):
            entry = view.artifact_provenance[key]
            facets_list = entry.get("source_facets")
            refs_list = entry.get("source_refs")
            facets_repr = (
                ", ".join(str(f) for f in facets_list) if isinstance(facets_list, list) else ""
            )
            refs_repr = json.dumps(refs_list, sort_keys=True) if isinstance(refs_list, list) else ""
            prov_table.add_row(key, facets_repr, refs_repr)
        console.print(prov_table)


__all__ = ["register"]
