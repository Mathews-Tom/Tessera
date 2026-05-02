"""S3BlobStore unit tests against an in-process fake S3 transport.

Per ADR-0022 §Alternatives considered: ``moto`` was rejected as a
new dependency because the test surface is small enough that a
hand-rolled in-process fake conforming to the real S3 wire
contract is simpler and adds no transitive deps. The fake
implements just the verbs the adapter calls (HEAD bucket, PUT
object, GET object, LIST objects v2) over ``httpx.MockTransport``.

Coverage philosophy: every public method on :class:`S3BlobStore`
gets at least one round-trip test plus at least one failure-mode
test. The full V0.5-P9 part 1 round-trip integration suite re-runs
against this backend in a separate test module so the protocol-
conformance contract is verified end-to-end.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable

import httpx
import pytest

from tessera.sync.s3 import (
    S3BlobStore,
    S3BlobStoreError,
    S3BucketUnreachableError,
    S3Config,
    S3RequestError,
)
from tessera.sync.storage import BlobNotFoundError, ManifestNotFoundError


def _config(**overrides: object) -> S3Config:
    base: dict[str, object] = {
        "endpoint": "https://s3.us-east-1.amazonaws.com",
        "bucket": "tessera-test-bucket",
        "region": "us-east-1",
        "access_key_id": "AKIDEXAMPLE",
        "secret_access_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "prefix": "",
    }
    base.update(overrides)
    return S3Config(**base)  # type: ignore[arg-type]


class _FakeS3Backend:
    """In-memory S3 fake. Speaks the real wire contract for the
    four verbs the S3 adapter uses."""

    def __init__(self) -> None:
        # bucket → key → bytes. None means the bucket exists but
        # holds no keys; missing bucket means HEAD/LIST return 404.
        self.buckets: dict[str, dict[str, bytes]] = {}
        self.requests: list[httpx.Request] = []

    def add_bucket(self, name: str) -> None:
        self.buckets[name] = {}

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def _handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path
            # Path: /<bucket>[/<key>]
            parts = path.lstrip("/").split("/", 1)
            bucket = parts[0]
            key = parts[1] if len(parts) > 1 else ""
            if bucket not in self.buckets:
                return httpx.Response(404, text="<Error>NoSuchBucket</Error>")
            store = self.buckets[bucket]
            if request.method == "HEAD" and not key:
                return httpx.Response(200)
            if request.method == "GET" and not key:
                return self._list(store, request)
            if request.method == "PUT":
                store[key] = request.content
                return httpx.Response(200)
            if request.method == "GET":
                if key not in store:
                    return httpx.Response(404, text="<Error>NoSuchKey</Error>")
                return httpx.Response(200, content=store[key])
            return httpx.Response(405, text=f"unsupported method {request.method}")

        return _handle

    @staticmethod
    def _list(store: dict[str, bytes], request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        prefix = params.get("prefix", "")
        max_keys = int(params.get("max-keys", "1000"))
        continuation = params.get("continuation-token")
        matching = sorted(k for k in store if k.startswith(prefix))
        if continuation is not None:
            matching = [k for k in matching if k > continuation]
        page = matching[:max_keys]
        is_truncated = len(matching) > max_keys
        next_token = page[-1] if (is_truncated and page) else None
        body = _build_list_xml(page, is_truncated=is_truncated, next_token=next_token)
        return httpx.Response(200, content=body)


def _build_list_xml(keys: list[str], *, is_truncated: bool, next_token: str | None) -> bytes:
    ns = "http://s3.amazonaws.com/doc/2006-03-01/"
    root = ET.Element(f"{{{ns}}}ListBucketResult")
    truncated_elem = ET.SubElement(root, f"{{{ns}}}IsTruncated")
    truncated_elem.text = "true" if is_truncated else "false"
    for key in keys:
        contents = ET.SubElement(root, f"{{{ns}}}Contents")
        key_elem = ET.SubElement(contents, f"{{{ns}}}Key")
        key_elem.text = key
    if is_truncated and next_token is not None:
        token_elem = ET.SubElement(root, f"{{{ns}}}NextContinuationToken")
        token_elem.text = next_token
    return bytes(ET.tostring(root, encoding="utf-8"))


@pytest.fixture
def backend() -> _FakeS3Backend:
    fake = _FakeS3Backend()
    fake.add_bucket("tessera-test-bucket")
    return fake


@pytest.fixture
def store(backend: _FakeS3Backend) -> S3BlobStore:
    transport = httpx.MockTransport(backend.handler())
    return S3BlobStore(_config(), transport=transport)


@pytest.mark.unit
def test_initialize_against_existing_bucket(store: S3BlobStore) -> None:
    store.initialize()


@pytest.mark.unit
def test_initialize_against_missing_bucket_raises_unreachable() -> None:
    backend = _FakeS3Backend()  # No buckets added.
    transport = httpx.MockTransport(backend.handler())
    with (
        S3BlobStore(_config(), transport=transport) as store,
        pytest.raises(S3BucketUnreachableError, match="check bucket name"),
    ):
        store.initialize()


@pytest.mark.unit
def test_blob_round_trip(store: S3BlobStore) -> None:
    blob_id = "a" * 64
    payload = b"the encrypted payload"
    store.put_blob(blob_id, payload)
    assert store.get_blob(blob_id) == payload


@pytest.mark.unit
def test_get_missing_blob_raises_blob_not_found(store: S3BlobStore) -> None:
    with pytest.raises(BlobNotFoundError):
        store.get_blob("0" * 64)


@pytest.mark.unit
def test_manifest_round_trip(store: S3BlobStore) -> None:
    raw = b'{"manifest_version": 1}'
    store.put_manifest(7, raw)
    assert store.get_manifest(7) == raw


@pytest.mark.unit
def test_get_missing_manifest_raises_manifest_not_found(store: S3BlobStore) -> None:
    with pytest.raises(ManifestNotFoundError):
        store.get_manifest(99)


@pytest.mark.unit
def test_list_manifest_sequences_returns_sorted(store: S3BlobStore) -> None:
    for seq in (3, 1, 5, 2, 4):
        store.put_manifest(seq, b"x")
    assert store.list_manifest_sequences() == [1, 2, 3, 4, 5]
    assert store.latest_manifest_sequence() == 5


@pytest.mark.unit
def test_list_manifest_sequences_empty(store: S3BlobStore) -> None:
    assert store.list_manifest_sequences() == []
    assert store.latest_manifest_sequence() is None


@pytest.mark.unit
def test_list_under_missing_bucket_raises_unreachable() -> None:
    backend = _FakeS3Backend()  # No buckets added.
    transport = httpx.MockTransport(backend.handler())
    with (
        S3BlobStore(_config(), transport=transport) as store,
        pytest.raises(S3BucketUnreachableError, match="LIST under prefix"),
    ):
        store.list_manifest_sequences()


@pytest.mark.unit
def test_blob_id_path_traversal_rejected(store: S3BlobStore) -> None:
    """Path-traversal in blob_id must fail at the boundary, not
    leak past the prefix into another object's namespace."""

    for malicious in ("", "../escape", "with/slash", "..\\windows"):
        with pytest.raises(S3BlobStoreError, match="path-unsafe"):
            store.put_blob(malicious, b"x")


@pytest.mark.unit
def test_put_blob_uses_path_style_url(store: S3BlobStore, backend: _FakeS3Backend) -> None:
    """The PUT request URL must be path-style under the configured
    bucket, not virtual-hosted-style. Verifies the URL construction
    contract from ADR-0022 (path-style for max compatibility)."""

    store.put_blob("a" * 64, b"payload")
    put_request = next(r for r in backend.requests if r.method == "PUT")
    assert put_request.url.path == "/tessera-test-bucket/blobs/" + "a" * 64
    assert "tessera-test-bucket" not in put_request.url.host


@pytest.mark.unit
def test_prefix_is_applied_to_object_keys(backend: _FakeS3Backend) -> None:
    """A non-empty prefix wraps the layout so multiple vaults can
    share one bucket. The key under S3 becomes
    ``<prefix>/blobs/<id>`` rather than ``blobs/<id>``."""

    transport = httpx.MockTransport(backend.handler())
    with S3BlobStore(_config(prefix="vault-A"), transport=transport) as store:
        store.put_blob("a" * 64, b"x")
    put_request = next(r for r in backend.requests if r.method == "PUT")
    assert put_request.url.path == "/tessera-test-bucket/vault-A/blobs/" + "a" * 64


@pytest.mark.unit
def test_prefix_is_normalized(backend: _FakeS3Backend) -> None:
    """Leading and trailing slashes in the configured prefix
    collapse so ``/vault-A/`` and ``vault-A`` produce the same
    object keys. Avoids a class of operator-typo bugs where a
    misformed prefix splits one vault's data across two paths."""

    transport_a = httpx.MockTransport(backend.handler())
    transport_b = httpx.MockTransport(backend.handler())
    with (
        S3BlobStore(_config(prefix="/vault-A/"), transport=transport_a) as store_a,
        S3BlobStore(_config(prefix="vault-A"), transport=transport_b) as store_b,
    ):
        store_a.put_blob("a" * 64, b"x")
        store_b.put_blob("a" * 64, b"x")
    put_a = next(r for r in backend.requests if r.method == "PUT")
    put_b_index = next(
        i for i, r in enumerate(backend.requests) if r.method == "PUT" and r is not put_a
    )
    assert put_a.url.path == backend.requests[put_b_index].url.path


@pytest.mark.unit
def test_pagination_follows_continuation_token() -> None:
    """LIST returns at most MAX_KEYS keys per page; the adapter
    must follow ``NextContinuationToken`` until the response
    reports ``IsTruncated=false``. Plant 3 manifests with a
    backend forced to page at 1 key per response."""

    class _PaginatingBackend(_FakeS3Backend):
        @staticmethod
        def _list(store: dict[str, bytes], request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            prefix = params.get("prefix", "")
            continuation = params.get("continuation-token")
            matching = sorted(k for k in store if k.startswith(prefix))
            if continuation is not None:
                matching = [k for k in matching if k > continuation]
            page = matching[:1]  # Force page size 1.
            is_truncated = len(matching) > 1
            next_token = page[-1] if (is_truncated and page) else None
            body = _build_list_xml(page, is_truncated=is_truncated, next_token=next_token)
            return httpx.Response(200, content=body)

    backend = _PaginatingBackend()
    backend.add_bucket("tessera-test-bucket")
    transport = httpx.MockTransport(backend.handler())
    with S3BlobStore(_config(), transport=transport) as store:
        for seq in (1, 2, 3):
            store.put_manifest(seq, b"x")
        assert store.list_manifest_sequences() == [1, 2, 3]


@pytest.mark.parametrize(
    ("verb_name", "verb_op"),
    [
        ("put_blob", lambda store: store.put_blob("a" * 64, b"x")),
        ("get_blob", lambda store: store.get_blob("a" * 64)),
        ("put_manifest", lambda store: store.put_manifest(1, b"{}")),
        ("get_manifest", lambda store: store.get_manifest(1)),
        ("list", lambda store: store.list_manifest_sequences()),
    ],
)
def test_unexpected_5xx_status_surfaces_request_error(verb_name: str, verb_op: object) -> None:
    """Every public verb must surface S3RequestError on an
    unexpected 5xx (throttling, server error). Previously only
    put_blob had a test; a regression that masked errors on any
    other verb (silently returning empty bytes / [] / None) could
    ship green. Parametric coverage so each verb has its own
    failure line."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(_handler)
    with (
        S3BlobStore(_config(), transport=transport) as store,
        pytest.raises(S3RequestError, match="HTTP 503"),
    ):
        verb_op(store)  # type: ignore[operator]


@pytest.mark.unit
def test_initialize_against_5xx_surfaces_request_error() -> None:
    """A 500 from a misconfigured proxy is distinct from a 404
    bucket-missing case. Surface as S3RequestError (not
    S3BucketUnreachableError) so the operator sees the actual
    status code in the message rather than the generic "check
    bucket name + credentials" path."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    transport = httpx.MockTransport(_handler)
    with (
        S3BlobStore(_config(), transport=transport) as store,
        pytest.raises(S3RequestError, match="HTTP 502"),
    ):
        store.initialize()


@pytest.mark.unit
def test_list_with_malformed_xml_raises_request_error() -> None:
    """A LIST response that is not well-formed XML must raise
    rather than silently return [] (which would be indistinguishable
    from "empty bucket"). Closes the silent-failure-hunter H4
    path with a parametric assertion that the parser is wired
    into the boundary."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not <xml>")

    transport = httpx.MockTransport(_handler)
    with (
        S3BlobStore(_config(), transport=transport) as store,
        pytest.raises(S3RequestError, match="not well-formed"),
    ):
        store.list_manifest_sequences()


@pytest.mark.unit
def test_list_with_truncated_response_missing_token_raises() -> None:
    """IsTruncated=true with no NextContinuationToken is a
    malformed response — silently treating the partial result
    as complete would let ``latest_manifest_sequence`` return
    a stale value, breaking replay defence on the next pull."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        body = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<IsTruncated>true</IsTruncated>"
            b"<Contents><Key>manifests/1.json</Key></Contents>"
            b"</ListBucketResult>"
        )
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(_handler)
    with (
        S3BlobStore(_config(), transport=transport) as store,
        pytest.raises(S3RequestError, match="NextContinuationToken"),
    ):
        store.list_manifest_sequences()


@pytest.mark.unit
def test_put_blob_to_missing_bucket_raises_unreachable() -> None:
    """A PUT against a missing bucket returns 404 (the bucket
    itself is missing, not the object — PUT creates objects).
    Surface as S3BucketUnreachableError so the operator gets the
    same "check bucket + creds" message as initialize/list rather
    than a generic status excerpt."""

    backend = _FakeS3Backend()  # No buckets added.
    transport = httpx.MockTransport(backend.handler())
    with (
        S3BlobStore(_config(), transport=transport) as store,
        pytest.raises(S3BucketUnreachableError, match="missing or unreachable"),
    ):
        store.put_blob("a" * 64, b"payload")


@pytest.mark.unit
def test_put_blob_signs_request_with_sigv4_headers(
    store: S3BlobStore, backend: _FakeS3Backend
) -> None:
    """Every outbound request must carry the SigV4 mandatory
    headers: Host, X-Amz-Date, X-Amz-Content-Sha256, Authorization.
    Defensive check that the request reaches the wire fully signed
    rather than the signer being bypassed by a future refactor."""

    store.put_blob("a" * 64, b"payload")
    put_request = next(r for r in backend.requests if r.method == "PUT")
    assert "Authorization" in put_request.headers
    assert put_request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "X-Amz-Date" in put_request.headers
    assert "X-Amz-Content-Sha256" in put_request.headers
    # Empty-payload sentinel must NOT appear when the payload is
    # non-empty — proves the signer hashed the actual bytes.
    assert (
        put_request.headers["X-Amz-Content-Sha256"]
        != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


@pytest.mark.unit
def test_invalid_sequence_number_rejected(store: S3BlobStore) -> None:
    """Sequence 0 / negative is invalid by the manifest contract;
    the S3 store rejects at the boundary the same way
    LocalFilesystemStore does so the protocol shape is uniform."""

    with pytest.raises(S3BlobStoreError, match=">= 1"):
        store.put_manifest(0, b"x")
    with pytest.raises(S3BlobStoreError, match=">= 1"):
        store.get_manifest(-1)


@pytest.mark.unit
def test_close_is_idempotent(store: S3BlobStore) -> None:
    """Calling close twice (or after a context exit) must not raise.
    httpx.Client.close is idempotent; the test guards against a
    future wrapper that adds state that breaks this contract."""

    store.close()
    store.close()
