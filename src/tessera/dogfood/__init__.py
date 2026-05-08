"""Dogfood evidence ledger for v0.5 GA gates.

Three v0.5 dogfood gates require longitudinal real-world evidence
that test suites cannot supply:

* `docs/dogfood/sync-dogfood.md` — 30+ days of multi-machine sync.
* `docs/dogfood/compiled-notebook-dogfood.md` — 60+ days of write-time
  compilation against a real research topic.
* `docs/dogfood/playbook-dogfood.md` — task-shaped Playbook compile /
  recompile / staleness loops with a populated failure-case log.

This package is the structured evidence channel those docs depend on.
A small JSONL ledger per gate lives under
``$TESSERA_DOGFOOD_DIR/<gate>.jsonl`` (default
``~/.tessera/dogfood/<gate>.jsonl``) and accumulates one append-only
row per real action — gate initialization, sync push / pull, audit
verify, compile / register, staleness flip, recompile, decision,
failure case, note. The CLI (``tessera dogfood``) drives manual entries
and rendering; the existing CLI surfaces (``tessera audit verify``,
``tessera sync push|pull``, ``tessera playbook register|stale``)
auto-emit rows when a gate is active.

The Markdown docs stay the human-readable narrative; the renderer
(``tessera dogfood render``) regenerates the doc's Evidence Log and
Acceptance Summary tables from the ledger between fenced markers so
the published evidence comes from the same source the auto-hooks
write to. Synthetic rows are not allowed — every row carries a real
machine_id and a real timestamp, and the gate-initialized row pins
the operator + start date that v0.5 GA review reads.

Call sites import from the submodules directly
(``tessera.dogfood.ledger`` / ``.render`` / ``.schemas``); the
package root deliberately does not re-export. Re-export surfaces
become load-bearing once they exist, and growing them speculatively
here violates "no abstractions without ≥2 concrete call sites today".
"""

from __future__ import annotations

__all__: list[str] = []
