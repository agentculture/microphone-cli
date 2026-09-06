# microphone-cli

Agent-first CLI for USB microphones and microphone arrays: enumerate devices,
select and inspect channels, control gain and sample format, and read
direction-of-arrival from array firmware.

> **Status: scaffold.** The identity, CI, and agent-first CLI baseline are in
> place; the microphone domain verbs are not implemented yet. What ships today
> is the introspection surface below.

## What you get

- **An agent-first CLI** cited from [teken](https://github.com/agentculture/teken)
  (`afi-cli`) — the runtime package has no third-party dependencies.
- **A mesh identity** — `culture.yaml` (`suffix` + `backend`) and the matching
  resident prompt file (`AGENTS.colleague.md`, since this agent runs
  `backend: colleague`).
- **The canonical guildmaster skill kit** under `.claude/skills/`, vendored
  cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **A build + deploy baseline** — pytest, lint, the agent-first rubric gate, and
  PyPI Trusted Publishing wired into GitHub Actions.

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run microphone whoami              # identity from culture.yaml
uv run microphone learn               # self-teaching prompt (add --json)
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

The installed console script is **`microphone`**. Argparse still prints
`microphone-cli` as the program name in `--help` output; the runnable command is
`microphone`.

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

## Development

```bash
uv run pytest tests/test_cli.py       # a single file
uv run pytest -k whoami               # a single test
uv run black --check microphone_cli tests
uv run isort --check-only microphone_cli tests
uv run flake8 microphone_cli tests
uv run bandit -c pyproject.toml -r microphone_cli
```

Every PR bumps the version in `pyproject.toml` and adds a `CHANGELOG.md` entry —
CI's `version-check` job enforces it, even for docs- and CI-only changes.

See [`CLAUDE.md`](CLAUDE.md) for the architecture, the CLI contracts (errors,
stream split, `--json`), and how to add a verb or noun group.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
