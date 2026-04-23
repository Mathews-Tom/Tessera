# Real-User Tester Kit

Artifacts that let an **external T-shape engineer** complete the Tessera demo unaided while the session is recorded. Closes P14 task 6 / v0.1 DoD item 9.

## Who qualifies as a real-user tester

Per `.docs/development-plan.md §P14 risks`:

- **Not Tom.**
- **Not a direct collaborator** on Tessera (hasn't reviewed the PRs, hasn't read the spec).
- T-shaped in profile: deep in one technical domain, active across ≥ 3 AI tools (Claude + ChatGPT + one other).
- Has installed and configured at least one MCP server before (Tessera is not their first MCP rodeo).
- Willing to record a 30-minute session for your own observation only.

Two candidates are enough for the DoD gate; aim higher to avoid one flaking.

## Session protocol

### Before the session (you)

1. Run `scripts/demo_smoke.sh` on the tester's machine (or over screen-share if remote) to confirm env readiness.
2. Hand the tester the **Tester Briefing** (next section) and the installed Tessera walkthrough in `CHANGELOG.md §[0.1.0] — Install`.
3. Start recording. Tester controls the keyboard; you observe silently.
4. Set a timer for 15 minutes. Do not intervene in the first 15 minutes unless asked a direct question.

### During the session (tester)

- Follow the install walkthrough in CHANGELOG.
- Attempt the T-shape demo (capture in Claude → recall in ChatGPT → draft in Tom's voice).
- Think aloud. Report what they expected to happen and what actually happened.
- If they get stuck for > 90 seconds, they ask a single clarifying question. **One total** across the whole session. More than one = fail.

### After the session (both)

- Tester fills out the **Debrief Form** below.
- You watch the recording once within 24 hours and tag frictions by DoD item number.
- Any friction that blocks the demo outcome is a release blocker.
- Any friction that doesn't block but wastes > 30 seconds is a v0.1.x follow-up.

## Tester Briefing (hand this to the tester)

```
Subject: Tessera user test — ~30 minutes, recorded

Thanks for testing Tessera. The goal: install it from a clean state,
capture four facets in Claude Desktop (a preference, a workflow, a project
note, a style sample), then open ChatGPT Developer Mode and ask it to
draft a LinkedIn post using your captured context.

What I need from you:

- Follow the CHANGELOG install steps literally.
- Think aloud the whole time. "I'm expecting X, I'm going to try Y."
- If you get stuck for 90 seconds, ask me ONE clarifying question.
- Tell me when you're done; I'll stop the recording.

What I don't need:

- Don't optimise. Don't look at the source. Don't read beyond the
  CHANGELOG install section until you're stuck.
- Don't worry about time. 10 minutes is the target; 30 minutes tells me
  more than 5 minutes would.

The goal is for ChatGPT to produce a LinkedIn draft that reads like
something I would write. We'll look at that together at the end.
```

## Debrief Form (tester fills out, or you interview)

Copy-paste this block into a fresh document named `docs/user-demo/sessions/<date>-<tester-id>.md`. The filename is the only structured piece; the rest is free-text. We will NOT commit session notes with the tester's name or contact info — strip those before committing or keep the file gitignored under `docs/user-demo/sessions/`.

```
# Session — <YYYY-MM-DD> — <anonymous-id>

Total wall-clock: <MM:SS>
Outcome: [completed | partial | abandoned]
ChatGPT draft voice match (tester's subjective 1–5): <N>

## What worked first try
- 

## What confused the tester (with timestamp from the recording)
- 

## Single clarifying question asked (if any)
Q: 
A: 

## DoD-item friction tags (fill after watching the recording)
- DoD 1 (fresh install):   [none | minor | blocker]  — notes
- DoD 3 (tessera doctor):  [none | minor | blocker]  — notes
- DoD 5 (latency envelope):[none | minor | blocker]  — notes
- DoD 6 (SWCR coherence):  [none | minor | blocker]  — notes

## Did the LinkedIn draft match the tester's read of Tom's voice?
- Yes / No / Partially — notes

## Would this tester install Tessera for themselves?
- Yes / No / Maybe — one sentence why

## Signal for the plan
- Does this change any v0.1 decision? [yes | no] — if yes, what
- Does this flag a v0.1.x follow-up? [yes | no] — if yes, what
```

## Session success criteria

A single session counts as a **DoD pass** if ALL hold:

1. Tester completed the demo without assistance beyond the single clarifying question.
2. Tester rated voice match ≥ 3/5 on the final LinkedIn draft.
3. No DoD-item friction tagged "blocker".
4. Recording length ≤ 30 minutes from `pip install` to draft output.

A session that fails any of the four is the signal the plan expects: **iterate the product or the docs, then re-test.** Do not ship v0.1.0 after a failed session; the plan's risk register explicitly names this as the hard gate.

## What to NOT do

- Don't coach the tester during the session. The real-user test measures the product, not the tester's tolerance for hand-holding.
- Don't aggregate across testers into a single "pass/fail". Each session is its own data point.
- Don't publish tester identity. Strip names and emails from anything you commit.
- Don't add the tester to your own MCP client configs afterward. Keep the test clean for a potential re-run.
