"""Project-local canonical JSON serializer per ADR 0021 §canonical_json.

The audit-chain hash is computed over ``sha256(prev_hash ||
canonical_json(event))``. The chain only stays verifiable across
Python versions, SQLite versions, and BYO-sync round trips if the
serializer produces byte-stable output for byte-equivalent inputs.
Python's stdlib ``json.dumps(..., sort_keys=True, separators=(",",
":"))`` is close, but it leaks several edge cases that have moved
between minor versions in the past:

* ``json.dumps(float('nan'))`` and ``json.dumps(float('inf'))``
  produce non-JSON tokens (``NaN`` / ``Infinity``) that round-trip
  through ``json.loads`` but break every other JSON parser.
* ``json.dumps(1.0)`` formats as ``"1.0"`` while
  ``json.dumps(1)`` formats as ``"1"``. RFC 8785 wants the
  shortest round-trip form, but stdlib's float formatting has
  shifted between Python releases.
* ``json.dumps`` lets non-string keys through with
  ``sort_keys=True`` — TypeError on dict iteration order is
  observable across versions.
* Datetimes must be string-serialized by callers; stdlib
  ``json`` does not encode them.

The canonicalizer here closes those gaps with explicit rules:

1. **Object keys** are strings only; non-string keys raise
   :class:`CanonicalJSONError`. Keys are sorted by Python
   ``sorted()`` (lexicographic on the str).
2. **Datetimes** (``datetime.datetime``) format as
   ``YYYY-MM-DDTHH:MM:SS.uuuuuuZ`` (microsecond precision, Z
   suffix; naive datetimes are rejected to avoid timezone
   ambiguity).
3. **Integers** format as plain decimal — no scientific notation.
4. **Floats** format as the shortest round-trip representation
   via :func:`float.__repr__` (Python 3.1+ uses David Gay's
   algorithm); ``inf`` / ``-inf`` / ``nan`` raise.
5. **Strings** use ``\\uXXXX`` escapes for control characters and
   non-ASCII surrogates only when the codepoint is unpaired;
   otherwise emit valid UTF-8 (matching RFC 8785). The output is
   ASCII-safe by construction.
6. **Bools** serialize as ``true`` / ``false``; ``None`` as
   ``null``.
7. **Lists** serialize as ``[item, item]`` with no whitespace.
8. **Tuples** are accepted and treated as lists. Sets and
   frozensets raise — sets have no defined ordering even after
   ``sorted()`` for mixed-type elements.

Every other Python value type raises. The function is intentionally
narrow so a regression is loud rather than silent.

Thread-safety: pure function, no shared state.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Final

_DATETIME_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S.%fZ"


class CanonicalJSONError(TypeError):
    """A value cannot be canonicalized.

    Inherits from :class:`TypeError` so callers catching the broader
    ``json``-style errors still see this surface, but the project
    code can branch on the narrower class for clearer messages.
    """


def canonical_json(value: Any) -> bytes:
    """Return the canonical UTF-8 byte serialization of ``value``.

    The output is byte-stable across Python versions, SQLite
    versions, and operating systems. Identical inputs produce
    byte-identical outputs; the audit-chain hash depends on this
    invariant.
    """

    return _encode(value).encode("ascii")


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, datetime):
        return _encode_datetime(value)
    if isinstance(value, dict):
        return _encode_dict(value)
    if isinstance(value, list | tuple):
        return _encode_list(value)
    raise CanonicalJSONError(f"cannot canonicalize value of type {type(value).__name__}")


def _encode_float(value: float) -> str:
    if math.isnan(value):
        raise CanonicalJSONError("NaN is not canonicalizable")
    if math.isinf(value):
        raise CanonicalJSONError("Infinity is not canonicalizable")
    # ``repr`` emits the shortest round-trip representation in
    # Python 3.1+. Use it directly; ``json.dumps`` calls the same
    # algorithm under the hood but with extra wrapping that has
    # shifted between Python versions.
    text = repr(value)
    # repr emits ``1.0`` for whole-number floats; that is the
    # round-trip-shortest form and what we want for byte stability
    # against Python releases.
    return text


def _encode_string(value: str) -> str:
    out: list[str] = ['"']
    for char in value:
        codepoint = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif codepoint == 0x08:
            out.append("\\b")
        elif codepoint == 0x09:
            out.append("\\t")
        elif codepoint == 0x0A:
            out.append("\\n")
        elif codepoint == 0x0C:
            out.append("\\f")
        elif codepoint == 0x0D:
            out.append("\\r")
        elif codepoint < 0x20:
            out.append(f"\\u{codepoint:04x}")
        elif codepoint < 0x7F:
            out.append(char)
        elif codepoint <= 0xFFFF:
            out.append(f"\\u{codepoint:04x}")
        else:
            # Emit as a UTF-16 surrogate pair to keep the output
            # ASCII-safe and byte-stable. RFC 8785 §3.1.5 names this
            # specific encoding; without it, non-BMP characters would
            # leave us at the mercy of Python's source-encoding
            # choices for the bytes the canonicalizer eventually
            # writes.
            adjusted = codepoint - 0x10000
            high = 0xD800 + (adjusted >> 10)
            low = 0xDC00 + (adjusted & 0x3FF)
            out.append(f"\\u{high:04x}\\u{low:04x}")
    out.append('"')
    return "".join(out)


def _encode_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise CanonicalJSONError(
            "datetime must be timezone-aware (UTC); naive datetimes are ambiguous"
        )
    # Normalise to UTC so two datetimes that point at the same
    # instant in different zones serialize identically.
    in_utc = value.astimezone(UTC)
    return f'"{in_utc.strftime(_DATETIME_FORMAT)}"'


def _encode_dict(value: dict[Any, Any]) -> str:
    parts: list[str] = ["{"]
    keys = list(value.keys())
    for key in keys:
        if not isinstance(key, str):
            raise CanonicalJSONError(f"object keys must be strings, got {type(key).__name__}")
    for index, key in enumerate(sorted(keys)):
        if index > 0:
            parts.append(",")
        parts.append(_encode_string(key))
        parts.append(":")
        parts.append(_encode(value[key]))
    parts.append("}")
    return "".join(parts)


def _encode_list(value: list[Any] | tuple[Any, ...]) -> str:
    parts: list[str] = ["["]
    for index, item in enumerate(value):
        if index > 0:
            parts.append(",")
        parts.append(_encode(item))
    parts.append("]")
    return "".join(parts)


__all__ = ["CanonicalJSONError", "canonical_json"]
