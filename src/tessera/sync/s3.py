"""S3-compatible BlobStore for V0.5-P9b BYO sync.

Implements the :class:`tessera.sync.storage.BlobStore` protocol
against any S3-API endpoint (Backblaze B2, Tigris, Cloudflare R2,
Wasabi, MinIO, AWS S3 itself). Per ADR-0022 D1 the signer is the
hand-rolled :mod:`tessera.sync._sigv4` module, dispatched through
``httpx`` rather than ``aioboto3``.

Layout under the bucket:

    <prefix>/blobs/<blob_id>           # one encrypted vault payload per push
    <prefix>/manifests/<sequence>.json # one signed manifest per push

Where ``<prefix>`` is configurable per store (allowing one bucket
to host many vaults under distinct prefixes). Path-style URLs are
used unconditionally for maximum compatibility — virtual-hosted-
style requires DNS configuration that many S3-compatible providers
don't fully support.

Per ADR-0022 D5: this module is the only new entry on the
``no-telemetry-grep`` allowlist. Every outbound HTTP request the
adapter emits is to the caller-configured S3 endpoint; no other
host is reachable from this code path.

Empty-bucket vs missing-bucket distinction (per ADR-0022 §Out of
scope, addressing handoff §Other pending follow-ups #5):
- A bucket that exists but has no manifests → :func:`list_manifest_sequences`
  returns ``[]``, :func:`latest_manifest_sequence` returns None.
- A bucket that does not exist or is unreachable →
  :class:`S3BucketUnreachableError`. The CLI's ``status`` and
  ``setup`` commands surface this distinction so the operator
  can tell "sync is configured, no push has happened yet" from
  "sync points at the wrong bucket / credentials are wrong".
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx

from tessera.sync._sigv4 import sign_request
from tessera.sync.storage import (
    BlobNotFoundError,
    BlobStoreError,
    ManifestNotFoundError,
)

_S3_SERVICE: Final[str] = "s3"
_LIST_MAX_KEYS: Final[int] = 1000
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0


class S3BlobStoreError(BlobStoreError):
    """Base class for S3-specific BlobStore failures."""


class S3BucketUnreachableError(S3BlobStoreError):
    """Bucket does not exist, credentials wrong, or endpoint unreachable.

    Distinct from :class:`BlobNotFoundError` (object missing in an
    otherwise-reachable bucket). The CLI surfaces this so an
    operator can distinguish "no push has happened yet" (bucket
    reachable, no manifests) from "sync points at the wrong place"
    (bucket unreachable).
    """


class S3RequestError(S3BlobStoreError):
    """An S3 API call returned an unexpected status.

    Carries the HTTP status and a short body excerpt for the
    operator. The body excerpt is bounded so a multi-MB error
    page from a misconfigured proxy doesn't blow up the log line.
    """


@dataclass(frozen=True, slots=True)
class S3Config:
    """Connection parameters for the S3 endpoint.

    Frozen so a configured store cannot mutate its endpoint mid-
    session. Callers build one ``S3Config`` per store and pass it
    to :class:`S3BlobStore`.

    Fields:
    - ``endpoint``: The base URL for the S3 API. AWS S3 in us-east-1
      is ``https://s3.us-east-1.amazonaws.com``; B2 is
      ``https://s3.us-west-002.backblazeb2.com``; R2 is
      ``https://<account>.r2.cloudflarestorage.com``.
    - ``bucket``: The bucket name. Path-style URLs are used so the
      bucket is the first path segment.
    - ``region``: SigV4 credential-scope region. AWS expects the
      bucket's actual region; alt providers usually accept any
      well-formed region string but documenting per-provider
      conventions matters (B2 maps ``us-west-002`` to a specific
      cluster, R2 always uses ``auto``).
    - ``access_key_id`` / ``secret_access_key``: SigV4 credentials.
      Loaded from the OS keyring by the CLI; passed in directly
      by tests.
    - ``prefix``: Optional path prefix under which the layout
      lives. Empty string means "use the bucket root". Lets one
      bucket host many vaults under distinct prefixes.
    """

    endpoint: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str
    prefix: str = ""


class S3BlobStore:
    """A BlobStore backed by an S3-compatible endpoint.

    Conforms to :class:`tessera.sync.storage.BlobStore`. Same
    exception surface as :class:`tessera.sync.storage.LocalFilesystemStore`
    so the V0.5-P9 part 1 test corpus and the round-trip integration
    tests run unchanged against this backend.

    The store does not buffer or pool connections beyond what
    ``httpx.Client`` does internally. A persistent ``Client`` is
    created at construction and closed by :meth:`close`. Tests
    inject a ``transport`` to redirect HTTP calls into an
    in-process fake; production callers leave it None to use the
    real network.
    """

    def __init__(
        self,
        config: S3Config,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        clock: type[datetime] = datetime,
    ) -> None:
        self._config = config
        self._clock = clock
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        """Close the underlying HTTP client.

        Idempotent. Callers that own the store as a context-managed
        resource use :meth:`__enter__` / :meth:`__exit__`; callers
        constructing the store directly call ``close`` explicitly
        when done.
        """

        self._client.close()

    def __enter__(self) -> S3BlobStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def initialize(self) -> None:
        """Verify the bucket is reachable under the configured creds.

        S3 has no ``mkdir`` — bucket creation is an out-of-band
        operator step. Initialize sends a HEAD request against the
        bucket to confirm reachability + credentials in one round
        trip. A 404 / 403 / connection error surfaces as
        :class:`S3BucketUnreachableError`; the CLI's ``setup`` and
        ``status`` commands rely on this distinction to give the
        operator a useful message rather than letting a wrong-
        endpoint mistake surface only when the first push fails.
        """

        url = self._bucket_url()
        try:
            response = self._signed_request("HEAD", url, payload=b"")
        except httpx.HTTPError as exc:
            raise S3BucketUnreachableError(
                f"bucket {self._config.bucket!r} unreachable at {self._config.endpoint!r}: {exc}"
            ) from exc
        if response.status_code in (404, 403):
            raise S3BucketUnreachableError(
                f"bucket {self._config.bucket!r} returned HTTP "
                f"{response.status_code}; check bucket name + credentials"
            )
        if not 200 <= response.status_code < 300:
            raise S3RequestError(
                f"HEAD bucket returned HTTP {response.status_code}: {_excerpt(response.text)}"
            )

    def put_blob(self, blob_id: str, ciphertext: bytes) -> None:
        _validate_blob_id(blob_id)
        url = self._object_url(self._blob_key(blob_id))
        response = self._signed_request("PUT", url, payload=ciphertext)
        if not 200 <= response.status_code < 300:
            raise S3RequestError(
                f"PUT blob {blob_id!r} returned HTTP {response.status_code}: "
                f"{_excerpt(response.text)}"
            )

    def get_blob(self, blob_id: str) -> bytes:
        _validate_blob_id(blob_id)
        url = self._object_url(self._blob_key(blob_id))
        response = self._signed_request("GET", url, payload=b"")
        if response.status_code == 404:
            raise BlobNotFoundError(f"blob {blob_id!r} not found in bucket {self._config.bucket!r}")
        if not 200 <= response.status_code < 300:
            raise S3RequestError(
                f"GET blob {blob_id!r} returned HTTP {response.status_code}: "
                f"{_excerpt(response.text)}"
            )
        return response.content

    def put_manifest(self, sequence_number: int, raw: bytes) -> None:
        if sequence_number < 1:
            raise S3BlobStoreError(f"sequence_number must be >= 1, got {sequence_number}")
        url = self._object_url(self._manifest_key(sequence_number))
        response = self._signed_request("PUT", url, payload=raw)
        if not 200 <= response.status_code < 300:
            raise S3RequestError(
                f"PUT manifest {sequence_number} returned HTTP "
                f"{response.status_code}: {_excerpt(response.text)}"
            )

    def get_manifest(self, sequence_number: int) -> bytes:
        if sequence_number < 1:
            raise S3BlobStoreError(f"sequence_number must be >= 1, got {sequence_number}")
        url = self._object_url(self._manifest_key(sequence_number))
        response = self._signed_request("GET", url, payload=b"")
        if response.status_code == 404:
            raise ManifestNotFoundError(
                f"manifest sequence {sequence_number} not found in bucket {self._config.bucket!r}"
            )
        if not 200 <= response.status_code < 300:
            raise S3RequestError(
                f"GET manifest {sequence_number} returned HTTP "
                f"{response.status_code}: {_excerpt(response.text)}"
            )
        return response.content

    def list_manifest_sequences(self) -> list[int]:
        sequences: list[int] = []
        for key in self._iter_manifest_keys():
            stem = key.rsplit("/", 1)[-1].removesuffix(".json")
            try:
                sequences.append(int(stem))
            except ValueError:
                # An object whose name happens to fall under the
                # manifests prefix but isn't an integer-named JSON
                # file. Filesystem stores see this from sync-provider
                # artefacts (Dropbox conflict files); the S3 surface
                # would only see this if an operator hand-uploaded
                # something. Skip rather than crash the list, but
                # the operator's ``tessera sync conflicts`` surface
                # will list these so they're not invisible.
                continue
        sequences.sort()
        return sequences

    def latest_manifest_sequence(self) -> int | None:
        sequences = self.list_manifest_sequences()
        return sequences[-1] if sequences else None

    def _iter_manifest_keys(self) -> Iterator[str]:
        prefix = self._manifests_prefix()
        continuation: str | None = None
        while True:
            url = self._list_url(prefix=prefix, continuation_token=continuation)
            response = self._signed_request("GET", url, payload=b"")
            if response.status_code == 404:
                # Bucket missing — distinct from "bucket exists,
                # nothing under prefix" (which returns 200 with
                # empty Contents). Surface the boundary mismatch
                # loudly rather than silently returning [].
                raise S3BucketUnreachableError(
                    f"LIST under prefix {prefix!r} returned 404; "
                    f"bucket {self._config.bucket!r} missing or unreachable"
                )
            if not 200 <= response.status_code < 300:
                raise S3RequestError(
                    f"LIST under prefix {prefix!r} returned HTTP "
                    f"{response.status_code}: {_excerpt(response.text)}"
                )
            keys, continuation = _parse_list_response(response.content)
            yield from keys
            if continuation is None:
                return

    def _signed_request(
        self,
        method: str,
        url: str,
        *,
        payload: bytes,
    ) -> httpx.Response:
        signed = sign_request(
            method=method,
            url=url,
            headers=None,
            payload=payload,
            access_key_id=self._config.access_key_id,
            secret_access_key=self._config.secret_access_key,
            region=self._config.region,
            service=_S3_SERVICE,
            timestamp=self._clock.now(UTC),
        )
        return self._client.request(
            method,
            url,
            headers=signed.headers,
            content=payload,
        )

    def _bucket_url(self) -> str:
        return f"{self._config.endpoint.rstrip('/')}/{self._config.bucket}"

    def _object_url(self, key: str) -> str:
        return f"{self._bucket_url()}/{key}"

    def _list_url(self, *, prefix: str, continuation_token: str | None) -> str:
        # S3 LIST API v2: GET /<bucket>/?list-type=2&prefix=<p>&max-keys=<n>
        # The continuation token from a prior page rides as
        # ?continuation-token=<token> on subsequent pages. The
        # signer URL-encodes pairs in canonical-query-string
        # construction; we URL-encode them here too so the
        # request URL httpx sends matches the canonical form the
        # signer used.
        from urllib.parse import quote

        params = [
            ("list-type", "2"),
            ("max-keys", str(_LIST_MAX_KEYS)),
            ("prefix", prefix),
        ]
        if continuation_token is not None:
            params.append(("continuation-token", continuation_token))
        query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
        return f"{self._bucket_url()}?{query}"

    def _blob_key(self, blob_id: str) -> str:
        prefix = self._normalized_prefix()
        return f"{prefix}blobs/{blob_id}"

    def _manifest_key(self, sequence_number: int) -> str:
        prefix = self._normalized_prefix()
        return f"{prefix}manifests/{sequence_number}.json"

    def _manifests_prefix(self) -> str:
        return f"{self._normalized_prefix()}manifests/"

    def _normalized_prefix(self) -> str:
        prefix = self._config.prefix.strip("/")
        return f"{prefix}/" if prefix else ""


def _validate_blob_id(blob_id: str) -> None:
    """Reject path-traversal attempts at the boundary.

    Mirrors the LocalFilesystemStore boundary check. blob_id is a
    sha256 hex digest in normal use; a malformed one means the
    caller's content-addressing logic broke or someone is
    attempting to escape the prefix. Either way fail loud.
    """

    if not blob_id or "/" in blob_id or ".." in blob_id or "\\" in blob_id:
        raise S3BlobStoreError(f"refusing path-unsafe blob_id {blob_id!r}")


def _parse_list_response(body: bytes) -> tuple[list[str], str | None]:
    """Parse an S3 ListObjectsV2 XML response.

    Returns ``(keys, next_continuation_token)``. The token is None
    when ``IsTruncated`` is false in the response. Pagination is
    transparent to callers of :meth:`S3BlobStore.list_manifest_sequences`.
    """

    root = ET.fromstring(body)
    namespace = _detect_namespace(root)
    keys: list[str] = []
    for elem in root.findall(f"{namespace}Contents"):
        key_elem = elem.find(f"{namespace}Key")
        if key_elem is not None and key_elem.text:
            keys.append(key_elem.text)
    truncated_elem = root.find(f"{namespace}IsTruncated")
    is_truncated = truncated_elem is not None and (truncated_elem.text or "").lower() == "true"
    if not is_truncated:
        return keys, None
    token_elem = root.find(f"{namespace}NextContinuationToken")
    if token_elem is None or not token_elem.text:
        # IsTruncated=true with no token is a malformed response.
        # Stop pagination rather than loop forever; the caller
        # gets the partial result it has and the next push/pull
        # cycle will retry the LIST.
        return keys, None
    return keys, token_elem.text


def _detect_namespace(root: ET.Element) -> str:
    """Return the namespace prefix in Clark-notation form.

    AWS S3 always returns the standard XML namespace; alt providers
    sometimes return the same one, sometimes a shortened form,
    occasionally none. Detect from the root tag rather than
    hard-coding so the parser tolerates the variation.
    """

    if root.tag.startswith("{"):
        end = root.tag.find("}")
        return root.tag[: end + 1]
    return ""


def _excerpt(text: str, *, limit: int = 200) -> str:
    """Bound an error-body excerpt so the log line stays readable.

    Misconfigured proxies and authentication landing pages can
    return multi-KB HTML bodies on what should be a JSON / XML
    error response. Truncate to the first 200 chars + an ellipsis
    so the diagnostic message stays usable in a one-line log.
    """

    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


__all__ = [
    "S3BlobStore",
    "S3BlobStoreError",
    "S3BucketUnreachableError",
    "S3Config",
    "S3RequestError",
]
