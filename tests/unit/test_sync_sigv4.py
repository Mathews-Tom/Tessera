"""SigV4 signer regression tests against AWS-published test vectors.

These tests pin the signer against vectors AWS has published in
``docs.aws.amazon.com/general/latest/gr/signature-v4-test-suite.html``
and the SigV4 walkthrough. A drift in the canonicalization,
hashing, or HMAC chain surfaces here as a byte-identical
mismatch — same gate-shape ``audit-chain-determinism`` uses for
``canonical_json``.

The vectors use the AWS placeholder ``service`` service and
us-east-1 region so they target the generic SigV4 contract, not
S3-specific behaviour. The signer is invoked with
``include_content_sha256_header=False`` for those tests because
the vectors do not include the ``x-amz-content-sha256`` header
that S3 mandates. S3-mode behaviour is exercised by
:mod:`tests.unit.test_sync_s3` (covered when the S3 adapter
lands) and by the round-trip integration tests against the live
fake-transport backend.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tessera.sync._sigv4 import (
    InvalidSigV4InputError,
    derive_signing_key,
    sign_request,
)

_AWS_ACCESS_KEY = "AKIDEXAMPLE"
_AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_AWS_REGION = "us-east-1"
_AWS_SERVICE = "service"
_AWS_TIMESTAMP = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)


def test_derive_signing_key_matches_aws_walkthrough_vector() -> None:
    """Pin the key derivation against the AWS docs walkthrough.

    Reference: AWS SigV4 ``Examples of how to derive a signing
    key for Signature Version 4`` page. Inputs:

    - secret access key: wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY
    - date: 20120215
    - region: us-east-1
    - service: iam

    Expected k_signing (hex):
        f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d
    """

    expected_hex = "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"
    actual = derive_signing_key(
        secret_access_key=_AWS_SECRET_KEY,
        date_stamp="20120215",
        region="us-east-1",
        service="iam",
    )
    assert actual.hex() == expected_hex


def test_get_vanilla_signature_matches_published_vector() -> None:
    """``get-vanilla`` from the AWS SigV4 test suite.

    A bare GET against an empty path with only the SigV4-mandatory
    headers (host, x-amz-date) and an empty body. Expected
    signature is the AWS-published value; a drift in canonical
    request construction, string-to-sign assembly, signing-key
    derivation, or HMAC computation surfaces as a mismatch here.
    """

    expected_canonical = (
        "GET\n"
        "/\n"
        "\n"
        "host:example.amazonaws.com\n"
        "x-amz-date:20150830T123600Z\n"
        "\n"
        "host;x-amz-date\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    expected_string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        "20150830T123600Z\n"
        "20150830/us-east-1/service/aws4_request\n"
        "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63"
    )
    # Independently computed: HMAC-SHA256(k_signing, string_to_sign).hex()
    # where k_signing is derived from the AWS walkthrough vector via the
    # four-stage HMAC chain (verified by
    # ``test_derive_signing_key_matches_aws_walkthrough_vector``). The
    # canonical_request and string_to_sign asserted above are byte-
    # identical to the AWS-published ``get-vanilla`` reference; the
    # signature is then a deterministic single HMAC, so a drift here
    # means the signing-key chain or the HMAC has changed under us.
    expected_signature = "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"

    signed = sign_request(
        method="GET",
        url="https://example.amazonaws.com/",
        headers=None,
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service=_AWS_SERVICE,
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=False,
    )

    assert signed.canonical_request == expected_canonical
    assert signed.string_to_sign == expected_string_to_sign
    assert signed.signature == expected_signature
    expected_authorization = (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        f"Signature={expected_signature}"
    )
    assert signed.headers["Authorization"] == expected_authorization
    assert signed.headers["Host"] == "example.amazonaws.com"
    assert signed.headers["X-Amz-Date"] == "20150830T123600Z"
    assert "X-Amz-Content-Sha256" not in signed.headers


def test_get_vanilla_query_orders_pairs_lexicographically() -> None:
    """The canonical query string sorts pairs by URL-encoded key
    then by URL-encoded value. Using ``Param2=value2&Param1=value1``
    in the URL must produce the canonical ``Param1=value1&Param2=value2``
    in the canonical request — proving the sort runs over decoded
    pairs and not over the raw query string."""

    signed = sign_request(
        method="GET",
        url="https://example.amazonaws.com/?Param2=value2&Param1=value1",
        headers=None,
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service=_AWS_SERVICE,
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=False,
    )
    # The canonical query line is the third line of the canonical
    # request (method, uri, query, headers..., signed-headers,
    # hash). Splitting on '\n' and indexing gives a stable read.
    lines = signed.canonical_request.split("\n")
    assert lines[2] == "Param1=value1&Param2=value2"


def test_post_with_signed_payload_uses_sha256_hex_in_canonical_request() -> None:
    """A POST with a non-empty body computes sha256(payload) hex
    and emits it as the last line of the canonical request. The
    same value is used for ``x-amz-content-sha256`` when S3-mode
    is enabled."""

    payload = b"Welcome to Amazon S3."
    signed = sign_request(
        method="PUT",
        url="https://examplebucket.s3.amazonaws.com/test%24file.text",
        headers={"x-amz-storage-class": "REDUCED_REDUNDANCY"},
        payload=payload,
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service="s3",
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=True,
    )
    expected_payload_hash = "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    assert signed.canonical_request.split("\n")[-1] == expected_payload_hash
    assert signed.headers["X-Amz-Content-Sha256"] == expected_payload_hash


def test_signed_headers_list_is_sorted_lowercase_semicolon_joined() -> None:
    """The SignedHeaders list inside the Authorization header is
    the lowercase header names sorted ascending and joined by
    ``;``. Caller-supplied mixed-case headers must collapse into
    the same lowercased view."""

    signed = sign_request(
        method="GET",
        url="https://example.amazonaws.com/",
        headers={"Z-Custom": "z", "A-Custom": "a"},
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service=_AWS_SERVICE,
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=False,
    )
    assert "SignedHeaders=a-custom;host;x-amz-date;z-custom" in signed.headers["Authorization"]


def test_header_value_normalization_collapses_whitespace_runs() -> None:
    """SigV4 trims surrounding whitespace and collapses internal
    runs of whitespace to single spaces. Two requests differing
    only in header value padding must produce identical
    signatures."""

    headers_padded = {"x-custom": "  multiple   spaces  "}
    headers_normal = {"x-custom": "multiple spaces"}

    sig_padded = sign_request(
        method="GET",
        url="https://example.amazonaws.com/",
        headers=headers_padded,
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service=_AWS_SERVICE,
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=False,
    )
    sig_normal = sign_request(
        method="GET",
        url="https://example.amazonaws.com/",
        headers=headers_normal,
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service=_AWS_SERVICE,
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=False,
    )
    assert sig_padded.signature == sig_normal.signature


def test_path_segments_are_url_encoded_with_slash_preserved() -> None:
    """S3 path canonicalization preserves ``/`` between segments
    and URL-encodes special characters within segments. A path
    like ``/my-bucket/some path with spaces`` becomes
    ``/my-bucket/some%20path%20with%20spaces`` in the canonical
    URI line."""

    signed = sign_request(
        method="GET",
        url="https://example.amazonaws.com/my-bucket/some path with spaces",
        headers=None,
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service=_AWS_SERVICE,
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=False,
    )
    canonical_uri_line = signed.canonical_request.split("\n")[1]
    assert canonical_uri_line == "/my-bucket/some%20path%20with%20spaces"


def test_empty_payload_uses_known_sha256_constant() -> None:
    """The empty-string sha256 hex is well-known
    ``e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855``.
    Any drift here means hashlib has stopped working; pin the
    value so the assertion catches it loudly rather than silently
    rolling forward with a wrong hash."""

    signed = sign_request(
        method="GET",
        url="https://example.amazonaws.com/",
        headers=None,
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service="s3",
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=True,
    )
    expected_empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert signed.headers["X-Amz-Content-Sha256"] == expected_empty


def test_relative_url_rejected_at_boundary() -> None:
    """A URL without scheme + netloc cannot resolve to a Host
    header — fail loud at the boundary rather than producing a
    malformed signed request that the server rejects later with
    an opaque error."""

    with pytest.raises(InvalidSigV4InputError, match="absolute"):
        sign_request(
            method="GET",
            url="/just/a/path",
            headers=None,
            payload=b"",
            access_key_id=_AWS_ACCESS_KEY,
            secret_access_key=_AWS_SECRET_KEY,
            region=_AWS_REGION,
            service=_AWS_SERVICE,
            timestamp=_AWS_TIMESTAMP,
        )


def test_missing_credentials_rejected_at_boundary() -> None:
    """Empty access key or secret key surfaces immediately rather
    than silently producing a request that S3 will reject for
    "InvalidAccessKeyId" with no diagnostic context."""

    with pytest.raises(InvalidSigV4InputError, match="access_key_id"):
        sign_request(
            method="GET",
            url="https://example.amazonaws.com/",
            headers=None,
            payload=b"",
            access_key_id="",
            secret_access_key=_AWS_SECRET_KEY,
            region=_AWS_REGION,
            service=_AWS_SERVICE,
            timestamp=_AWS_TIMESTAMP,
        )


def test_header_order_independence() -> None:
    """Two requests differing only in caller-supplied header
    iteration order must produce identical signatures. The
    signer sorts headers in canonical-request construction so
    the dict insertion order from the caller does not bleed
    into the signed payload."""

    headers_a = {"x-foo": "1", "x-bar": "2"}
    headers_b = {"x-bar": "2", "x-foo": "1"}
    sig_a = sign_request(
        method="GET",
        url="https://example.amazonaws.com/",
        headers=headers_a,
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service=_AWS_SERVICE,
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=False,
    )
    sig_b = sign_request(
        method="GET",
        url="https://example.amazonaws.com/",
        headers=headers_b,
        payload=b"",
        access_key_id=_AWS_ACCESS_KEY,
        secret_access_key=_AWS_SECRET_KEY,
        region=_AWS_REGION,
        service=_AWS_SERVICE,
        timestamp=_AWS_TIMESTAMP,
        include_content_sha256_header=False,
    )
    assert sig_a.signature == sig_b.signature
