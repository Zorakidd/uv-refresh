# uv-refresh

Rebuilds a uv project's `pyproject.toml` so every dependency gets freshly
resolved instead of dragging along old version pins.

## How it works

1. Read `pyproject.toml`, collect the dependencies without their version
   specifiers (extras and environment markers are kept; `include-group`
   entries in `dependency-groups` are resolved, not dropped)
2. Back up `pyproject.toml` and `uv.lock` into `.uv-refresh-backup/<timestamp>/`
3. Run `uv init --bare` + `uv add <packages>` in a temp directory next to
   the project, with the project's `[tool.uv]` settings (indexes, sources,
   constraints) and `.python-version` -- the real `pyproject.toml` stays
   untouched the whole time
4. Merge only `dependencies`/`optional-dependencies`/`dependency-groups`
   from the result into a copy of the ORIGINAL `pyproject.toml` -- everything
   else stays untouched
5. Atomically swap the result in place of the old `pyproject.toml`/`uv.lock`
6. Report which version bounds actually moved

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
| `-q`, `--quiet` | only print warnings/errors -- also quiets `uv` itself |
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

## What it reports

A run ends with the bounds it actually changed, so there's no need to diff
the backup by hand:

    Updated 2 of 3 dependencies:
      iniconfig   >=1.0 -> >=2.3.0
      packaging  (none) -> >=26.3
      (1 unchanged)

    Done.

If nothing moved, it says so instead. `--verbose` additionally prints the
whole new `pyproject.toml`; `--quiet` prints neither.

## Note

Only `dependencies`, `optional-dependencies` and `dependency-groups` are
rewritten. Everything else -- `description`, `readme`, `license`,
`authors`, `keywords`, `[project.urls]`, `[project.scripts]`,
`[build-system]`, `[tool.*]` and so on -- stays unchanged, because it's
never deleted: the tool only builds a minimal `pyproject.toml` temporarily
to resolve versions, then takes just the freshly resolved dependency lists
from it and writes those back into a copy of the original file. Within those
lists, each entry only gets its new bound in place: comments (above an entry,
after it, or between two groups) and the order of the entries stay as they
were.

One exception: with `--no-groups`, any existing
`optional-dependencies`/`dependency-groups` are intentionally removed (the
tool warns beforehand). If `[tool.uv]` still names one of them (a
`default-groups` list, or a source limited to an `extra`/`group`),
`--no-groups` is refused up front instead, since uv rejects references to
extras/groups that no longer exist. Individual entries that can't be
interpreted as a PEP 508 string are skipped and reported per entry.

`--full` additionally bumps `requires-python` to the newest *already
installed* Python it can find (`uv python list --only-installed` -- it
never triggers a download on its own), e.g. `>=3.11` becomes `>=3.13`. That
bump is part of the same atomic pyproject.toml rebuild as the dependency
refresh, so it's covered by the same backup/all-or-nothing guarantee.

It only ever goes *up*: pre-release Pythons (e.g. `3.15.0rc2`) are ignored,
a `requires-python` that already starts at that minor version or above is
kept as is, and if the newest installed Python doesn't satisfy a kept
`requires-python` (older than its floor, or e.g. an exact `==3.13` with
3.13.5 installed), `--full` leaves both `requires-python` and
`.python-version` alone (with a warning) and just does the normal refresh.

Only once that rebuild has landed does `--full` re-pin `.python-version` via
`uv python pin` to that same version. This runs *after* the rebuild on
purpose: `uv python pin` refuses to write anything if the target version
doesn't satisfy `requires-python`, and by pinning after the bump above, it's
checked against the *new* `requires-python` -- so jumping to a newer Python
than the project previously allowed still works. If the pin itself then
fails, the dependency refresh and `requires-python` bump are kept regardless
(they already succeeded); only `.python-version` is left as it was.

## Why the temp build copies `.python-version`

A bare `uv init --python=<requires-python floor>` only writes that floor
(e.g. `>=3.11`) into `requires-python`, not an exact interpreter pin. Left
alone, the temp `uv add` would then run against the *newest* installed
Python satisfying that floor, even if the real project's own
`.python-version` pins an older one -- and fail on any dependency (e.g.
`torch`) that has no wheel for that newer version, despite the real project
working fine on its actual pinned interpreter. So uv-refresh copies the pin
into the temp directory before running `uv add`. `--full` is the one
exception: when it re-pins, the temp build already uses the new version
instead of the old pin.

## Limitations

The temp directory only ever holds `pyproject.toml` (plus `uv.lock` and
`.python-version`), one level below the project. So uv-refresh refuses these
projects up front, before any backup is made, instead of failing halfway with
a uv or build-backend error:

- uv workspaces (`[tool.uv.workspace]`) and `workspace = true` sources
- `path` sources with a relative path, and `${PROJECT_ROOT}` references
  (absolute paths work fine)
- a relative local path in `[[tool.uv.index]]`, `index-url`,
  `extra-index-url` or `find-links` (absolute paths and URLs work fine)
- a dynamic `version`, `dependencies`, `optional-dependencies` or
  `requires-python` (e.g. setuptools-scm, hatch-vcs), because uv would have to
  build the project to lock it, and its files aren't in the temp directory

An exact `requires-python = "==3.14"` can make the refresh fail: uv may pick
a newer 3.14.x interpreter to lock with and then reject it. Plain `uv lock`
fails the same way on such a project, so that's not something uv-refresh can
fix.
