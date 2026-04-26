# ADR 0006 — Ollama as default model runtime

**Status:** Superseded by [ADR-0014](0014-onnx-only-stack.md)
**Date:** April 2026
**Deciders:** Tom Mathews

> **Superseded 2026-04-27.** Tessera v0.3 dropped Ollama as a runtime entirely
> and moved both embedder and reranker into the daemon process via fastembed
> (ONNX Runtime). The historical context below is preserved for archaeology;
> the operational defaults documented here no longer apply. See ADR-0014 for
> the current stack and the rationale for the switch.

## Context

The retrieval pipeline calls three model slots: embedder, extractor (optional, for metadata enrichment), reranker. Each needs a runtime. Candidates:

| Runtime | Local | Models available | Install friction | Model management |
|---|---|---|---|---|
| Ollama | Yes | Llama, Qwen, Mistral, Nomic, BGE, Phi, many more | Moderate (one installer) | Built-in pull/list/remove |
| llama.cpp (raw) | Yes | Any GGUF | Hard (build from source or use pre-built binary + model files manually) | Manual |
| sentence-transformers (Python in-process) | Yes | HuggingFace embedding / cross-encoder models | Low (pip dependency) | Via HF cache |
| OpenAI API | No | GPT, text-embedding-3 | Low | Cloud |
| Voyage, Cohere APIs | No | voyage-3, rerank-3 | Low | Cloud |
| vLLM, TGI, etc. | Yes | Full HF model set | High (server + GPU config) | Manual |

## Decision

**Ollama is the default runtime for embedder and extractor slots.** `sentence-transformers` is the default runtime for the reranker slot (cross-encoder). Cloud providers (OpenAI, Voyage, Cohere) are supported via the adapter framework but are opt-in, never required.

All-local mode (Ollama + sentence-transformers, no cloud keys) is a tested, supported configuration in every release.

## Rationale

1. **All-local mode must be a first-class path.** The product's positioning hinges on the substrate being swappable to local models. If the default requires cloud keys, the claim is rhetorical. Ollama makes local the easy path.
2. **Model management without per-user scripts.** Ollama handles download, cache, versioning, and concurrent loading. Rolling our own model management on top of llama.cpp would triple the v0.1 surface area for no user-visible gain.
3. **Embedding and generation in one runtime.** Ollama serves both; the extractor slot (which may call a small instruction-tuned model for metadata extraction) shares infrastructure with the embedder. One localhost service, not two.
4. **Cross-encoder reranking fits sentence-transformers better than Ollama.** Reranker models (e.g., `cross-encoder/ms-marco-MiniLM-L-6-v2`) are small, CPU-capable, and loaded into a Python process naturally. Using Ollama for rerank would force a model-server round-trip per candidate, slowing recall significantly. Two runtimes is the right trade.
5. **Provider lock-in rejection.** Defaulting to OpenAI embeddings means every user's vault has OpenAI-shaped embedding vectors. Switching becomes a full re-embed. Defaulting to a local model puts the switch cost on the minority who opt into cloud, not the majority who stay local.

## Consequences

**Positive:**
- Zero cloud dependency in the default install path.
- Model swap is a CLI command (`tessera models set embedder ollama/bge-m3`), not a code change.
- Ollama users have one fewer service to manage (they likely already run Ollama for other reasons).

**Negative:**
- Ollama itself is a dependency. Users without it need a one-time install. `tessera doctor` detects and advises.
- Ollama is a young project; the API surface has shifted in past versions. Pin to a minimum Ollama version per release.
- sentence-transformers pulls in PyTorch (~2 GB). For the reranker slot, this is the floor. Users on constrained environments can disable reranking explicitly (not recommended; see system-design.md §Retrieval pipeline hard rules).
- First recall after daemon start is slow (cold reranker load). Warm-up documented.

## Adapter framework posture

The three-slot adapter pattern (embedder, extractor, reranker) means no code change is required to swap runtimes. Reference implementations ship for:

- Ollama (embedder, extractor)
- sentence-transformers (reranker, optional embedder)
- OpenAI (all three slots, opt-in)
- Voyage (embedder, reranker, opt-in)
- Cohere (reranker, opt-in)

Third-party adapters are a pluggable decorator-registry. Full details in the forthcoming `docs/model-adapters.md`.

## Alternatives considered

- **Cloud-default (OpenAI)**: Simpler first-run but contradicts local-first positioning. Rejected.
- **llama.cpp raw**: Lower overhead but higher install friction. Rejected for v0.1; may be added as an adapter in v0.3+ if user demand exists.
- **sentence-transformers for embedder too**: Works but pushes Python-process memory pressure onto the daemon. Ollama's out-of-process model keeps daemon memory stable under large models. Rejected for embedder; retained for reranker.
- **vLLM, TGI**: Production-grade but assume GPU + server-management skills. Wrong audience. Rejected.

## Revisit triggers

- Ollama project stalls or fundamentally changes licensing.
- A comparable local runtime with less install friction emerges (e.g., MLX-based runtime with first-party Apple Silicon support and easy model management).
- User evidence shows cross-encoder reranking is too slow on CPU-only configurations and a different architecture (e.g., LLM-as-reranker via Ollama) is required.
