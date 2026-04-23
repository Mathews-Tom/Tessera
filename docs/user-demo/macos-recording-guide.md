# macOS Recording Guide

Closes P14 task 4 for **macOS** (`docs/v0.1-dod-audit.md` item 1). Ubuntu and Windows have their own guides deferred to v0.1.x.

## What you're recording

A single ~10-minute screen+audio session that shows a clean-state install of Tessera on macOS, through to a ChatGPT draft in Tom's voice. No cuts, no edits beyond top-and-tail. The video is evidence for the release-spec DoD item, not a marketing piece — optimise for legibility of the demo, not production polish.

## Reference hardware

The video must be recorded on the same reference baseline the DoD latency table pins:

- MacBook Pro **M1 Pro**, 10-core CPU, 16-core GPU, 16 GB RAM.
- macOS 15.x (current stable).
- Daemon idle except for the test query; no concurrent Ollama workload.
- Ollama model **pinned** via the daemon's `keep_alive=-1` (handled by the code, not the operator).

A recording on an M3 Max or an Intel Mac is not the DoD evidence; it's a "future hardware" artifact that belongs to v0.1.x.

## Pre-recording checklist

Before screen-recording starts:

- [ ] `scripts/demo_smoke.sh` green.
- [ ] `ollama list` shows `nomic-embed-text:latest`.
- [ ] `~/.tessera/demo.db` and `~/.tessera/demo.db.salt` do NOT exist (wipe from any prior attempts).
- [ ] `~/Library/Application Support/Claude/claude_desktop_config.json` has no pre-existing `tessera` entry.
- [ ] ChatGPT Developer Mode enabled, no pre-existing `tessera` MCP server.
- [ ] Terminal window set to a readable font size (≥ 14 pt) and a high-contrast theme (light for screen capture).
- [ ] Claude Desktop and ChatGPT windows both sized to fit in the capture area alongside the terminal.
- [ ] Notifications disabled system-wide (Focus → Do Not Disturb; close Slack, Messages, Mail).
- [ ] Menu-bar clock format set to 24-hour numeric only (e.g. `15:42`) — this is the only per-frame timestamp the recording needs.
- [ ] Microphone selected and test-recorded; noise floor acceptable.

## Recording tools (pick one)

| Tool | When to use | Cost |
|------|-------------|------|
| **QuickTime Player** (built-in) → File → New Screen Recording | Default choice. Zero-config, captures full screen + mic, exports to .mov. | Free |
| **OBS Studio** | When you want layered capture (terminal window + webcam inset) or a local hotkey bind to pause | Free |
| **Loom** | When you want auto-generated chapter markers and a shareable URL without self-hosting | Paid tier has watermark removal |

**Do not use** iPhone Mirroring, AirPlay-to-external, or Zoom's local recording — all have codec quirks that butcher terminal text.

Recommended settings: 1080p @ 30 fps, H.264, 44.1 kHz stereo audio. Anything higher inflates the file without adding legibility.

## Time budget (matches `demo-script.md` §Time budget)

| Stage | Wall-clock | What must be on frame |
|-------|-----------:|-----------------------|
| 0 Bootstrap | 0:00–1:00 | Terminal only |
| 1 Claude connect | 1:00–1:30 | Terminal + Claude window |
| 2 Capture facets | 1:30–5:30 | Claude conversation (terminal can tuck under) |
| 3 Verify vault | 5:30–6:00 | Terminal only |
| 4 ChatGPT connect | 6:00–7:00 | ChatGPT window + terminal |
| 5 Recall + draft | 7:00–9:30 | ChatGPT tool panel + draft output |
| 6 Close + verify | 9:30–10:00 | Terminal only |

Total: **≤ 10:00**. A clean take that lands at 11:30 is a release blocker — iterate the demo, not the cutting-room floor.

## Post-recording steps

1. **Review the recording once.** Watch at 1× speed with audio on. Flag any moment where the tester-kit's debrief form would mark "blocker".
2. **Tag frictions.** If any, stop the release process and iterate on the code or the demo script, then re-record.
3. **Trim top-and-tail.** Cut dead air before "Tessera is a portable context layer..." and after "daemon stop". No interior cuts.
4. **Export** as `demo-macos-v0.1.0-<YYYY-MM-DD>.mp4`, H.264, ≤ 200 MB.
5. **Publish.** Options, in order of preference:
   - **YouTube unlisted** — stable URL, no storage cost, captions auto-generated.
   - **Loom** — auto chapters, but single-account binding.
   - **Self-hosted** at `https://tessera.dev/demo-macos-v0.1.0.mp4` (if a static site stands up by release day).
6. **Link from**:
   - `CHANGELOG.md §[0.1.0] — Install` (demo walk-through link placeholder).
   - `README.md` (post-reframe rewrite under P15 task 4).
   - `docs/v0.1-dod-audit.md` item 1 (flip Pending-external → Green with the URL as evidence).

## Common recording mistakes

| Mistake | Fix |
|---------|-----|
| Terminal font too small; viewer can't read commands | ≥ 14 pt, preferably 16 pt. Verify on a 1080p monitor, not the Retina 2× display. |
| Background app notification mid-recording | Focus / Do Not Disturb ON before the pre-flight. Close Slack, Mail, Messages, calendar. |
| Cursor flicker during Claude's response streaming | Accept it — this is what the product looks like. Don't add jump-cuts. |
| Token / passphrase visible in terminal history scroll-back | Start a fresh terminal tab for the recording. `history -c` beforehand. Use a throwaway passphrase (`demo-$(date +%s)`). |
| Ollama pulls the model mid-recording | `ollama pull nomic-embed-text` during pre-flight, not on camera. The smoke script catches this. |
| `tessera doctor` shows ERROR on camera | Stop immediately. Fix the underlying issue per `doctor`'s error message. Restart clean. |

## What this guide deliberately does NOT cover

- **Linux / Windows recording.** Different tooling (SimpleScreenRecorder, OBS on Ubuntu; OBS or Game Bar on Windows), different client config paths. Guides for those land in v0.1.x alongside the cross-platform smoke test evidence.
- **Marketing-grade video production.** Intros, music, chapter markers, stinger graphics. None of those belong to the DoD; they belong to launch-day outreach in P15 task 4.
- **Editing interior cuts.** The DoD evidence is a continuous recording. If the demo has ugly pauses, iterate the demo.

## After the recording lands

Update `docs/v0.1-dod-audit.md` item 1:

```markdown
### 1. Fresh install on clean macOS, Ubuntu, Windows — under 10 minutes end-to-end

**Status:** Partial — macOS green, Ubuntu + Windows pending-external
**Evidence (macOS):** <URL to the published video>, duration <MM:SS>, recorded <YYYY-MM-DD> on M1 Pro / macOS 15.x.
```

macOS done flips the item from **Pending-external** to **Partial**. Going fully Green requires the Ubuntu + Windows recordings too — a v0.1.x item per the plan.
