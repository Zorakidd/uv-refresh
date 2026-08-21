# uv-refresh

Rebuilds a uv project's `pyproject.toml` so every dependency gets freshly
resolved instead of dragging along old version pins.

## How it works

1. Read `pyproject.toml`, collect the dependencies without their version
   specifiers (extras and environment markers are kept; `include-group`
   entries in `dependency-groups` are resolved, not dropped)
2. Back up `pyproject.toml` and `uv.lock` into `.uv-refresh-backup/<timestamp>/`
3. Run `uv init --bare` + `uv add <packages>` in a temp directory next to
   the project -- the real `pyproject.toml` stays untouched the whole time
4. Merge only `dependencies`/`optional-dependencies`/`dependency-groups`
   from the result into a copy of the ORIGINAL `pyproject.toml` -- everything
   else stays untouched
5. Atomically swap the result in place of the old `pyproject.toml`/`uv.lock`

If any step fails -- including Ctrl+C -- the real `pyproject.toml` was never
touched, since the whole build happened in the temp directory. The backup
is kept around as an extra reference regardless.

## Installation

    uv tool install uv-refresh

Makes `uv-refresh` permanently available as a command (globally on PATH).

For a one-off test run, without installing anything:

    uvx uv-refresh --dry-run

Or straight from the repo, e.g. to try an unreleased version:

    uv tool install git+https://github.com/Zorakidd/uv-refresh

## Usage

    uv-refresh --dry-run     # just show what would happen, change nothing
    uv-refresh               # runs with a confirmation prompt
    uv-refresh -y            # no confirmation prompt

## Options

| Flag | Effect |
| --- | --- |
| `--path PATH` | different project directory (default: current) |
| `--dry-run` | only show what would happen, touch nothing |
| `-y`, `--yes` | run without asking for confirmation |
| `-v`, `--verbose` | print the full new `pyproject.toml` at the end |
| `-q`, `--quiet` | only print warnings/errors, no status output |
| `--timeout SECONDS` | timeout per `uv` call, default 300s |
| `--raw` | add packages with no version bound at all |
| `--bounds {lower,major,minor,exact}` | kind of version bound `uv add` sets |
| `--keep-lock` | keep `uv.lock` (uv will then prefer the old versions!) |
| `--keep-backups N` | how many past backups to keep, oldest deleted first (default: 5, 0 keeps all) |
| `--no-groups` | ignore optional-dependencies and dependency-groups |
| `--full` | also bump `requires-python` and re-pin `.python-version` to the newest installed Python |
| `--drop-extras` | shrink `fastapi[standard]` down to `fastapi` |
| `--drop-markers` | drop environment markers |
| `--version` | show the uv-refresh version |

## Note

Only `dependencies`, `optional-dependencies` and `dependency-groups` are
rewritten. Everything else -- `description`, `readme`, `license`,
`authors`, `keywords`, `[project.urls]`, `[project.scripts]`,
`[build-system]`, `[tool.*]` and so on -- stays unchanged, because it's
never deleted: the tool only builds a minimal `pyproject.toml` temporarily
to resolve versions, then takes just the freshly resolved dependency lists
from it and writes those back into a copy of the original file.

One exception: with `--no-groups`, any existing
`optional-dependencies`/`dependency-groups` are intentionally removed (the
tool warns beforehand). Individual entries that can't be interpreted as a
PEP 508 string are skipped and reported per entry.

`--full` additionally bumps `requires-python` to the newest *already
installed* Python it can find (`uv python list --only-installed` -- it
never triggers a download on its own), e.g. `>=3.11` becomes `>=3.13`. That
bump is part of the same atomic pyproject.toml rebuild as the dependency
refresh, so it's covered by the same backup/all-or-nothing guarantee.

Only once that rebuild has landed does `--full` re-pin `.python-version` via
`uv python pin` to that same version. This runs *after* the rebuild on
purpose: `uv python pin` refuses to write anything if the target version
doesn't satisfy `requires-python`, and by pinning after the bump above, it's
checked against the *new* `requires-python` -- so jumping to a newer Python
than the project previously allowed still works. If the pin itself then
fails, the dependency refresh and `requires-python` bump are kept regardless
(they already succeeded); only `.python-version` is left as it was.
