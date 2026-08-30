# Contributing

Hermes Health Apollo is a local-first Hermes plugin for sensitive health and
daily-context data. Keep contribution examples synthetic and keep runtime data
outside the repository.

## Current setup

This repository currently has no committed `uv.lock`, and `.gitignore` ignores
generated lockfiles. Use the non-frozen `uv` setup below unless a separate
dependency/lockfile PR changes that policy. The package artifact tests call
`python -m pip wheel`, so seed the local `.venv` with `pip`.

```bash
uv venv --seed --clear .venv
uv sync --extra dev
```

Current installation for users and contributors is from a local source checkout:

```bash
make install-git-hooks
make install-local
hermes plugins enable health-data
```

The `pip install hermes-health-data` path is future packaging guidance only
until the package is published.

## Required checks

Run the current local gate before opening a pull request:

```bash
uv venv --seed --clear .venv
uv sync --extra dev
uv run --extra dev python -m pytest
uv run --extra dev python scripts/secret_scan.py
uv run --extra dev python scripts/secret_scan.py --all-files
git diff --check
```

For release-oriented changes, also run:

```bash
uv run --extra dev --with build python scripts/release_safety.py
```

## Data and privacy rules

Do not commit, attach, or paste real user data. This includes:

- health databases, wearable exports, workout/food records, or screenshots;
- OAuth credentials, access tokens, refresh tokens, callback URLs, `code`, or
  `state` values;
- calendar event titles, attendee identities, Gmail subjects, snippets, or
  body content;
- route, map, location, timeline, or takeout files;
- logs that expose local paths, account identifiers, or private context.

Use synthetic fixtures for tests. If a bug cannot be explained without real
data, follow [`SECURITY.md`](SECURITY.md) and use private reporting.

## Pull requests

- Keep changes focused and explain the user-visible boundary in the PR body.
- Update tests or fixtures when behavior changes.
- Keep installation wording honest: source checkout is current; PyPI is future
  until the package exists.
- Keep MIT license wording consistent with `pyproject.toml` and `LICENSE`.
- Do not add generated lockfiles, local databases, credentials, or runtime
  state unless the maintainer explicitly changes repository policy first.
