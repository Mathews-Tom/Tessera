# ADR 0014 — ONNX-only model stack via fastembed

**Status:** Accepted
**Date:** April 2026
**Deciders:** Tom Mathews

## Context

ADR-0006 picked Ollama as the default model runtime and the in-process `sentence-transformers` cross-encoder as the default reranker. That decision was reasonable at v0.1: Ollama is the local-first lingua franca, the cross-encoder ships in PyTorch, and the per-process HTTP boundary felt like a clean separation of concerns.

Three problems compounded over the v0.1 → v0.3 window:

1. **The torch dependency.** `sentence-transformers` transitively pulls torch (+transformers, +safetensors, +tokenizers, +scipy, +sympy, +numpy, …). Install footprint with the CPU-only torch wheel runs ~600 MB; with CUDA it pushes 2 GB. For a "single-file SQLite vault" project that pitches itself on simplicity, the dependency closure was a contradiction.

2. **Ollama process coupling.** Tessera's embed worker queries Ollama on every embed pass. Ollama serializes GPU model loads per process — a Tessera embed firing during `ollama run llama3:70b` evicts whichever chat model the user had hot, and `OLLAMA_KEEP_ALIVE` resets every time Tessera touches `nomic-embed-text`. The problem isn't throughput; it's that Tessera as a background embed worker constantly disrupts whatever the user is doing in their primary Ollama-using tool.

3. **The "all-local" claim wasn't really.** The bundled adapters included `cohere_reranker` (cloud rerank API) and `openai_embedder` (cloud embedding API). Both registered themselves as switchable alternatives. For a local-first project there was no reason to ship cloud fallbacks; they just expanded the security surface (bearer tokens in keyring) and the README's "all-local" disclaimer.

Candidate responses:

| Option | What changes | Cost | Reversibility |
|---|---|---|---|
| Keep Ollama, add fastembed as opt-in | One more adapter; defaults unchanged | Low | Trivial — `git rm` the adapter |
| Move embedder in-process, keep Ollama as opt-in | Refactor adapter dispatch, demote Ollama | Medium | Hard — need to keep Ollama wiring around |
| Drop Ollama and torch entirely; fastembed only | Delete five adapter files + torch + sentence-transformers; reranker switches to fastembed cross-encoder | Higher one-time; lower ongoing | Hard — need to re-add code if reverted |

## Decision

**fastembed (ONNX Runtime) as the sole adapter for both embedder and reranker.** Ollama, sentence-transformers, openai_embedder, cohere_reranker, and the torch-based `devices.py` device-detection helper are removed entirely. fastembed handles ONNX provider selection (CPU / CoreML / CUDA) internally; no torch dependency anywhere.

Defaults:

- **Embedder:** `nomic-ai/nomic-embed-text-v1.5` (768 dim, ~520 MB on disk; the `-Q` quantised variant at ~130 MB is also accepted as a one-line override). Same model family the previous Ollama-served default used; the underlying weights are identical, only the runtime changed.
- **Reranker:** `Xenova/ms-marco-MiniLM-L-12-v2` (cross-encoder ONNX export, ~130 MB). One step up from the L-6 the torch reranker carried; chosen because the size delta is small and the relevance lift is measurable on MS-MARCO.

The `embedding_models.name` column now stores the fastembed model identifier directly rather than an adapter slot name. The previous indirection — where `name` was the adapter label (`"ollama"`) and the provider model lived behind `TESSERA_OLLAMA_MODEL` — was a v0.1 hack that lost meaning the moment the second adapter never landed.

## Rationale

1. **One ML stack, one cache, one process.** Both the embedder and reranker now use the same `fastembed` package, which uses the same `onnxruntime` package, which downloads to the same `~/.cache/fastembed` directory. The embedder/reranker boundary stops being two separate dependency closures and becomes "two classes that import fastembed."

2. **Install footprint drops by an order of magnitude.** Dependencies removed: `ollama`, `sentence-transformers`, `torch`, `transformers`, `tokenizers`, `safetensors`, `scipy`, `sympy`, `scikit-learn`, `huggingface-hub`. Dependencies added: `fastembed`, `onnxruntime` (transitively). Net change: ~600 MB → ~30 MB on disk for the Python install (model weights still download to the cache on first use; that's data, not code).

3. **No process coupling with the user's other tools.** With the embedder running inside `tesserad`, there is no shared Ollama instance to fight over GPU/VRAM with the user's chat session. The user's `ollama run llama3:70b` and Tessera's embed worker no longer interfere.

4. **Strictly local-first.** Removing `cohere_reranker` and `openai_embedder` aligns the shipped adapter set with the README's "all-local by default" claim. There is no cloud-API code in `src/tessera/adapters/` for an attacker to find a bearer token in.

5. **Zero migration cost.** Tessera has one user (the project author). No vault preserves embeddings produced by the previous Ollama runtime; no adapter switch path needs documenting; no compatibility shim survives. A user upgrading runs `tessera models set --activate` once and the embed worker fills `vec_<id>` over the next few minutes.

## Consequences

**Positive:**
- `pip install tessera-context` now completes in tens of seconds rather than minutes; CI build time drops correspondingly.
- The "all-local" pitch is enforceable by `grep` — there is no networked-API adapter in `src/tessera/adapters/`.
- The daemon supervisor's `_warm_adapters` no longer reaches out to `ollama serve`; first-run-after-install fails with a fastembed cache miss (downloadable) rather than an Ollama-not-running error (requires installing a separate service).
- Reranker quality goes up slightly (L-6 → L-12 cross-encoder).

**Negative:**
- The first daemon start downloads the embedder weights (~520 MB) and the reranker weights (~130 MB). On a slow connection that's a 5–10 minute wait. Mitigation: `tessera models test --name <model>` warms the cache without starting the daemon.
- Hot model swap is no longer possible — under Ollama, `ollama pull <other-model>` followed by `tessera models set --activate` was a hot path. Now switching the embedder model requires the daemon to re-download weights on first use after activation. Acceptable given how rarely the embedder is swapped in practice.
- ONNX inference performance on Apple Silicon is dependent on the CoreML execution provider, which fastembed selects automatically but which can underperform a tuned MPS torch path on certain models. Not a problem for the current default models; flagged here because it's the most likely future bottleneck.

## Boundary with ADR-0006

ADR-0006 (Ollama as default model runtime) is now Superseded. Its rationale was sound at v0.1 — Ollama was a reasonable default when in-process ONNX runtimes were less mature and torch was unavoidable for the reranker anyway. ADR-0014 supersedes it because both premises stopped being true: fastembed matured into a credible drop-in for the embedder, and removing the reranker's torch dependency removed the project's last reason to keep torch installed at all.

## Alternatives considered

- **Keep Ollama as a registered alternative** — rejected. The stated reason for the switch is that the project has one user who has confirmed they don't run Ollama for any other reason and never want to. Maintaining the wiring "just in case" for a future second user adds complexity for zero current value; a future revival is one `git revert` of this commit.
- **fastembed for embedder, keep `sentence-transformers` for reranker** — rejected. Half-measure: keeps torch in the dependency closure for one cross-encoder. The whole point is to drop torch.
- **`model2vec` static embedder** — rejected for now. Tested at sub-1ms latency but with a 3–5 MTEB-point quality drop vs. nomic-embed-text-v1.5. Fast enough that it might revisit at v0.5 if real-user latency complaints surface.
- **Cloud-only embedder (OpenAI / Voyage)** — rejected. Conflicts with the local-first ethos and would need a payment surface inside a project that has none.
