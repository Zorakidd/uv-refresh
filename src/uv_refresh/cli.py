"""uv-refresh
==========

Rebuilds a uv project's pyproject.toml so every dependency gets freshly
resolved instead of dragging along old version pins.

Steps:
  1. Read pyproject.toml and collect the dependencies without their version
     specifiers (extras and environment markers are kept, see --drop-extras)
  2. Back up pyproject.toml + uv.lock into a backup folder
  3. Run uv init --bare + uv add <names> in a temp directory next to the
     project, with the project's [tool.uv] settings (indexes, sources,
     constraints) and .python-version -- the real pyproject.toml stays
     untouched the whole time
  4. Merge only dependencies/optional-dependencies/dependency-groups from
     the result into a copy of the ORIGINAL pyproject.toml -- everything
     else (description, readme, license, authors, keywords, [project.urls],
     [project.scripts], [build-system], [tool.*], ...) stays untouched
  5. Atomically swap the result in place of the old pyproject.toml/uv.lock

If any step fails -- including Ctrl+C -- the real pyproject.toml was never
touched, since the whole build happened in the temp directory. The backup
is kept around as an extra reference regardless.

Projects the temp directory can't rebuild -- uv workspaces, relative path
sources, a dynamic version or dependencies -- are refused up front, before
any backup is made.

--full additionally bumps requires-python to the newest installed Python as
part of step 4, so it's covered by the same atomic swap in step 5, then
re-pins .python-version to match once that swap has landed. It only ever
raises requires-python, and ignores pre-release Pythons.

Usage:
  uv-refresh                 # in the project directory, asks for confirmation
  uv-refresh --dry-run       # just show what would happen, touch nothing
  uv-refresh --raw           # add packages with no version bound at all
  uv-refresh --path ../other # a different project directory
  uv-refresh --full          # bump requires-python + re-pin .python-version to the newest installed Python
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import tomlkit

from . import __version__

try:  # bevorzugt der offizielle PEP-508-Parser
    from packaging.requirements import InvalidRequirement, Requirement
except ModuleNotFoundError:  # Fallback, damit das Skript auch nackt laeuft
    Requirement = None
    InvalidRequirement = ValueError  # ty: ignore[invalid-assignment]

try:  # --full's requires-python handling; without packaging, plan_full() skips it
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
except ModuleNotFoundError:
    SpecifierSet = None  # ty: ignore[invalid-assignment]

_SPEC_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?")
_RELEASE_RE = re.compile(r"\d+(?:\.\d+)*")

C_OK, C_WARN, C_ERR, C_DIM, C_OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"

# seconds, for the quick local helper commands ('uv python list', icacls) --
# --timeout only covers the uv calls that resolve/lock
_HELPER_TIMEOUT = 60

_quiet = False


def _color_enabled(stream) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def say(msg: str, color: str = "") -> None:
    # Fehler/Warnungen auf stderr: sonst verschwinden sie beim Umleiten von
    # stdout (z. B. 'uv-refresh --dry-run > log.txt') spurlos. --quiet
    # unterdrueckt nur den Status-Output, nie Warnungen/Fehler.
    if _quiet and color not in (C_WARN, C_ERR):
        return
    stream = sys.stderr if color in (C_WARN, C_ERR) else sys.stdout
    text = f"{color}{msg}{C_OFF}" if color and _color_enabled(stream) else msg
    # flush: piped stdout is block-buffered, stderr isn't -- in
    # 'uv-refresh > log.txt 2>&1' warnings otherwise landed above the status
    # lines they belong to
    print(text, file=stream, flush=True)


def die(msg: str) -> NoReturn:
    say(f"ERROR: {msg}", C_ERR)
    sys.exit(1)


def strip_version(spec: str, keep_extras: bool = True, keep_markers: bool = True) -> str | None:
    """Schneidet NUR die Versionsangabe ab.

    'fastapi[standard]>=0.110; python_version<"3.13"'
        -> 'fastapi[standard]; python_version < "3.13"'

    Extras und Marker sind keine Versionsinformation: Extras bestimmen, WAS
    installiert wird, Marker WO. Beides bleibt darum per Default erhalten.
    Direkte Quellen ('paket @ git+https://...') bleiben komplett unveraendert,
    sonst waere das Package hinterher nicht mehr auffindbar.
    """
    spec = spec.strip()
    if not spec:
        return None

    if Requirement is not None:
        try:
            req = Requirement(spec)
        except InvalidRequirement:
            req = None
        if req is not None:
            if req.url:
                return spec
            out = req.name
            if keep_extras and req.extras:
                out += "[" + ",".join(sorted(req.extras)) + "]"
            if keep_markers and req.marker:
                out += f"; {req.marker}"
            return out

    # Fallback, falls packaging fehlt
    head, sep, marker = spec.partition(";")
    if "@" in head:
        return spec
    m = _SPEC_RE.match(head)
    if not m:
        return None
    out = m.group("name")
    if keep_extras and m.group("extras"):
        out += m.group("extras").replace(" ", "")
    if keep_markers and sep and marker.strip():
        out += f"; {marker.strip()}"
    return out


def specs_from(entries: list, keep_extras: bool, keep_markers: bool) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):  # z. B. {include-group = "..."}
            say(f"  skipped (not a PEP 508 string): {entry!r}", C_WARN)
            continue
        spec = strip_version(entry, keep_extras, keep_markers)
        if not spec or spec.lower() in seen:
            continue
        seen.add(spec.lower())
        if "@" in spec:
            say(f"  {spec}: direct reference, left unchanged", C_WARN)
        out.append(spec)
    return out


def resolve_groups(raw_groups: dict, keep_extras: bool, keep_markers: bool) -> dict[str, list[str]]:
    """Loest 'include-group'-Eintraege (PEP 735) auf, bevor Specs eingesammelt werden.

    Ohne das hier wuerde specs_from() jeden {include-group = "..."}-Eintrag nur
    ueberspringen (mit Warnung) und die darueber eingebundenen Pakete
    stillschweigend aus der neuen Gruppe verlieren.
    """
    flat: dict[str, list] = {}

    def expand(grp: str, chain: tuple[str, ...] = ()) -> list:
        if grp in flat:
            return flat[grp]
        if grp in chain:
            raise RuntimeError(f"dependency-groups: cycle at include-group '{grp}'")
        if grp not in raw_groups:
            raise RuntimeError(f"dependency-groups: include-group '{grp}' does not exist")
        out: list = []
        for entry in raw_groups[grp]:
            if isinstance(entry, dict) and set(entry) == {"include-group"}:
                out += expand(entry["include-group"], (*chain, grp))
            else:
                out.append(entry)
        flat[grp] = out
        return out

    return {grp: specs_from(expand(grp), keep_extras, keep_markers) for grp in raw_groups}


def restrict_to_owner(path: Path) -> None:
    """Keep other local accounts from reading a backup that may hold credentials
    (see ensure_backup_ignored -- direct references like 'pkg @ git+https://user:token@...'
    are copied into the backup unchanged).

    os.chmod cannot express owner/group/other bits on Windows -- it only flips
    the read-only attribute -- so 0o700 there would silently do nothing. Use
    icacls instead, and warn (rather than stay silent) if even that fails.
    """
    if platform.system() != "Windows":
        try:
            os.chmod(path, 0o700)
        except OSError as e:
            say(f"  could not restrict backup permissions: {e}", C_WARN)
        return
    try:
        grant = f"{os.environ.get('USERNAME', '')}:(OI)(CI)F"
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            text=True,
            check=False,
            timeout=_HELPER_TIMEOUT,
        )
        failed = result.returncode != 0
    except subprocess.TimeoutExpired:
        failed = True
    if failed:
        say(
            f"  could not restrict backup permissions ({path} may be readable "
            "by other local accounts; it may contain credentials)",
            C_WARN,
        )


def prune_backups(root: Path, keep: int) -> None:
    """Removes the oldest backups beyond 'keep' (timestamp names sort chronologically).

    keep <= 0 disables pruning -- backups accumulate forever otherwise, each one
    a place credentials from a direct reference could still be sitting.
    """
    if keep <= 0:
        return
    base = root / ".uv-refresh-backup"
    if not base.is_dir():
        return
    stamps = sorted(p for p in base.iterdir() if p.is_dir())
    for old in stamps[:-keep]:
        try:
            shutil.rmtree(old)
        except OSError as e:
            say(f"  could not remove old backup {old}: {e}", C_WARN)


def ensure_backup_ignored(root: Path) -> None:
    """Traegt '.uv-refresh-backup/' und '.uv-refresh-tmp-*/' in die .gitignore
    ein, falls root ein Git-Repo ist.

    Das Backup kann unveraendert uebernommene Direktquellen enthalten, z. B.
    'pkg @ git+https://user:token@...' (siehe strip_version) -- ohne Eintrag
    landet das leicht im naechsten 'git add .'. Dasselbe gilt fuer das
    Temp-Build-Verzeichnis, das nach einem harten Abbruch (kill, Absturz)
    liegen bleibt, statt aufgeraeumt zu werden.
    """
    if not (root / ".git").is_dir():
        return
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    missing = [e for e in (".uv-refresh-backup/", ".uv-refresh-tmp-*/") if e not in text.splitlines()]
    if not missing:
        return
    with gitignore.open("a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.writelines(f"{e}\n" for e in missing)
    say(f"  added {', '.join(missing)} to .gitignore (backup/temp build may contain credentials)", C_WARN)


def build_init_cmd(name: str | None, requires_python: str | None, description: str | None) -> list[str]:
    """Baut den 'uv init'-Aufruf.

    Bindet Werte mit '=' statt als eigenes Argv-Token: eine description, die
    mit '-' beginnt (z. B. '--experimental'), ist sonst nicht von einem Flag
    zu unterscheiden und uv/clap lehnt den Aufruf mit 'unexpected argument' ab.
    """
    cmd = ["uv", "init", "--bare", "--no-workspace"]
    if name:
        cmd.append(f"--name={name}")
    if requires_python:
        cmd.append(f"--python={requires_python}")
    if description:
        cmd.append(f"--description={description}")
    return cmd


def run(cmd: list[str], cwd: Path, dry: bool, timeout: float | None = None) -> None:
    say(f"  $ {' '.join(cmd)}", C_DIM)
    if dry:
        return
    try:
        result = subprocess.run(cmd, cwd=cwd, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Command ran longer than {timeout:.0f}s and was aborted: {' '.join(cmd)}") from e
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")


def latest_installed_python() -> str | None:
    """Newest stable Python version 'uv python list' can find already
    installed (uv-managed or otherwise) -- the list comes back newest first.
    Deliberately --only-installed: --full re-pins to what's already there,
    it should never trigger a Python download on its own just to figure out
    what the newest version would be.

    Pre-releases like '3.15.0rc2' are skipped: --full turns this version into
    a requires-python floor, and an rc someone installed to try out must
    never become the minimum Python of a published package.
    """
    try:
        result = subprocess.run(
            ["uv", "python", "list", "--only-installed", "--output-format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_HELPER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        installs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    stable = [i["version"] for i in installs if _RELEASE_RE.fullmatch(i["version"])]
    return stable[0] if stable else None


def requires_python_floor(version: str) -> str:
    """Turns an installed interpreter version like '3.13.5' into the '>=3.13'
    floor a fresh 'uv init' would write for it -- major.minor only, no patch,
    so --full doesn't make requires-python any stricter than a real project
    normally would (nobody floors requires-python at an exact patch release).
    """
    major_minor = ".".join(version.split(".")[:2])
    return f">={major_minor}"


def _lower_bound(spec: SpecifierSet) -> Version | None:
    """The highest version 'spec' puts a floor at -- '>=3.11', '>3.11',
    '~=3.11' and '==3.11.*' all floor at 3.11. None if it has no floor at
    all (empty, or only '<'/'!=' clauses)."""
    bounds = []
    for s in spec:
        if s.operator in (">=", ">", "~=", "==", "==="):
            with contextlib.suppress(InvalidVersion):  # '===' may hold any string
                bounds.append(Version(s.version.removesuffix(".*")))
    return max(bounds, default=None)


def bumped_requires_python(current: SpecifierSet, version: str) -> str | None:
    """--full: the requires-python to write for 'version' -- its major.minor
    floor (see requires_python_floor), but only if that's actually HIGHER
    than the current floor. None means keep requires-python as it is:
    writing the floor anyway could LOWER it, e.g. '>=3.15' -> '>=3.14' on a
    machine whose newest Python happens to be 3.14 -- silently widening what
    the project claims to support.
    """
    floor = requires_python_floor(version)
    bound = _lower_bound(current)
    if bound is not None and Version(floor.removeprefix(">=")) <= bound:
        return None
    return floor


def plan_full(requires_python: str | None) -> tuple[str | None, str | None]:
    """--full, decided once before anything is touched: returns (the new
    requires-python, or None to keep it; the version to re-pin
    .python-version to, or None to leave it), and says which it is.

    A requires-python this can't evaluate -- invalid, or packaging missing
    (see the import at the top) -- is left alone rather than guessed at.
    """
    unchanged = "leaving requires-python/.python-version unchanged"
    if SpecifierSet is None:
        say(f"\n--full: needs the 'packaging' module (a uv-refresh dependency), {unchanged}", C_WARN)
        return None, None
    latest = latest_installed_python()
    if latest is None:
        say(
            f"\n--full: no installed Python found (uv python list, pre-releases skipped), {unchanged}",
            C_WARN,
        )
        return None, None
    try:
        current = SpecifierSet(requires_python or "")
    except InvalidSpecifier:
        say(f"\n--full: can't parse requires-python {requires_python!r}, {unchanged}", C_WARN)
        return None, None

    if new := bumped_requires_python(current, latest):
        # the bump replaces the whole specifier, so only the new floor
        # (which 'latest' satisfies by construction) matters for the pin
        say(
            f"\n--full: requires-python will be bumped to {new} and .python-version re-pinned to {latest}.",
            C_DIM,
        )
        return new, latest
    if latest in current:
        say(
            f"\n--full: requires-python {requires_python} already starts at Python {latest}'s minor "
            f"version or above, kept as is; .python-version will be re-pinned to {latest}.",
            C_DIM,
        )
        return None, latest
    # requires-python is kept, but excludes 'latest' (older than its floor,
    # or e.g. an exact '==3.13' with 3.13.5 installed): 'uv python pin' would
    # refuse, and lowering or loosening requires-python to make it fit is
    # not ours to decide
    say(
        f"\n--full: newest installed Python {latest} doesn't satisfy requires-python "
        f"{requires_python}, {unchanged}",
        C_WARN,
    )
    return None, None


def refresh_python_version(root: Path, dry: bool, version: str) -> None:
    """--full: drops whatever .python-version currently pins and re-pins the
    project to 'version' (the newest installed Python -- plan_full() already
    looked it up, checked requires-python allows it, and bumped
    requires-python in the same rebuild where that was needed).

    No separate delete step: 'uv python pin' overwrites an existing pin (or
    creates a fresh one) directly, and -- crucially -- refuses to write
    anything at all if the target version doesn't satisfy requires-python.

    Runs AFTER build_and_swap(), not before: requires-python is bumped (if
    needed) as part of that same atomic pyproject.toml rebuild, so it
    already allows 'version' by the time this pin runs. Pinning first (the
    old order) would fail here whenever --full jumps to a Python newer than
    the OLD requires-python permitted -- exactly the case --full is for.
    """
    run(["uv", "python", "pin", version], root, dry)


def pin_build_interpreter(build_dir: Path, root: Path, pin_python: str | None) -> None:
    """Pins the temp build to whichever interpreter 'uv add' should actually
    resolve/install against, instead of whatever's newest installed.

    'uv init --python=<requires-python floor>' (see build_and_swap) only
    writes that floor into requires-python -- it does not pin an exact
    interpreter (confirmed: a bare init with --python='>=3.11' leaves no
    .python-version behind). Left alone, 'uv add' below then picks the
    NEWEST installed Python satisfying that floor, even when the real
    project's own .python-version already pins an older one. If a dependency
    (e.g. torch) has no wheel for that newer version, 'uv add' fails here
    even though the real project works fine on its actual pinned interpreter.

    --full is the exception: when plan_full() decided to re-pin the project
    to pin_python, the temp build should resolve against THAT version, not
    the old pin it's about to replace.
    """
    if pin_python:
        version = pin_python
    else:
        existing = root / ".python-version"
        if not existing.is_file():
            return
        version = existing.read_text(encoding="utf-8").strip()
        if not version:
            return
    (build_dir / ".python-version").write_text(version + "\n", encoding="utf-8")


def _toml_array(values: list[str]):
    arr = tomlkit.array()
    for v in values:
        arr.append(v)
    if values:
        arr.multiline(True)
    return arr


def _toml_group_table(groups: dict[str, list[str]]):
    table = tomlkit.table()
    for grp, specs in groups.items():
        table[grp] = _toml_array(specs)
    return table


def merge_dependencies(
    original_text: str,
    new_deps: list[str],
    new_extras: dict[str, list[str]],
    new_groups: dict[str, list[str]],
    new_requires_python: str | None = None,
) -> str:
    """Schreibt frisch aufgeloeste Dependencies in eine Kopie der ORIGINALEN
    pyproject.toml, statt sie in eine von 'uv init --bare' neu angelegte
    zurueckzupatchen.

    dependencies/optional-dependencies/dependency-groups werden ersetzt (oder
    entfernt, wenn nichts mehr uebrig ist -- z. B. durch --no-groups). Alles
    andere -- description, readme, license, authors, keywords,
    [project.urls], [project.scripts], [build-system], [tool.*], ... -- wird
    nie angefasst, weil es nie geloescht wurde. tomlkit erhaelt dabei
    Formatierung und Kommentare des Originals, statt es platt neu zu
    serialisieren.

    new_requires_python is the one exception to "leave everything but deps
    alone": --full passes it (see build_and_swap) to bump requires-python
    alongside the dependency refresh; every other caller leaves it None and
    requires-python stays whatever the original had.
    """
    doc = tomlkit.parse(original_text)
    doc["project"]["dependencies"] = _toml_array(new_deps)

    if new_requires_python:
        doc["project"]["requires-python"] = new_requires_python

    if new_extras:
        doc["project"]["optional-dependencies"] = _toml_group_table(new_extras)
    elif "optional-dependencies" in doc["project"]:
        del doc["project"]["optional-dependencies"]

    if new_groups:
        doc["dependency-groups"] = _toml_group_table(new_groups)
    elif "dependency-groups" in doc:
        del doc["dependency-groups"]

    return tomlkit.dumps(doc)


def seed_build_pyproject(bare_text: str, specs: ProjectSpecs) -> str:
    """Prepares the bare 'uv init' pyproject.toml in the temp directory for
    'uv add': carries over the original [tool.uv] table (indexes, sources,
    constraint/override deps, conflicts, ...) and gives every extra/group an
    empty list up front, so sources or conflicts that name one still
    validate before its own 'uv add' has run.

    Without [tool.uv], 'uv add' resolved against plain PyPI: a project with
    'constraint-dependencies = ["packaging<25"]' got 'packaging>=26.3'
    written (and the final 'uv lock' then failed), and a path-sourced
    'mylib' got its bound from an unrelated 'mylib' on PyPI (both reproduced
    against real uv). Only this temp copy is seeded -- merge_dependencies()
    still starts from the original text.
    """
    doc = tomlkit.parse(bare_text)
    if specs.tool_uv:
        doc.setdefault("tool", tomlkit.table(is_super_table=True))["uv"] = specs.tool_uv
    for name in specs.extras:
        doc["project"].setdefault("optional-dependencies", tomlkit.table())[name] = []
    if specs.groups:
        doc["dependency-groups"] = {name: [] for name in specs.groups}
    return tomlkit.dumps(doc)


# fields uv needs to lock a project -- if any is dynamic, it has to build it
_DYNAMIC_NEEDS_BUILD = ("dependencies", "optional-dependencies", "requires-python", "version")


def unsupported_reason(data: dict) -> str | None:
    """Why the temp directory can't rebuild this project, or None if it can.

    The temp directory holds only pyproject.toml (+ uv.lock), one level below
    the project, so relative paths ('path'/workspace sources, workspace
    members, '${PROJECT_ROOT}' references) resolve to the wrong place, and
    metadata that only a build can produce (a dynamic version or
    dependencies) fails for lack of the project's files. All of these used to
    fail only at the final 'uv lock' -- after the backup, with a uv or
    build-backend error that didn't say why (each reproduced against real
    uv). Absolute 'path' sources and a dynamic readme work fine and pass.
    """
    project = data.get("project", {})
    tool_uv = data.get("tool", {}).get("uv", {})
    if "workspace" in tool_uv:
        return "it's a uv workspace ([tool.uv.workspace])"
    for name, source in (tool_uv.get("sources") or {}).items():
        for entry in source if isinstance(source, list) else [source]:
            if not isinstance(entry, dict):
                continue
            if entry.get("workspace"):
                return f"'{name}' is a workspace source"
            if "path" in entry and not Path(entry["path"]).is_absolute():
                return f"'{name}' has a relative path source ({entry['path']!r})"
    deps = [
        *project.get("dependencies", []),
        *(d for ds in (project.get("optional-dependencies") or {}).values() for d in ds),
        *(d for ds in (data.get("dependency-groups") or {}).values() for d in ds),
    ]
    if any(isinstance(d, str) and "${PROJECT_ROOT}" in d for d in deps):
        return "a dependency points into the project via ${PROJECT_ROOT}"
    if dynamic := [f for f in _DYNAMIC_NEEDS_BUILD if f in project.get("dynamic", [])]:
        return f"it lists {', '.join(dynamic)} as dynamic (computed by a build)"
    return None


@dataclass
class ProjectSpecs:
    """Everything pulled out of the original pyproject.toml that's needed to
    rebuild it: identity for 'uv init', and version-stripped deps for 'uv add'."""

    name: str | None
    requires_python: str | None
    description: str | None
    main_deps: list[str]
    extras: dict[str, list[str]]
    groups: dict[str, list[str]]
    had_groups: bool  # optional-dependencies/dependency-groups existed in the original,
    # independent of --no-groups -- used for the removal warning below
    tool_uv: dict  # the original [tool.uv] table, see seed_build_pyproject()


def load_project_specs(pyproject: Path, args: argparse.Namespace) -> tuple[str, ProjectSpecs]:
    """Reads pyproject.toml and extracts what 'uv init'/'uv add' need to
    re-resolve every dependency. Exits via die() on anything unusable."""
    try:
        original_text = pyproject.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        die(f"pyproject.toml is not UTF-8 encoded: {e}")
    try:
        data = tomllib.loads(original_text)
    except tomllib.TOMLDecodeError as e:
        die(f"pyproject.toml is not valid TOML: {e}")

    project = data.get("project", {})
    if not project:
        die("No [project] section found. Is this a uv/PEP 621 project?")

    # e.g. 'requires-python = 3.11' (a TOML float): uv rejects the project
    # anyway, and --full's specifier helpers would crash on it -- say so up
    # front instead, before any backup is made
    requires_python = project.get("requires-python")
    if requires_python is not None and not isinstance(requires_python, str):
        die(f'requires-python must be a string like ">=3.11", not {requires_python!r}')

    if reason := unsupported_reason(data):
        die(
            f"Can't refresh this project: {reason}.\n"
            "  uv-refresh resolves in a temp directory that holds only pyproject.toml -- "
            "relative paths point elsewhere from there, and build-time metadata needs the project's files."
        )

    keep_extras, keep_markers = not args.drop_extras, not args.drop_markers
    main_deps = specs_from(project.get("dependencies", []), keep_extras, keep_markers)

    had_groups = bool(project.get("optional-dependencies") or data.get("dependency-groups"))

    extras: dict[str, list[str]] = {}
    groups: dict[str, list[str]] = {}
    if not args.no_groups:
        for grp, specs in (project.get("optional-dependencies") or {}).items():
            extras[grp] = specs_from(specs, keep_extras, keep_markers)
        raw_groups = data.get("dependency-groups") or {}
        if raw_groups:
            try:
                groups = resolve_groups(raw_groups, keep_extras, keep_markers)
            except RuntimeError as e:
                die(str(e))

    if not main_deps and not extras and not groups:
        die("No dependencies found. Nothing to do.")

    return original_text, ProjectSpecs(
        name=project.get("name"),
        requires_python=requires_python,
        description=project.get("description"),
        main_deps=main_deps,
        extras=extras,
        groups=groups,
        had_groups=had_groups,
        tool_uv=data.get("tool", {}).get("uv", {}),
    )


def build_and_swap(
    root: Path,
    pyproject: Path,
    lock: Path,
    backup: Path,
    original_text: str,
    specs: ProjectSpecs,
    args: argparse.Namespace,
    new_requires_python: str | None = None,
    pin_python: str | None = None,
) -> None:
    """Runs steps 2-6: backup, 'uv init' + 'uv add' in a temp directory, merge
    the freshly resolved dependencies into a copy of the ORIGINAL
    pyproject.toml, then atomically swap it into place.

    pyproject.toml/uv.lock are only ever touched by the final atomic swap, so
    if this raises (including on KeyboardInterrupt), they are guaranteed
    unchanged -- 'backup' is kept regardless, as an extra safety net.

    new_requires_python is only set when --full decided requires-python has
    to go up (see plan_full()); it's then written as
    part of this same atomic rebuild, and 'uv init' below targets it too --
    main() re-pins .python-version afterwards, once requires-python already
    allows that pin. pin_python (also from plan_full()) is the version that
    pin will use; the temp build already resolves against it, see
    pin_build_interpreter().
    """
    build_ctx = (
        tempfile.TemporaryDirectory(dir=root, prefix=".uv-refresh-tmp-")
        if not args.dry_run
        else contextlib.nullcontext(root)
    )

    if not args.dry_run:
        ensure_backup_ignored(root)
        backup.mkdir(parents=True, exist_ok=True)
        restrict_to_owner(backup)
        shutil.copy2(pyproject, backup / "pyproject.toml")
        if lock.is_file():
            shutil.copy2(lock, backup / "uv.lock")
        prune_backups(root, args.keep_backups)

    with build_ctx as build_dir_raw:
        build_dir = Path(build_dir_raw)
        if not args.dry_run and args.keep_lock and lock.is_file():
            shutil.copy2(lock, build_dir / "uv.lock")

        # ---- 3. uv init -----------------------------------------------
        init_python = new_requires_python or specs.requires_python
        init = build_init_cmd(specs.name, init_python, specs.description)
        try:
            run(init, build_dir, args.dry_run, args.timeout)
        except RuntimeError:
            if not any(f.startswith("--python=") for f in init):
                raise
            say("  uv init with --python failed, retrying without it", C_WARN)
            run([f for f in init if not f.startswith("--python=")], build_dir, args.dry_run, args.timeout)

        if args.dry_run:
            if specs.tool_uv:
                say("  $ (the project's [tool.uv] settings would now go into the temp pyproject.toml)", C_DIM)
        else:
            pin_build_interpreter(build_dir, root, pin_python)
            if specs.tool_uv or specs.extras or specs.groups:
                bare = build_dir / "pyproject.toml"
                seeded = seed_build_pyproject(bare.read_text(encoding="utf-8"), specs)
                bare.write_text(seeded, encoding="utf-8")

        # ---- 4. uv add --------------------------------------------------
        # --no-sync: only the resolved versions matter here. Syncing installed
        # every dependency into a temp .venv that's deleted right after (GBs
        # for e.g. torch), and that venv's interpreter then made the final
        # 'uv lock' reject exact pins like 'requires-python = "==3.14"'.
        add = ["uv", "add", "--no-sync"]
        flags: list[str] = []
        if args.raw:
            flags.append("--raw")
        if args.bounds:
            flags += ["--bounds", args.bounds]

        if specs.main_deps:
            run([*add, *flags, *specs.main_deps], build_dir, args.dry_run, args.timeout)
        for grp, deps in specs.extras.items():
            if deps:
                run([*add, "--optional", grp, *flags, *deps], build_dir, args.dry_run, args.timeout)
        for grp, deps in specs.groups.items():
            if deps:
                run([*add, "--group", grp, *flags, *deps], build_dir, args.dry_run, args.timeout)

        # ---- 5. Dependencies in die ORIGINALE pyproject.toml einmergen ----
        if not args.dry_run:
            built = tomllib.loads((build_dir / "pyproject.toml").read_text(encoding="utf-8"))
            built_project = built.get("project", {})
            merged = merge_dependencies(
                original_text,
                built_project.get("dependencies", []),
                built_project.get("optional-dependencies", {}),
                built.get("dependency-groups", {}),
                new_requires_python,
            )
            (build_dir / "pyproject.toml").write_text(merged, encoding="utf-8")

            # uv.lock im Temp-Verzeichnis wurde bisher gegen die BARE uv-init-Datei
            # aufgeloest (kein [build-system], keine Version -> virtual/0.1.0). Die
            # eben gemergte pyproject.toml hat jetzt wieder Version/[build-system]/etc.
            # aus dem Original -- ohne Neuauflösung hier würde genau diese veraltete
            # Lock-Datei gleich unveraendert ins echte Projekt geswapt.
            run(["uv", "lock"], build_dir, args.dry_run, args.timeout)

        # ---- 6. atomarer Tausch -------------------------------------------
        # root blieb bis hierher unveraendert: schlaegt oben etwas fehl
        # (auch per Ctrl+C), gibt es nichts zurueckzuholen.
        if not args.dry_run:
            os.replace(build_dir / "pyproject.toml", pyproject)
            new_lock = build_dir / "uv.lock"
            if new_lock.is_file():
                os.replace(new_lock, lock)
        else:
            say("  $ (pyproject.toml/uv.lock would now be atomically replaced)", C_DIM)


def main() -> int:
    p = argparse.ArgumentParser(
        prog="uv-refresh", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--path", default=".", help="project directory (default: current)")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen, change nothing")
    p.add_argument("--yes", "-y", action="store_true", help="run without asking for confirmation")
    p.add_argument(
        "--verbose", "-v", action="store_true", help="print the full new pyproject.toml at the end"
    )
    p.add_argument("--quiet", "-q", action="store_true", help="only print warnings/errors, no status output")
    p.add_argument(
        "--timeout", type=float, default=300.0, help="timeout in seconds per uv call (default: 300)"
    )
    p.add_argument(
        "--keep-lock", action="store_true", help="keep uv.lock (uv will then prefer the old versions!)"
    )
    p.add_argument(
        "--keep-backups",
        type=int,
        default=5,
        metavar="N",
        help="how many past backups to keep, oldest deleted first (default: 5, 0 keeps all)",
    )
    p.add_argument(
        "--no-groups",
        action="store_true",
        help="don't carry over optional-dependencies and dependency-groups",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="also bump requires-python and re-pin .python-version to the newest installed Python",
    )
    p.add_argument(
        "--drop-extras",
        action="store_true",
        help="drop extras: fastapi[standard] becomes fastapi (rarely useful!)",
    )
    p.add_argument(
        "--drop-markers", action="store_true", help="drop environment markers, e.g. ; sys_platform == 'win32'"
    )
    bounds_group = p.add_mutually_exclusive_group()
    bounds_group.add_argument(
        "--raw", action="store_true", help="add package names with no version bound at all (uv add --raw)"
    )
    bounds_group.add_argument(
        "--bounds",
        choices=["lower", "major", "minor", "exact"],
        help="kind of version bound uv add sets (uv preview feature)",
    )
    args = p.parse_args()

    global _quiet
    _quiet = args.quiet

    root = Path(args.path).resolve()
    pyproject = root / "pyproject.toml"
    lock = root / "uv.lock"

    if not shutil.which("uv"):
        die("uv is not on PATH.")
    if not pyproject.is_file():
        die(f"No pyproject.toml in {root}")

    # ---- 1. lesen ---------------------------------------------------------
    original_text, specs = load_project_specs(pyproject, args)

    say(f"\nProject: {specs.name or '(no name)'}   [{root}]", C_OK)
    say(f"  dependencies      : {', '.join(specs.main_deps) or '-'}")
    for g, n in specs.extras.items():
        say(f"  optional [{g}]    : {', '.join(n)}")
    for g, n in specs.groups.items():
        say(f"  group [{g}]       : {', '.join(n)}")

    # ---- Was sich aendert ---------------------------------------------------
    # dependencies/optional-dependencies/dependency-groups werden ersetzt,
    # alles andere (description, readme, license, authors, keywords,
    # [project.urls], [project.scripts], [build-system], [tool.*], ...)
    # bleibt unangetastet -- siehe merge_dependencies().
    if args.no_groups and specs.had_groups:
        say(
            "\n--no-groups: existing optional-dependencies/dependency-groups "
            "will be removed from the new pyproject.toml.",
            C_WARN,
        )

    # --full is decided once, here (read-only 'uv python list') rather than
    # inside build_and_swap(): the dry-run/confirm messages below need it
    # already, and the requires-python bump and the pin after the swap must
    # both use the SAME lookup -- a second one could return something
    # different (e.g. a Python installed in between).
    new_requires_python, pin_python = plan_full(specs.requires_python) if args.full else (None, None)

    if args.dry_run:
        say("\n--dry-run: from here on, this would happen:", C_DIM)
    elif not args.yes:
        prompt = "Rebuild pyproject.toml (and re-pin Python) now? [Y/N] " if pin_python \
            else "Rebuild pyproject.toml now? [Y/N] "
        try:
            answer = input(f"\n{prompt}").strip().lower()
        except EOFError:
            die("No input possible (no terminal). Use --yes to run without confirmation.")
        if answer not in ("y", "yes"):
            say("Aborted.", C_DIM)
            return 1

    # ---- 2. Backup --------------------------------------------------------
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    backup = root / ".uv-refresh-backup" / stamp
    say(f"\nBackup -> {backup}", C_DIM)
    say(
        "Building in a temp directory; your real pyproject.toml/uv.lock stay untouched until the final step.",
        C_DIM,
    )

    try:
        build_and_swap(
            root, pyproject, lock, backup, original_text, specs, args, new_requires_python, pin_python
        )
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 -- Notbremse: bei
        # JEDEM Fehler (uv, Dateisystem, Interrupt, ...) klar melden. Der Aufbau
        # geschah in einem Temp-Verzeichnis, root ist daher normalerweise
        # unveraendert; das Backup bleibt als zusaetzliches Netz trotzdem liegen.
        say(f"\n{e}", C_ERR)
        if not args.dry_run:
            say(f"pyproject.toml unchanged. Backup is at {backup}.", C_WARN)
        return 1

    # ---- 7. .python-version ------------------------------------------------
    # (steps 2-6 are build_and_swap's) Only after the atomic swap above: if
    # requires-python had to go up to allow pin_python, the real
    # pyproject.toml has that bump by now, so 'uv python pin' passes its own
    # requires-python check instead of failing against the OLD (pre-swap)
    # constraint.
    if pin_python:
        try:
            refresh_python_version(root, args.dry_run, pin_python)
        except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 -- siehe oben:
            # pyproject.toml/uv.lock were already swapped successfully above;
            # only the .python-version pin itself failed here.
            say(f"\n{e}", C_ERR)
            if not args.dry_run:
                say(
                    "pyproject.toml/uv.lock were already refreshed; only the .python-version pin failed.",
                    C_WARN,
                )
            return 1

    if args.dry_run:
        say("\n--dry-run: nothing was changed.", C_OK)
        return 0

    say("\nDone.", C_OK)
    if args.verbose and pyproject.is_file():
        say(pyproject.read_text(encoding="utf-8"), C_DIM)
    return 0


if __name__ == "__main__":
    sys.exit(main())
