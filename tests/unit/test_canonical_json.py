"""ADR 0021 canonical_json — byte-stable serialization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tessera.vault.canonical_json import CanonicalJSONError, canonical_json


@pytest.mark.unit
def test_canonical_json_encodes_primitives() -> None:
    assert canonical_json(None) == b"null"
    assert canonical_json(True) == b"true"
    assert canonical_json(False) == b"false"
    assert canonical_json(0) == b"0"
    assert canonical_json(-7) == b"-7"
    assert canonical_json(1.5) == b"1.5"


@pytest.mark.unit
def test_canonical_json_sorts_object_keys() -> None:
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


@pytest.mark.unit
def test_canonical_json_no_whitespace() -> None:
    out = canonical_json({"a": [1, 2, {"c": 3}]})
    assert b" " not in out
    assert b"\n" not in out
    assert b"\t" not in out


@pytest.mark.unit
def test_canonical_json_rejects_non_string_keys() -> None:
    with pytest.raises(CanonicalJSONError, match="object keys must be strings"):
        canonical_json({1: "a"})


@pytest.mark.unit
def test_canonical_json_rejects_nan_and_inf() -> None:
    with pytest.raises(CanonicalJSONError, match="NaN"):
        canonical_json(float("nan"))
    with pytest.raises(CanonicalJSONError, match="Infinity"):
        canonical_json(float("inf"))
    with pytest.raises(CanonicalJSONError, match="Infinity"):
        canonical_json(float("-inf"))


@pytest.mark.unit
def test_canonical_json_rejects_set() -> None:
    with pytest.raises(CanonicalJSONError, match="set"):
        canonical_json({1, 2, 3})


@pytest.mark.unit
def test_canonical_json_rejects_naive_datetime() -> None:
    naive = datetime(2026, 5, 2, 12, 0, 0)
    with pytest.raises(CanonicalJSONError, match="timezone-aware"):
        canonical_json(naive)


@pytest.mark.unit
def test_canonical_json_normalises_datetime_to_utc() -> None:
    aware_in_utc = datetime(2026, 5, 2, 12, 0, 0, 123456, tzinfo=UTC)
    aware_in_pst = aware_in_utc.astimezone(timezone(timedelta(hours=-8)))
    assert canonical_json(aware_in_utc) == canonical_json(aware_in_pst)
    assert canonical_json(aware_in_utc) == b'"2026-05-02T12:00:00.123456Z"'


@pytest.mark.unit
def test_canonical_json_escapes_control_characters() -> None:
    assert canonical_json("line1\nline2") == b'"line1\\nline2"'
    assert canonical_json("tab\there") == b'"tab\\there"'
    assert canonical_json('quoted "x"') == b'"quoted \\"x\\""'
    assert canonical_json("back\\slash") == b'"back\\\\slash"'


@pytest.mark.unit
def test_canonical_json_emits_hex_for_non_ascii() -> None:
    # All non-ASCII codepoints are emitted as \uXXXX so the output
    # bytes are stable regardless of source encoding.
    assert canonical_json("café") == b'"caf\\u00e9"'


@pytest.mark.unit
def test_canonical_json_surrogate_pair_for_non_bmp() -> None:
    # 🦊 is U+1F98A; surrogate pair is D83E DD8A.
    assert canonical_json("🦊") == b'"\\ud83e\\udd8a"'


@pytest.mark.unit
def test_canonical_json_lists_have_no_trailing_separator() -> None:
    assert canonical_json([1, 2, 3]) == b"[1,2,3]"
    assert canonical_json([]) == b"[]"


@pytest.mark.unit
def test_canonical_json_tuple_serialises_as_list() -> None:
    assert canonical_json((1, 2, 3)) == canonical_json([1, 2, 3])


@pytest.mark.unit
def test_canonical_json_deep_dict_round_trip_stable() -> None:
    payload = {
        "alpha": 1,
        "beta": [1, 2, {"x": "y"}],
        "delta": {"nested": [None, True, False]},
    }
    first = canonical_json(payload)
    second = canonical_json(payload)
    assert first == second


@pytest.mark.unit
def test_canonical_json_byte_stable_across_two_runs_on_fixed_vector() -> None:
    """The audit-chain determinism gate runs this exact assertion in CI."""

    payload = {
        "id": 42,
        "at": datetime(2026, 5, 2, 12, 0, 0, 0, tzinfo=UTC),
        "actor": "system",
        "agent_id": 7,
        "op": "facet_inserted",
        "target_external_id": "01HZX1Y2Z3MNPQRSTVWXYZ0123",
        "payload": {
            "facet_type": "agent_profile",
            "source_tool": "cli",
            "is_duplicate": False,
            "content_hash_prefix": "deadbeef",
            "volatility": "persistent",
            "ttl_seconds": None,
        },
    }
    first = canonical_json(payload)
    second = canonical_json(payload)
    assert first == second
    # Lock the exact bytes so a regression in the canonicalizer
    # (e.g. a change to dict-key ordering, datetime formatting, or
    # float repr) is caught at the test layer before it reaches the
    # determinism CI gate.
    assert first == (
        b'{"actor":"system",'
        b'"agent_id":7,'
        b'"at":"2026-05-02T12:00:00.000000Z",'
        b'"id":42,'
        b'"op":"facet_inserted",'
        b'"payload":{'
        b'"content_hash_prefix":"deadbeef",'
        b'"facet_type":"agent_profile",'
        b'"is_duplicate":false,'
        b'"source_tool":"cli",'
        b'"ttl_seconds":null,'
        b'"volatility":"persistent"'
        b"},"
        b'"target_external_id":"01HZX1Y2Z3MNPQRSTVWXYZ0123"}'
    )
