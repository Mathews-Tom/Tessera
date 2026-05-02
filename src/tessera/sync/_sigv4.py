"""AWS Signature Version 4 signer for the BYO sync S3 adapter.

Hand-rolled per ADR-0022 D1: the S3 adapter signs requests with
this module and dispatches them through ``httpx``. The module has
no dependency on ``boto3`` / ``aioboto3``. Four-verb surface (PUT
object, GET object, LIST objects with prefix, HEAD object) is the
only place SigV4 runs; the signer is generic enough to handle the
full SigV4 contract but the CI test corpus only exercises the
verbs the S3 adapter actually emits.

Reference: AWS Signature Version 4 documentation
(``docs.aws.amazon.com/general/latest/gr/sigv4_signing.html``).
The AWS-published test vector ``get-vanilla`` and its siblings
gate this module in CI via :mod:`tests.unit.test_sync_sigv4` so a
silent drift from the reference algorithm surfaces as a byte-
identical-signature mismatch — the same failure-shape the
``audit-chain-determinism`` gate uses for ``canonical_json``.

Security boundary: the signer treats the secret access key as the
sole material from which signing keys derive. Callers pass the
key as ``str``; the derivation HMAC-chains it with the date,
region, service, and ``aws4_request`` literal exactly as the
spec mandates. The intermediate keys are not exposed by the
public surface — :func:`sign_request` returns only the headers
the caller adds to the outgoing request.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import parse_qsl, quote, urlsplit

_ALGORITHM: Final[str] = "AWS4-HMAC-SHA256"
_REQUEST_TYPE: Final[str] = "aws4_request"
_AMZ_DATE_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"
_DATE_FORMAT: Final[str] = "%Y%m%d"
_EMPTY_PAYLOAD_SHA256: Final[str] = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
# Per RFC 3986 §2.3 the unreserved set is ALPHA / DIGIT / "-" / "." / "_" / "~".
# AWS canonical URI / query encoding allows exactly these unescaped. The path
# encoding additionally allows "/" because S3 does not collapse path segments
# (see :func:`_canonical_uri_for_s3`); query encoding does NOT allow "/".
_UNRESERVED_QUERY: Final[str] = "-_.~"
_UNRESERVED_PATH: Final[str] = "-_.~/"


class SigV4Error(Exception):
    """Base class for SigV4 signing failures."""


class InvalidSigV4InputError(SigV4Error):
    """Caller passed input that violates the SigV4 contract."""


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """Outcome of signing one request.

    ``headers`` is the full header map the caller sends — it
    includes the ``Authorization``, ``X-Amz-Date``, ``X-Amz-Content-Sha256``,
    ``Host`` headers the signer added on top of caller-supplied
    headers. Caller is expected to pass ``headers`` directly to
    ``httpx.AsyncClient.request(method, url, content=payload, headers=headers)``.
    """

    headers: dict[str, str]
    signature: str
    canonical_request: str
    string_to_sign: str


def sign_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    payload: bytes,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    service: str,
    timestamp: datetime,
    include_content_sha256_header: bool = True,
) -> SignedRequest:
    """Sign one HTTP request under AWS Signature Version 4.

    ``timestamp`` is the request's signing time in UTC. Callers
    pass ``datetime.now(UTC)`` in production; tests pin a known
    value to compare against the AWS-published vectors.

    Returns a :class:`SignedRequest` whose ``headers`` are the
    fully-prepared header map for transmission. The signer does
    NOT mutate caller-supplied headers — it returns a new dict.

    The signer always signs the payload (no ``UNSIGNED-PAYLOAD``
    mode). The S3 adapter never streams uploads at v0.5 (vault
    snapshots are sub-100MB at the dogfood scale ADR-0022 §Out
    of scope §5 names), so the signed-payload mode is always
    appropriate. A future streaming-upload feature opens its own
    ADR.

    ``include_content_sha256_header`` defaults to True because S3
    requires the ``x-amz-content-sha256`` header in every signed
    request. Other AWS services (and the generic AWS-published
    SigV4 test vectors under the placeholder ``service`` service)
    do not require it. The CI test corpus uses ``False`` to
    verify against the published vectors and ``True`` (default)
    for the S3-specific signing surface.
    """

    if not method:
        raise InvalidSigV4InputError("method required")
    if not access_key_id or not secret_access_key:
        raise InvalidSigV4InputError("access_key_id and secret_access_key required")
    if not region or not service:
        raise InvalidSigV4InputError("region and service required")

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise InvalidSigV4InputError(f"url must be absolute, got {url!r}")

    amz_date = timestamp.strftime(_AMZ_DATE_FORMAT)
    date_stamp = timestamp.strftime(_DATE_FORMAT)
    payload_sha256 = _EMPTY_PAYLOAD_SHA256 if not payload else hashlib.sha256(payload).hexdigest()

    # Canonical headers always include host + x-amz-date. The
    # x-amz-content-sha256 header is added when
    # ``include_content_sha256_header`` is True (S3 mode); the
    # caller's header map can focus on application-level headers
    # and never has to add the SigV4 mandatory entries by hand.
    request_headers: dict[str, str] = dict(headers or {})
    request_headers["host"] = parts.netloc
    request_headers["x-amz-date"] = amz_date
    if include_content_sha256_header:
        request_headers["x-amz-content-sha256"] = payload_sha256

    canonical_uri = _canonical_uri_for_s3(parts.path)
    canonical_query = _canonical_query_string(parts.query)
    canonical_headers, signed_headers = _canonical_headers(request_headers)

    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_sha256,
        ]
    )
    canonical_request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

    credential_scope = f"{date_stamp}/{region}/{service}/{_REQUEST_TYPE}"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            credential_scope,
            canonical_request_hash,
        ]
    )

    signing_key = derive_signing_key(
        secret_access_key=secret_access_key,
        date_stamp=date_stamp,
        region=region,
        service=service,
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{_ALGORITHM} "
        f"Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    # Return a header map that mixes the wire-canonical case
    # (Authorization, Host, X-Amz-Date, optionally X-Amz-Content-Sha256)
    # with caller-supplied headers preserved verbatim. httpx is
    # case-insensitive so the duplication a caller might cause by
    # passing "Host" themselves is harmless on the wire; we use
    # the canonical case so debug logs of the headers read
    # naturally.
    final_headers: dict[str, str] = {
        "Host": parts.netloc,
        "X-Amz-Date": amz_date,
        "Authorization": authorization,
    }
    if include_content_sha256_header:
        final_headers["X-Amz-Content-Sha256"] = payload_sha256
    skip_keys = {"host", "x-amz-date"}
    if include_content_sha256_header:
        skip_keys.add("x-amz-content-sha256")
    for key, value in request_headers.items():
        if key in skip_keys:
            continue
        final_headers[key] = value

    return SignedRequest(
        headers=final_headers,
        signature=signature,
        canonical_request=canonical_request,
        string_to_sign=string_to_sign,
    )


def derive_signing_key(
    *,
    secret_access_key: str,
    date_stamp: str,
    region: str,
    service: str,
) -> bytes:
    """Derive the SigV4 signing key for one (date, region, service).

    Exposed so a future caller (e.g., a presigner) can derive the
    key once and sign many requests under the same scope without
    re-deriving. The S3 adapter does not use this surface today
    — :func:`sign_request` re-derives per call to keep the API
    surface tiny — but the function is the natural extension
    point if presigned-URL support ever lands.
    """

    k_secret = ("AWS4" + secret_access_key).encode("utf-8")
    k_date = hmac.new(k_secret, date_stamp.encode("utf-8"), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, _REQUEST_TYPE.encode("utf-8"), hashlib.sha256).digest()


def _canonical_uri_for_s3(path: str) -> str:
    """Return the canonical URI segment for SigV4.

    S3 keeps path segments unmerged (per AWS docs §"Create a
    canonical request" — S3 is one of two services that does NOT
    normalize successive ``//`` collapses). Each path segment is
    URL-encoded with the unreserved-path set; ``/`` itself is not
    escaped because it separates segments.
    """

    if not path:
        return "/"
    return quote(path, safe=_UNRESERVED_PATH)


def _canonical_query_string(raw_query: str) -> str:
    """Return the canonical query string for SigV4.

    Sorts pairs by URL-encoded key (then by URL-encoded value for
    ties), URL-encodes both sides per the unreserved-query set,
    joins with ``&`` and ``=``. Empty query string → empty
    output (one empty line in the canonical request).
    """

    if not raw_query:
        return ""
    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(raw_query, keep_blank_values=True):
        pairs.append((quote(key, safe=_UNRESERVED_QUERY), quote(value, safe=_UNRESERVED_QUERY)))
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """Return the canonical headers block + the signed-headers list.

    SigV4 lower-cases header names, trims surrounding whitespace
    on values, collapses internal whitespace runs in unquoted
    values to single spaces, and sorts by lowercase header name.
    Values are joined with newlines as ``name:value\\n``; the
    signed-headers list is the lowercase names joined by ``;``.

    The signer always signs every header the caller passes plus
    the three SigV4 mandatory headers (host, x-amz-date,
    x-amz-content-sha256). A future presigner that needs partial
    header signing opens its own surface.
    """

    normalized: dict[str, str] = {
        name.lower(): _normalize_header_value(value) for name, value in headers.items()
    }
    sorted_names = sorted(normalized)
    canonical = "".join(f"{name}:{normalized[name]}\n" for name in sorted_names)
    signed = ";".join(sorted_names)
    return canonical, signed


def _normalize_header_value(value: str) -> str:
    """Trim surrounding whitespace and collapse internal runs."""

    return " ".join(value.split())


__all__ = [
    "InvalidSigV4InputError",
    "SigV4Error",
    "SignedRequest",
    "derive_signing_key",
    "sign_request",
]
