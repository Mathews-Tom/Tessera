# ADR 0008 — Adapter framework scope and registration

**Status:** Partially superseded by [ADR-0014](0014-onnx-only-stack.md)
**Date:** April 2026
**Deciders:** Tom Mathews

> **Partially superseded 2026-04-27.** ADR-0014 narrowed the shipped
> adapter set to a single embedder + reranker pair (fastembed). The
> module-level conventions this ADR established — decorator
> registration, lazy import for unused adapters, narrow error
> taxonomy — survive intact and apply to fastembed. The cloud-adapter
> framework (OpenAI, Voyage, Cohere) referenced below was deleted at
> v0.4; those references read as historical record of the v0.1–v0.3
> framework rather than a live shipping list.

## Context

`docs/system-design.md §Model adapter framework` reserves three slots —
embedder, extractor, reranker — behind a decorator-based registry. Each
could ship with two, three, or a dozen reference implementations. Each
implementation costs an import graph, a dependency, and a threat-model
surface. Several concrete shaping questions:

1. **Which reference adapters ship in v0.1?** The plan enumerates Ollama,
   sentence-transformers, OpenAI, Voyage, Cohere, extractor Ollama. The
   abstraction rule — no abstraction without ≥ 2 concrete call sites today —
   means each slot is only justified if two implementations exist.
2. **Is the extractor slot present in v0.1?** The plan calls out "stubbed
   in P2; full implementation deferred to P3 only if capture needs it."
3. **Auto-discovery vs. explicit import?** A convenience `import tessera`
   could chain-import every adapter module, which drags the cloud adapter
   surface into the all-local deployment.
4. **Error surface.** How much of the provider's failure mode does the
   adapter expose to callers?

## Decision

### v0.1 reference adapters

**Embedder slot ships two references:**
- `tessera.adapters.ollama_embedder.OllamaEmbedder` (all-local default).
- `tessera.adapters.openai_embedder.OpenAIEmbedder` (opt-in cloud).

**Reranker slot ships two references:**
- `tessera.adapters.st_reranker.SentenceTransformersReranker` (in-process
  cross-encoder, all-local default).
- `tessera.adapters.cohere_reranker.CohereReranker` (opt-in cloud).

**Extractor slot is not present in v0.1.** The protocol is deferred to P3
when the capture pipeline needs it. Introducing the protocol today creates
an abstraction with zero concrete call sites, which the project's no-
bullshit bar rejects. P3 adds the protocol alongside its first real user.

**Voyage is not shipped in v0.1.** The plan lists Voyage as a stretch
target. At two implementations per slot the abstraction bar is satisfied;
adding a third for completeness is speculative. v0.3 or later revisits
based on user request.

### Explicit-import registration

Importing `tessera.adapters` does **not** import individual adapter
modules. Each adapter module registers its name via decorator as an
import-time side effect; the caller must `import
tessera.adapters.openai_embedder` to make ``"openai"`` resolvable via
`get_embedder_class("openai")`.

This yields two properties:

- An all-local deployment that only imports `ollama_embedder` and
  `st_reranker` never loads the OpenAI or Cohere modules. The transitive
  surface of `httpx` clients pointed at cloud hosts is absent from the
  process entirely.
- A misconfiguration that names an adapter never imported produces a
  clean `UnknownAdapterError` rather than a best-effort fall-through.

### Error classification at the adapter boundary

Adapters do not raise raw `httpx.HTTPError` or `torch.RuntimeError`.
Errors are translated into a closed taxonomy:

- `AdapterNetworkError` — connection refused, timeout, DNS, 5xx.
- `AdapterModelNotFoundError` — 404 on the configured model.
- `AdapterOOMError` — provider-reported resource exhaustion, including
  429 rate-limit (OpenAI, Cohere).
- `AdapterAuthError` — 401 / 403 on credentialed adapters.
- `AdapterResponseError` — schema mismatch, dim drift, malformed body.

The P3 embed worker branches its retry policy on the error class per
`docs/system-design.md §Failure taxonomy`. Collapsing these into a single
`AdapterError` would force the worker to parse exception messages, which
is a classic silent-failure vector.

### Retry policy lives outside adapters

Adapters make one HTTP call and classify on failure. Exponential backoff,
the "`ollama pull` then retry" recovery, and the degraded-mode fallback
live in the P3 embed worker (for embedders) and the P4 retrieval pipeline
(for rerankers). Embedding retry logic inside each adapter would duplicate
it across providers and make the retry strategy untestable at the level
that actually matters.

### Key storage

Cloud adapters load API keys from the OS keyring only. Environment
variables are never consulted. Config files never store the key itself,
only a keyring handle. The `KeyringUnavailableError` is surfaced to the
caller so that a misconfigured keyring fails loudly rather than silently
falling back to no auth.

## Consequences

**Positive:**
- All-local deployments do not import cloud adapter code. Verified by CI
  grep and the no-outbound network test that lands alongside the adapter
  framework in P2.
- Retry policy and classification are testable independently.
- The extractor slot is added when its first real user exists, not
  earlier. One less speculative abstraction in the v0.1 surface.

**Negative:**
- Users who want a third-party embedder must import it explicitly in
  their code. A provided `tessera.adapters.all` module could chain-import
  every shipped adapter for convenience; that is a v0.3+ consideration if
  real users request it.
- Each adapter module carries its own error classification boilerplate.
  A helper module could collapse this but the duplication is small and
  the explicit per-adapter shape reads more clearly.

## Alternatives considered

- **Auto-discovery via entrypoints.** Python packaging supports adapter
  discovery via `[project.entry-points]`. This would let third-party
  packages register adapters without modifying the import graph. Deferred
  to v0.3+ when a third-party adapter actually exists.
- **Abstract base class (ABC) instead of Protocol.** ABC gives inheritance
  and enforced signatures. Protocol gives structural typing without a
  required base class. Protocol won: adapters are thin data-plus-method
  shapes with no shared behaviour; inheritance would encourage sharing
  that does not exist.
- **Per-adapter retry baked into the adapter.** Simpler for one-off
  callers but duplicates the retry ladder across providers. Rejected.
- **Single `AdapterError` with error-code enum.** Smaller surface but
  forces the caller to branch on strings or enum values that the type
  checker does not help with. Rejected in favour of exception subclasses.

## Revisit triggers

- A third adapter per slot lands, which would justify a shared base class
  or helper module for error-classification boilerplate.
- A user reports that the explicit-import model is too frictional for
  their configuration-driven deployment (pushes us toward entry-points).
- The P3 embed worker's retry ladder grows complex enough that some of
  it should be pushed into the adapter layer.
