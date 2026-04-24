# Homebrew packaging

This directory holds the canonical Homebrew formula for `tessera-context`. The formula is version-controlled alongside the source tree so that a formula bump is part of the same PR as the release bump.

## Layout

```
packaging/homebrew/
├── README.md                  ← this file
└── Formula/
    └── tessera.rb             ← canonical formula, pinned to a PyPI release
```

The `Formula/` subdirectory is the path Homebrew looks for inside a tap repo. Keeping the same layout here makes publishing to the tap a copy-and-commit rather than a re-structure.

## Local install (no tap required)

You can install directly from the file without setting up a tap:

```bash
brew install --build-from-source ./packaging/homebrew/Formula/tessera.rb
```

Runs through the full `depends_on` graph (`python@3.12`, `sqlcipher`), creates the private venv, installs `tessera-context==<version>` from PyPI, and symlinks the `tessera` binary onto your `PATH`. The `test do` block runs via `brew test tessera` after install.

Local install is the right path for:

- Validating a formula change before publishing it
- One-off installs on a machine you don't want to tap
- CI verification against a fresh Homebrew environment

## Publishing to the tap repo

The tap repo lives (or will live) at `https://github.com/Mathews-Tom/homebrew-tessera` — the `homebrew-` prefix is a Homebrew convention; users tap it as `Mathews-Tom/tessera` (without the prefix) and Homebrew resolves the full repo name automatically.

**First-time tap bootstrap:**

```bash
# 1. Create the tap repo on GitHub (empty, public, no README).
gh repo create Mathews-Tom/homebrew-tessera --public --description "Homebrew tap for Tessera" --confirm

# 2. Clone it somewhere outside the Tessera tree.
git clone git@github.com:Mathews-Tom/homebrew-tessera.git ~/src/homebrew-tessera
cd ~/src/homebrew-tessera

# 3. Copy the canonical formula over with the Formula/ path.
mkdir -p Formula
cp ~/src/Tessera/packaging/homebrew/Formula/tessera.rb Formula/

# 4. Commit and push.
git add Formula/tessera.rb
git commit -m "feat(tap): initial tessera formula for 0.1.0rc1"
git push -u origin main
```

Users then install:

```bash
brew tap Mathews-Tom/tessera
brew install tessera
```

**On every subsequent release:**

1. Bump `version` and `sha256` in `packaging/homebrew/Formula/tessera.rb` here in the Tessera repo (as part of the release PR).
2. After that PR merges, copy the updated file to the tap repo, commit with a `bump: tessera <new-version>` message, and push. This keeps the tap's commit history aligned with Tessera's releases.

A CI job that mirrors the formula automatically on release-tag push is a future follow-up — today's step is manual copy so the loop stays visible.

## Bumping the version

When a new `tessera-context` release ships to PyPI, update the formula:

```bash
# 1. Grab the new sdist sha256 from PyPI.
curl -sSf https://pypi.org/pypi/tessera-context/<new-version>/json \
  | python3 -c "import sys, json; \
                d = json.load(sys.stdin); \
                [print(u['digests']['sha256']) for u in d['urls'] if u['packagetype']=='sdist']"

# 2. Edit Formula/tessera.rb:
#    - version "<new-version>"
#    - sha256 "<new-hash>"
#    (The url line stays the same — it interpolates via the canonical
#    source/t/tessera-context/ PyPI URL pattern.)

# 3. Syntax-check.
ruby -c packaging/homebrew/Formula/tessera.rb

# 4. Run brew audit + local install to validate (optional on non-macOS).
brew audit --strict --new-formula packaging/homebrew/Formula/tessera.rb
brew install --build-from-source packaging/homebrew/Formula/tessera.rb
brew test tessera
```

## Non-goals for this formula

- **Not suitable for `homebrew-core` submission.** Homebrew's main repo requires every transitive Python dependency to be listed as a `resource` block with its own pinned sha256 — a maintenance cost that isn't justified for a pre-release tap. If Tessera reaches wider distribution and core inclusion becomes worth the overhead, the formula can be regenerated with `homebrew-pypi-poet` or `brew update-python-resources`.
- **Not a reproducibility guarantee.** Without resource pinning, the transitive dep graph is resolved at install time. Pip's resolver is deterministic for a given `pyproject.toml` pin set, so the practical reproducibility is high; the theoretical guarantee is lower than `homebrew-core`'s.
- **Not a Linux packaging path.** Homebrew formulas target macOS primarily; Homebrew-on-Linux works but Tessera's Linux distribution story is planned to route through `.deb` / `.rpm` packaging (v0.1.x packaging, pending strategy pick) rather than Homebrew.

## Adjacent packaging work

- `.deb` and `.rpm` packages for Linux — separate decision point per `docs/release-spec.md §v0.1.x — Stabilization`.
- PEP 541 reclaim for the short `tessera` PyPI name — out-of-session, tracked in `docs/v0.1-dod-audit.md §Follow-ups`. When the reclaim lands, the formula `url` + `pip_install` arguments change from `tessera-context` to `tessera`; the formula class name and CLI binary name stay the same.
