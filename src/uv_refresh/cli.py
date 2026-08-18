"""uv-refresh
==========

Rebuilds a uv project's pyproject.toml so every dependency gets freshly
resolved instead of dragging along old version pins.

Steps:
  1. Read pyproject.toml and collect the dependencies without their version
     specifiers (extras and environment markers are kept, see --drop-extras)
  2. Back up pyproject.toml + uv.lock into a backup folder
  3. Run uv init --bare + uv add <names> in a temp directory next to the
     project -- the real pyproject.toml stays untouched the whole time
  4. Merge only dependencies/optional-dependencies/dependency-groups from
     the result into a copy of the ORIGINAL pyproject.toml -- everything
     else (description, readme, license, authors, keywords, [project.urls],
     [project.scripts], [build-system], [tool.*], ...) stays untouched
  5. Atomically swap the result in place of the old pyproject.toml/uv.lock

If any step fails -- including Ctrl+C -- the real pyproject.toml was never
touched, since the whole build happened in the temp directory. The backup
is kept around as an extra reference regardless.

Usage:
  uv-refresh                 # in the project directory, asks for confirmation
  uv-refresh --dry-run       # just show what would happen, touch nothing
  uv-refresh --raw           # add packages with no version bound at all
  uv-refresh --path ../other # a different project directory
"""

from __future__ import annotations

import argparse
import contextlib
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

_SPEC_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?")

C_OK, C_WARN, C_ERR, C_DIM, C_OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"

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
    print(text, file=stream)


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
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r",
         f"{os.environ.get('USERNAME', '')}:(OI)(CI)F"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        say(f"  could not restrict backup permissions ({path} may be readable "
            "by other local accounts; it may contain credentials)", C_WARN)


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
    """Traegt '.uv-refresh-backup/' in die .gitignore ein, falls root ein Git-Repo ist.

    Das Backup kann unveraendert uebernommene Direktquellen enthalten, z. B.
    'pkg @ git+https://user:token@...' (siehe strip_version) -- ohne Eintrag
    landet das leicht im naechsten 'git add .'.
    """
    if not (root / ".git").is_dir():
        return
    entry = ".uv-refresh-backup/"
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if entry in text.splitlines():
        return
    with gitignore.open("a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"{entry}\n")
    say(f"  added {entry} to .gitignore (backup may contain credentials)", C_WARN)


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
        raise RuntimeError(
            f"Command ran longer than {timeout:.0f}s and was aborted: {' '.join(cmd)}"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")


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


def merge_dependencies(original_text: str, new_deps: list[str],
                        new_extras: dict[str, list[str]], new_groups: dict[str, list[str]]) -> str:
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
    """
    doc = tomlkit.parse(original_text)
    doc["project"]["dependencies"] = _toml_array(new_deps)

    if new_extras:
        doc["project"]["optional-dependencies"] = _toml_group_table(new_extras)
    elif "optional-dependencies" in doc["project"]:
        del doc["project"]["optional-dependencies"]

    if new_groups:
        doc["dependency-groups"] = _toml_group_table(new_groups)
    elif "dependency-groups" in doc:
        del doc["dependency-groups"]

    return tomlkit.dumps(doc)


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
        requires_python=project.get("requires-python"),
        description=project.get("description"),
        main_deps=main_deps,
        extras=extras,
        groups=groups,
        had_groups=had_groups,
    )


def build_and_swap(root: Path, pyproject: Path, lock: Path, backup: Path,
                    original_text: str, specs: ProjectSpecs, args: argparse.Namespace) -> None:
    """Runs steps 2-6: backup, 'uv init' + 'uv add' in a temp directory, merge
    the freshly resolved dependencies into a copy of the ORIGINAL
    pyproject.toml, then atomically swap it into place.

    pyproject.toml/uv.lock are only ever touched by the final atomic swap, so
    if this raises (including on KeyboardInterrupt), they are guaranteed
    unchanged -- 'backup' is kept regardless, as an extra safety net.
    """
    build_ctx = (tempfile.TemporaryDirectory(dir=root, prefix=".uv-refresh-tmp-")
                 if not args.dry_run else contextlib.nullcontext(root))

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
        init = build_init_cmd(specs.name, specs.requires_python, specs.description)
        try:
            run(init, build_dir, args.dry_run, args.timeout)
        except RuntimeError:
            if not any(f.startswith("--python=") for f in init):
                raise
            say("  uv init with --python failed, retrying without it", C_WARN)
            run([f for f in init if not f.startswith("--python=")],
                build_dir, args.dry_run, args.timeout)

        # ---- 4. uv add --------------------------------------------------
        flags: list[str] = []
        if args.raw:
            flags.append("--raw")
        if args.bounds:
            flags += ["--bounds", args.bounds]

        if specs.main_deps:
            run(["uv", "add", *flags, *specs.main_deps], build_dir, args.dry_run, args.timeout)
        for grp, deps in specs.extras.items():
            if deps:
                run(["uv", "add", "--optional", grp, *flags, *deps],
                    build_dir, args.dry_run, args.timeout)
        for grp, deps in specs.groups.items():
            if deps:
                run(["uv", "add", "--group", grp, *flags, *deps],
                    build_dir, args.dry_run, args.timeout)

        # ---- 5. Dependencies in die ORIGINALE pyproject.toml einmergen ----
        if not args.dry_run:
            built = tomllib.loads((build_dir / "pyproject.toml").read_text(encoding="utf-8"))
            built_project = built.get("project", {})
            merged = merge_dependencies(
                original_text,
                built_project.get("dependencies", []),
                built_project.get("optional-dependencies", {}),
                built.get("dependency-groups", {}),
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
    p = argparse.ArgumentParser(prog="uv-refresh", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--path", default=".", help="project directory (default: current)")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen, change nothing")
    p.add_argument("--yes", "-y", action="store_true", help="run without asking for confirmation")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print the full new pyproject.toml at the end")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="only print warnings/errors, no status output")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="timeout in seconds per uv call (default: 300)")
    p.add_argument("--keep-lock", action="store_true",
                   help="keep uv.lock (uv will then prefer the old versions!)")
    p.add_argument("--keep-backups", type=int, default=5, metavar="N",
                   help="how many past backups to keep, oldest deleted first "
                        "(default: 5, 0 keeps all)")
    p.add_argument("--no-groups", action="store_true",
                   help="don't carry over optional-dependencies and dependency-groups")
    p.add_argument("--drop-extras", action="store_true",
                   help="drop extras: fastapi[standard] becomes fastapi (rarely useful!)")
    p.add_argument("--drop-markers", action="store_true",
                   help="drop environment markers, e.g. ; sys_platform == 'win32'")
    bounds_group = p.add_mutually_exclusive_group()
    bounds_group.add_argument("--raw", action="store_true",
                   help="add package names with no version bound at all (uv add --raw)")
    bounds_group.add_argument("--bounds", choices=["lower", "major", "minor", "exact"],
                   help="kind of version bound uv add sets (uv preview feature)")
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
        say("\n--no-groups: existing optional-dependencies/dependency-groups "
            "will be removed from the new pyproject.toml.", C_WARN)

    if args.dry_run:
        say("\n--dry-run: from here on, this would happen:", C_DIM)
    elif not args.yes:
        try:
            answer = input("\nRebuild pyproject.toml now? [y/N] ").strip().lower()
        except EOFError:
            die("No input possible (no terminal). Use --yes to run without confirmation.")
        if answer not in ("y", "yes"):
            say("Aborted.", C_DIM)
            return 1

    # ---- 2. Backup --------------------------------------------------------
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    backup = root / ".uv-refresh-backup" / stamp
    say(f"\nBackup -> {backup}", C_DIM)
    say("Building in a temp directory; your real pyproject.toml/uv.lock "
        "stay untouched until the final step.", C_DIM)

    try:
        build_and_swap(root, pyproject, lock, backup, original_text, specs, args)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 -- Notbremse: bei
        # JEDEM Fehler (uv, Dateisystem, Interrupt, ...) klar melden. Der Aufbau
        # geschah in einem Temp-Verzeichnis, root ist daher normalerweise
        # unveraendert; das Backup bleibt als zusaetzliches Netz trotzdem liegen.
        say(f"\n{e}", C_ERR)
        if not args.dry_run:
            say(f"pyproject.toml unchanged. Backup is at {backup}.", C_WARN)
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
