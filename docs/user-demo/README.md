# Tessera User-Demo Kit

Artifacts that close the two remaining P14 release blockers in `docs/v0.1-dod-audit.md`:

- **P14 task 4** — record the T-shape demo on macOS (and later Ubuntu + Windows).
- **P14 task 6** — have one external T-shape engineer complete the demo unaided, recorded.

Everything here is a **contract with a human operator**. The demo itself is a 10-minute human session across Claude Desktop + ChatGPT + the terminal that cannot be run from CI.

## Which artifact do I open next?

| Situation | Open this |
|-----------|-----------|
| I want to record my own walkthrough on macOS right now | [`demo-script.md`](demo-script.md) → then [`macos-recording-guide.md`](macos-recording-guide.md) |
| I have a tester coming in and need to hand them a packet | [`tester-kit.md`](tester-kit.md) |
| My recording session kept failing because of setup | Run `scripts/demo_smoke.sh` first; the script flags the environment issue in under 30 seconds |
| I want to understand the demo narrative before the session | [`demo-script.md`](demo-script.md) top section (the 60-second briefing) |

## The 30-second pre-flight

Before any recording session, always run:

```bash
scripts/demo_smoke.sh
```

Green = ready to record. Red = stderr tells you what to fix. The script is the single-command alternative to discovering mid-recording that Ollama isn't running, the model isn't pulled, or the vault won't unlock.

## What these artifacts deliberately do NOT cover

- **MCP client configuration** on non-macOS platforms — the Claude Desktop / Codex / Cursor config paths differ across operating systems. v0.1.x ships Ubuntu + Windows guides after the macOS recording lands.
- **Bug-report capture** during the session — that lives in `tester-kit.md §Post-session debrief`. Don't reinvent it in the recording guide.
- **Outcome interpretation** — a failed real-user session is a useful signal, not a failure of the kit. The tester kit's debrief form captures the signal structurally.

## Document ownership

Tom is the sole maintainer through v0.1.x. If a tester reports a friction that changes the demo narrative, update `demo-script.md` in the same PR as the corresponding code change; do not let this kit drift from the shipped product.
