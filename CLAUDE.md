# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**microphone-cli** — an agent-first CLI for USB microphones and microphone
arrays: enumerate devices, select and inspect channels, control gain and sample
format, and read direction-of-arrival from array firmware.

**Current state: scaffold only.** The repo was cloned from the AgentCulture
agent template (`5f9b1bd scaffold microphone-cli from culture-agent-template`).
The package is renamed and the CI/identity/skills baseline is live, but **no
microphone domain code exists yet** — the only verbs are the template's
agent-first introspection surface (`whoami`, `learn`, `explain`, `overview`,
`doctor`, `cli overview`). Several strings still describe the template
("a clonable template for AgentCulture mesh agents") rather than the microphone
domain: `microphone_cli/cli/_commands/learn.py`, `overview.py` (`_ARTIFACTS`),
`microphone_cli/explain/catalog.py`, and `README.md`. Rewrite those as the
domain lands.

## Commands

```bash
uv sync                                   # install (dev group included)

uv run pytest -n auto                     # full suite, parallel
uv run pytest tests/test_cli.py           # one file
uv run pytest -k whoami                   # one test / pattern
uv run pytest --cov=microphone_cli --cov-report=term   # coverage (fail_under=60)

uv run black --check microphone_cli tests # CI lint set — all four must pass
uv run isort --check-only microphone_cli tests
uv run flake8 microphone_cli tests
uv run bandit -c pyproject.toml -r microphone_cli

uv run teken cli doctor . --strict        # the agent-first rubric gate CI runs

uv run microphone whoami                  # the installed console script
uv run microphone doctor --json
python -m microphone_cli learn
```

**Markdown lint is not a `uv` tool** — `uv sync` does not install it, so it is
not available on a fresh checkout. CI installs it from npm; do the same locally,
pinning the version CI uses so results match:

```bash
npm install -g markdownlint-cli2@0.21.0
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills"
```

Config lives in `.markdownlint-cli2.yaml` (MD013 and MD060 off, MD024
siblings-only for the changelog; `.claude/skills/**` ignored).

### Console-script name

`pyproject.toml` declares `microphone = "microphone_cli.cli:main"` — the binary
is **`microphone`**, not `microphone-cli`. The argparse `prog` is
`"microphone-cli"`, so `--help` and every doc string say `microphone-cli …`
while the actual command is `microphone …`. Either rename the script or the
`prog` before this ships; until then, prefer `microphone` in anything runnable.

## Architecture

Zero runtime dependencies (`dependencies = []` in `pyproject.toml`) — that is a
deliberate constraint of the agent-first template, and `culture.yaml` is parsed
by hand in `whoami.py` rather than pulling in PyYAML. Keep new runtime deps out
unless the microphone backend genuinely requires one.

### CLI dispatch

`microphone_cli/cli/__init__.py` is the only place that knows the verb set.
`_build_parser()` imports each command module and calls its `register(sub)`;
`main()` parses and hands off to `_dispatch()`, which invokes `args.func(args)`.

Three contracts hold across the whole surface, and the rubric gate checks them:

- **Errors** — `microphone_cli/cli/_errors.py`. Every failure raises `CliError`
  (`{code, message, remediation}`). `_dispatch` wraps any stray exception into
  one, so **no traceback ever reaches stderr**. Exit codes: `0` success,
  `1` user error, `2` environment error, `3+` reserved.
- **Streams** — `microphone_cli/cli/_output.py`. Results to stdout
  (`emit_result`), errors and diagnostics to stderr (`emit_error`,
  `emit_diagnostic`); they never mix. Text errors render `error:` + `hint:`.
- **`--json` everywhere** — every parser adds `--json`; handlers read it via
  `getattr(args, "json", False)`.

Argparse's own errors also honour these contracts: `_CliArgumentParser`
overrides `.error()` to emit a `CliError`, and subparsers are built with
`parser_class=_CliArgumentParser` so nested nouns inherit it. Because
parse-time errors happen before `args.json` exists, `main()` pre-scans raw argv
for `--json` into the class-level `_json_hint`. **When you add a nested
subparser group, pass `parser_class=type(p)` through** (see
`_commands/cli.py`) — forgetting it silently drops that group back to
argparse's default `exit(2)` behaviour.

### Identity

`_commands/whoami.py` walks up from `__file__` (not the CWD) to find the repo's
own `culture.yaml`, so identity is the agent's, not the caller's. In a wheel
install no `culture.yaml` ships and the literal fallbacks apply. `doctor` and
`overview` both build on `whoami`'s `report()` / `read_agent_fields()`.

`doctor` mirrors the two `steward doctor` invariants — **prompt-file-present**
and **backend-consistency** — via the `_PROMPT_FILE` map (`claude` →
`CLAUDE.md`, `colleague` → `AGENTS.colleague.md`, `acp` → `AGENTS.md`,
`gemini` → `GEMINI.md`), plus a skills-present check. It returns
`{healthy, checks: [{id, passed, severity, message, remediation}]}` — the
rubric depends on that exact shape.

**This agent runs `backend: colleague`** (`culture.yaml`), so its resident
prompt file is `AGENTS.colleague.md`; this `CLAUDE.md` is the Claude Code
guidance file. Changing `backend` means adding the matching prompt file or
`doctor` (and CI's rubric gate) goes red.

### Adding a verb or noun

1. New module in `microphone_cli/cli/_commands/` exposing `register(sub)`, with
   `--json` and a `func` default.
2. Register it in `_build_parser()` (there is a marked spot).
3. **Add a catalog entry** in `microphone_cli/explain/catalog.py` keyed by the
   command-path tuple — `tests/test_cli.py::test_every_catalog_path_resolves`
   walks every key, and the rubric requires an `explain` entry per path.
4. Update the command map in `_commands/learn.py` (both `_TEXT` and
   `_as_json_payload()`) and the `_VERBS` list in `_commands/overview.py`.
5. A noun group that gets action-verbs **must** also expose `<noun> overview` —
   the rubric's `overview_cli_noun_exists` check. `_commands/cli.py` is the
   worked example (its bare form prints its own overview).
6. Descriptive verbs must not hard-fail on a bad target path — `overview`
   accepts and ignores a positional `target` for exactly this reason.

## CI and release

Three jobs in `.github/workflows/tests.yml`: `test` (pytest + coverage →
SonarCloud, scan skipped when `SONAR_TOKEN` is empty, so fork PRs stay green),
`lint` (black, isort, flake8, bandit, markdownlint, `teken cli doctor --strict`),
and `version-check`.

**Every PR bumps the version — including docs-, config-, and CI-only PRs.**
Use the `version-bump` skill (or edit `pyproject.toml` + `CHANGELOG.md` by hand,
Keep-a-Changelog format). `__version__` is read from package metadata, so there
is no second version literal to update.

`version-check` only partly enforces that rule: it compares the PR's
`pyproject.toml` version against `origin/main` as **strings** and fails on
equality alone. Any different value passes, a downgrade included — `0.8.1` →
`0.8.0` is green today. Treat the check as a "did you forget entirely?" tripwire,
not a guarantee the version moved forward; the publish job is what actually
breaks later. (The comparison is string-equality in every AgentCulture sibling,
so tightening it belongs upstream, not in this repo alone.)

`publish.yml` publishes to TestPyPI on same-repo PRs (`<version>.devN`) and to
PyPI on push to main, both via Trusted Publishing.

Line length is **100** (black, isort profile=black, flake8 with `E203,W503`
ignored). SonarCloud project key: `agentculture_microphone-cli`; the quality
gate blocks CI when the token is configured.

## Vendored skills

`.claude/skills/` is vendored **cite-don't-import** from `guildmaster` (several
originate in `devague`, one in `colleague`). Do not hand-edit them — they are
excluded from markdownlint and Sonar for that reason. Provenance and the
re-sync procedure live in `docs/skill-sources.md`; per-machine paths go in a
git-ignored `.claude/skills.local.yaml` (copy the `.example`).

Use the `cicd` skill for PR creation and review-comment handling. Online posts
made outside those scripts sign as `- microphone-cli (Claude)`.
