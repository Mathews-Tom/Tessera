"""Diagnostic-bundle scrubber.

Per ``docs/determinism-and-observability.md §Scrubber`` every file
that ships inside a ``tessera doctor --collect`` tarball passes
through this module first. The scrubber is a closed gate: it raises
:class:`ScrubberViolationError` on any field that matches a credential
key-name pattern, any string that exceeds the content escape-hatch
length, or any value matching a known-secret regex. A violating
bundle is rejected — the tarball is not produced.

The design keeps the check boring on purpose. Three narrow rules, a
short list of regexes per public API key format, one length cap. A
future leak-vector is either a new credential format (add a regex and
a regression test in the same commit) or a new bundle file type
(route it through :func:`scrub_bundle_file` before packaging).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# Longest legitimate bundle string observed at design time is the
# full `.schema` dump line, around 180 chars. 256 is comfortably
# above that and well below a body-of-facet-content threshold.
# Content that slips into a bundle is the leak we catch; the
# ``.schema`` dump is the only routine exception, handled by marking
# the schema-file value at emit time rather than relaxing the cap.
CONTENT_STRING_LIMIT: Final[int] = 256

# Key-name substrings that by convention carry secrets. Case-insensitive.
# Matched against the lowercased JSON key; bundle emitters intentionally
# avoid these names for non-sensitive fields so the scrubber's false-
# positive surface is zero.
_FORBIDDEN_KEY_SUBSTRINGS: Final[tuple[str, ...]] = (
    "token",
    "key",
    "passphrase",
    "secret",
    "api_",
    "bearer",
    "authorization",
)

# Credential regexes. Each pattern fires on any string literal anywhere
# in the bundle payload (not just inside forbidden keys), so a rogue
# emitter that tucks a token into an otherwise-innocent field is still
# caught.
_CREDENTIAL_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_session_key", re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Tessera's own token format — a leaked bundle must never carry one.
    ("tessera_token", re.compile(r"\btessera_(session|service|subagent)_[A-Z2-7]{20,}\b")),
    # Private keys in PEM form.
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


class ScrubberError(Exception):
    """Base class for scrubber failures."""


class ScrubberViolationError(ScrubberError):
    """Bundle payload violated one or more scrubber rules."""


@dataclass(frozen=True, slots=True)
class ScrubViolation:
    """One scrubber rule that fired.

    ``location`` is a dotted path into the payload (``recent_events.3.attrs.token``)
    so operators reviewing a rejected bundle can find the offending field without
    re-reading the whole file. ``rule`` names the rule that matched — callers
    inspecting a list of violations can sort or dedupe by rule.
    """

    location: str
    rule: str
    sample: str

    def render(self) -> str:
        return f"[{self.rule}] {self.location}: {self.sample}"


def scrub_bundle_file(name: str, payload: Any) -> None:
    """Assert ``payload`` is safe to ship under bundle file ``name``.

    ``payload`` may be a dict, list, scalar, or any nested combination
    produced by :func:`json.loads`. Assertions fire fast: the first
    violation aborts the walk (but every violation is surfaced in the
    error message by collecting them in one pass so operators see the
    full picture, not one-at-a-time whack-a-mole).
    """

    violations = _scan(payload, location=name)
    if violations:
        raise ScrubberViolationError(
            "scrubber refused to ship bundle file; violations:\n"
            + "\n".join(f"  {v.render()}" for v in violations)
        )


def scrub_text_file(name: str, text: str) -> None:
    """Assert a raw text payload (schema dump, README, ...) is safe.

    Unlike :func:`scrub_bundle_file`, string payloads are allowed to
    exceed the content length cap (a schema dump is legitimately long)
    but credential-regex matches still abort the bundle. Callers that
    want the length cap applied pass the text through
    :func:`scrub_bundle_file` as a dict value instead.
    """

    violations = [
        ScrubViolation(location=name, rule=rule, sample=_sample(match.group(0)))
        for rule, pattern in _CREDENTIAL_PATTERNS
        for match in pattern.finditer(text)
    ]
    if violations:
        raise ScrubberViolationError(
            "scrubber refused to ship bundle text file; violations:\n"
            + "\n".join(f"  {v.render()}" for v in violations)
        )


def _scan(value: Any, *, location: str) -> list[ScrubViolation]:
    if isinstance(value, dict):
        return _scan_dict(value, location=location)
    if isinstance(value, list):
        return _scan_list(value, location=location)
    if isinstance(value, str):
        return _scan_string(value, location=location)
    # Ints, floats, bools, None — non-string scalars cannot carry secrets.
    return []


def _scan_dict(value: dict[Any, Any], *, location: str) -> list[ScrubViolation]:
    out: list[ScrubViolation] = []
    for raw_key, nested in value.items():
        key = str(raw_key)
        child_location = f"{location}.{key}"
        if _is_forbidden_key(key):
            out.append(
                ScrubViolation(
                    location=child_location,
                    rule="forbidden_key_name",
                    sample=_sample(key),
                )
            )
        out.extend(_scan(nested, location=child_location))
    return out


def _scan_list(value: Iterable[Any], *, location: str) -> list[ScrubViolation]:
    out: list[ScrubViolation] = []
    for idx, item in enumerate(value):
        out.extend(_scan(item, location=f"{location}.{idx}"))
    return out


def _scan_string(value: str, *, location: str) -> list[ScrubViolation]:
    out: list[ScrubViolation] = []
    if len(value) > CONTENT_STRING_LIMIT:
        out.append(
            ScrubViolation(
                location=location,
                rule="string_length_cap",
                sample=f"len={len(value)}",
            )
        )
    for rule, pattern in _CREDENTIAL_PATTERNS:
        for match in pattern.finditer(value):
            out.append(
                ScrubViolation(
                    location=location,
                    rule=rule,
                    sample=_sample(match.group(0)),
                )
            )
    return out


def _is_forbidden_key(key: str) -> bool:
    lowered = key.lower()
    return any(sub in lowered for sub in _FORBIDDEN_KEY_SUBSTRINGS)


def _sample(value: str) -> str:
    """Render a short tag for a violation without echoing the secret."""

    if len(value) <= 16:
        return f"{value!r}"
    return f"{value[:8]!r}... ({len(value)} chars)"


def scrub_jsonl_file(path: Path, *, label: str | None = None) -> None:
    """Scan a JSONL file line-by-line; refuse if any line violates a rule."""

    location = label or path.name
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScrubberViolationError(
                    f"scrubber could not parse {location}:{lineno}: {exc}"
                ) from exc
            scrub_bundle_file(f"{location}:{lineno}", payload)


__all__ = [
    "CONTENT_STRING_LIMIT",
    "ScrubViolation",
    "ScrubberError",
    "ScrubberViolationError",
    "scrub_bundle_file",
    "scrub_jsonl_file",
    "scrub_text_file",
]
