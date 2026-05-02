# ADR 0022 — BYO sync transport: S3 adapter, CLI surface, watermark persistence

**Status:** Accepted
**Date:** May 2026
**Deciders:** Tom Mathews
**Related:** [ADR 0021](0021-audit-chain-tamper-evidence.md), `docs/threat-model.md` §S6 (Sync backend), `docs/release-spec.md` §v0.5 (BYO cloud sync), `.docs/v0.5-handoff-2026-05-02.md` §V0.5-P9b
**Supersedes:** none

## Context

V0.5-P9 part 1 (PR #65) shipped the storage primitives behind BYO sync: a `BlobStore` protocol, a `LocalFilesystemStore` implementation, AES-256-GCM envelope encryption, and an HMAC-SHA256-signed monotonic manifest. The release-spec §v0.5 commitment is a round-trip through an S3-compatible bucket — filesystem-only is not the v0.5 ship. V0.5-P9b closes the gap with three concrete deliverables:

1. An S3-compatible adapter conforming to the `BlobStore` protocol so `push` and `pull` work against any S3-API endpoint (Backblaze B2, Tigris, Cloudflare R2, Wasabi, MinIO, AWS S3 itself).
2. A `tessera sync` CLI surface so an operator drives push / pull / status without writing Python.
3. A persisted watermark so the CLI's `pull` does not re-fetch the same snapshot on every invocation and so the replay-defence invariant survives across process restarts.

Three load-bearing decisions need locking down before code lands. Each closes off an alternative a reasonable reader would pick; without an ADR they get relitigated in code review.

## Decision

### D1 — Hand-rolled AWS SigV4 over httpx, not aioboto3

The S3 adapter signs requests with a hand-rolled AWS Signature Version 4 implementation in `src/tessera/sync/_sigv4.py`, sending the signed requests through the project's existing `httpx` dependency. The adapter does **not** depend on `boto3` / `botocore` / `aioboto3`.

**Why:** the protocol surface the adapter touches is small — four verbs (PUT object, GET object, LIST objects with prefix, HEAD object) — and the sign-then-send loop has been published as test vectors by AWS for over a decade. SigV4 in pure Python is roughly 200 LOC; pulling in `aioboto3` adds `boto3` + `botocore` + `s3transfer` + `jmespath` (tens of MB of transitive code) for the same four verbs. The dependency cost is wildly out of proportion to what we'd actually use, and every byte of transitive code is a future maintenance + supply-chain surface. Tessera's no-bullshit-code stance on optional imports + closed dependency surfaces argues for the hand-roll. CI runs the published AWS SigV4 test vectors as a determinism gate (mirrors `audit-chain-determinism`).

### D2 — `BlobStore` protocol contract is unchanged; S3 adapter conforms

`tessera/sync/s3.py` implements the existing `BlobStore` protocol from `tessera/sync/storage.py` without modification. The same exception surface applies: `BlobNotFoundError`, `ManifestNotFoundError`, `BlobStoreError`. The S3 adapter inherits-by-protocol every crypto, manifest, and round-trip invariant `LocalFilesystemStore` already exercises. The existing test suite is re-parametrized to run against both backends.

**Why:** the protocol was designed in V0.5-P9 part 1 with this exact extension in mind. Re-parametrizing tests across both backends turns the V0.5-P9 part 1 test corpus into a regression suite for the S3 adapter, free of charge. A divergent S3-only contract would split the test corpus and create two near-identical-but-subtly-different code paths — exactly the surface where bugs hide between layers.

### D3 — Watermark persists in `_meta`, keyed by store identity

The `last_restored_sequence` watermark — the replay-defence input — persists as a row in the existing `_meta` table, keyed by a stable hash of `endpoint || bucket || prefix`. The watermark lives inside the encrypted vault. It survives credential rotation (the credentials are not part of the key). It resets on a bucket-change because a different bucket means a different sync target and the new target's history has its own monotonic sequence.

**Why:** option (a) over option (b) (sidecar file). The watermark is per-vault-per-store state; encrypting it under SQLCipher costs nothing. A sidecar file would survive a vault restoration that the watermark-in-`_meta` would reset — but resetting on restore is the *correct* semantics: you just restored from sequence N, and the next pull from the same store should refuse to overwrite that with anything ≤ N. A sidecar that retained the pre-restore watermark would refuse the same legitimate restore-then-pull flow.

The store-identity hash uses `endpoint || bucket || prefix` (not credentials) so rotating an access key against the same bucket continues against the same watermark. Rotating the bucket — actual store change — produces a new `store_id` and starts a fresh watermark, which matches operator intuition.

### D4 — `tessera sync` CLI surface

Five subcommands:

| Subcommand | Action |
|---|---|
| `tessera sync setup` | Interactive: prompts bucket / endpoint / region / access-key / secret-key / prefix. Stores credentials under `tessera-sync-<store_id>` in the OS keyring. Stores non-secret config (endpoint, bucket, prefix, region) in vault `_meta`. |
| `tessera sync status` | Reports configured store, last manifest sequence on the store, local watermark, store reachability (one HEAD on the bucket — does not pull). |
| `tessera sync push` | Calls `tessera.sync.push.push` with the configured `S3BlobStore`. Returns sequence + bytes-uploaded. |
| `tessera sync pull` | Calls `tessera.sync.pull.pull` with `last_restored_sequence` from `_meta`. Updates the watermark on success. `--target` overrides target path for restore-to-different-location flows. |
| `tessera sync conflicts` | Lists `1 (conflicted copy).json`-style files that the V0.5-P9 part 1 `_iter_manifest_sequences` warning surfaces. Filesystem-store specific; S3 stores raise on encounter. |

The CLI does not implement multi-device row-merge. That is V0.5-P9c (see §Out of scope).

### D5 — `src/tessera/sync/s3.py` is the only new outbound surface

The `no-telemetry-grep` CI gate's allowlist gains exactly one entry: `src/tessera/sync/s3.py`. The boundary statement from ADR 0019 / 0020 / 0021 extends:

> Tessera stores; the caller-configured BlobStore receives. No outbound calls beyond the configured BYO sync target.

Any future feature wanting outbound-by-default opens its own ADR.

## Rationale

1. **Dependency hygiene is load-bearing on solo-dev velocity.** Every transitive dep is a future supply-chain audit, a future CVE, a future build-break. SigV4 in `httpx` is ~200 LOC of pure Python that runs unchanged for a decade; `aioboto3` brings tens of MB that change weekly. The cost asymmetry is severe.
2. **CI gate against AWS test vectors closes the hand-roll's main risk.** The single failure mode for a hand-rolled signer is silent signature drift across `boto3` / SigV4 implementations. Pinning byte-identical signatures against the AWS-published vectors on every PR catches drift before it ships. Same pattern as `audit-chain-determinism` for `canonical_json`.
3. **Protocol-conformant S3 adapter inherits the V0.5-P9 part 1 test corpus.** Twelve security and round-trip tests re-run for free. The S3-specific test additions are then narrow: SigV4 vectors, fake-transport plumbing, and S3-specific marker-object semantics (empty bucket vs. missing bucket).
4. **Watermark-in-`_meta` keeps watermark reset semantics correct.** A sidecar would create the "old watermark survives a fresh restore" surface that produces a false replay-rejection on legitimate flows. Encrypting under SQLCipher is zero-cost defence-in-depth.
5. **Store-identity hash excludes credentials so credential rotation does not start over.** Operators rotate keys regularly; restarting the watermark every rotation produces a one-pull regression each time. The fix is to key on what defines the *store* (endpoint + bucket + prefix), not on the *credential*.
6. **Five-subcommand CLI is the minimum useful surface.** Setup, status, push, pull, conflicts. No `delete-store`, no `prune-old`, no `verify-store`, no `force-pull`. Each absent verb is a feature whose absence is the right v0.5 default. Pruning history is a v1.0 multi-device concern; the verify path is `tessera audit verify` after pull.
7. **Boundary statement extension is the load-bearing security commitment.** Adding `src/tessera/sync/s3.py` to the no-telemetry allowlist is a hard line that says "this is the only new outbound surface; everything else still has zero outbound calls." The grep gate enforces it on every PR.

## Out of scope (explicitly deferred)

These are not blocking V0.5-P9b. They are documented here so a future reader can see the bounds at decision time.

1. **Multi-device row-merge (V0.5-P9c).** Append-on-conflict for facets dedup'd by `content_hash`; manual merge for entities. V0.5-P9b ships snapshot-only sync per release-spec §v0.5 DoD ("vault → bucket → restore on second machine → identical state"). Snapshot semantics satisfy the DoD; row-merge is a multi-device concern that needs real-user signal to scope correctly.
2. **Server-side encryption opt-out.** Per §S6, the envelope-encrypted blobs make S3 SSE redundant — the provider sees only AES-256-GCM ciphertext under the DEK. The adapter passes through whatever the bucket's default-encryption setting is; we do not request `x-amz-server-side-encryption: AES256` because it would advertise to the provider that we expect them to encrypt (we don't — we already did).
3. **Bucket lifecycle / retention policies.** Operators set these in the AWS console / equivalent. Tessera does not configure or enforce lifecycle rules on the bucket; that would require IAM `s3:PutBucketLifecycle` privileges most users won't grant.
4. **Concurrent push from multiple hosts.** V0.5-P9 part 1 assumed single-writer-per-vault (the existing v0.1 invariant). V0.5-P9b inherits this. Two hosts pushing to the same bucket simultaneously is a footgun on snapshot-based sync; the row-merge work in V0.5-P9c is where we'd address it properly.
5. **Resume-on-failure / multipart upload.** Vaults at the v0.5 dogfood scale (Tom's vault is sub-100MB) fit in a single PUT. Multipart upload is a v1.0 concern when vaults grow past the 5GB single-object limit or when network conditions make resumable uploads worth the complexity.

## Consequences

**Positive:**
- The v0.5 release-spec DoD bullet "vault → S3-compatible bucket → restore on second machine → identical state" is achievable.
- Dependency surface stays narrow. The largest single dep we've avoided is `aioboto3 → boto3 → botocore`; the second-largest is `aiohttp` (we use httpx).
- Test corpus from V0.5-P9 part 1 becomes the regression suite for the S3 adapter. New tests are scoped to S3-specific concerns.
- The CLI is the documented operator surface; future GUI work in v1.0 can wrap the same CLI verbs.
- The watermark resets on legitimate vault-restore flows and survives credential rotation. Both behaviours match operator intuition.

**Negative:**
- Hand-rolled SigV4 needs ongoing maintenance. The AWS spec changes rarely, but when it does (e.g., SigV4A for multi-region access points) the project owns the upgrade. Documented in `tessera/sync/_sigv4.py` module docstring with a pointer to AWS's SigV4 reference.
- The CI vector test asserts byte-identical signatures against published AWS vectors. A change to the canonicalization, hashing, or encoding will surface here loudly. That is the intended behaviour.
- `tessera sync setup` writes credentials to the OS keyring under one entry per store. Users with keyring backends that don't support multiple entries per service (rare) will need to re-enter credentials or set a single store.
- The `_meta` watermark is encrypted under the vault key. Operators who lose the vault key lose the watermark, which means the next pull starts from sequence 0 — replays are accepted up to whatever was last on the store. This matches the real semantics: losing the vault key is total loss.
- Snapshot-based sync means simultaneous captures on two devices collapse to one, even with V0.5-P9b shipped. The release-spec DoD names this; row-merge is V0.5-P9c.

## Alternatives considered

- **`aioboto3` as the S3 client.** Rejected per D1. The transitive-dep cost is an order of magnitude more than the SigV4-over-httpx hand-roll for the same four verbs.
- **`boto3` (sync) as the S3 client.** Same rejection as `aioboto3` plus the impedance mismatch with the existing async daemon.
- **A separate `sync_meta.json` sidecar for the watermark.** Rejected per D3. The "old watermark survives fresh restore" semantics produce false replay rejections on legitimate restore-then-pull flows.
- **Watermark in OS keyring.** Rejected. Watermarks are per-vault state, not per-credential. Putting them in the keyring would survive vault-recreation in ways that mask data loss; putting them in `_meta` resets correctly.
- **Watermark keyed by credentials** (so rotating keys starts a fresh watermark). Rejected. Operators rotate keys regularly; restarting the watermark each time produces a one-pull regression every rotation. The store identity is the *target*, not the *credential*.
- **`tessera sync prune` subcommand to delete old manifests.** Deferred. Pruning history on snapshot-based sync risks losing the only restore point on a partial-failure window. V1.0 may add it with a `--keep-last N` policy; V0.5-P9b ships without it.
- **`moto` (S3 mock library) for tests.** Considered. The test surface is small enough that a hand-rolled in-process fake conforming to the same `BlobStore` protocol is simpler and adds no new dependency. We use the fake; `moto` becomes optional if a future test needs deeper S3 emulation.
- **HTTP/3 / QUIC client.** Out of scope. `httpx` over HTTP/1.1 is sufficient for the snapshot-based push/pull pattern; switching transports is invisible to the `BlobStore` protocol if a future need surfaces.
