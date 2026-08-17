"""
uv-refresh
==========

Baut die pyproject.toml eines uv-Projekts neu auf, damit alle Dependencies
frisch aufgeloest werden statt alte Versionsbindungen mitzuschleppen.

Ablauf:
  1. pyproject.toml lesen und die Dependencies ohne Versionsangabe einsammeln
     (Extras und Environment-Marker bleiben erhalten, siehe --drop-extras)
  2. pyproject.toml + uv.lock in einen Backup-Ordner sichern
  3. pyproject.toml (und optional uv.lock) loeschen
  4. uv init --bare   ->  neue, minimale pyproject.toml
  5. uv add <namen>   ->  uv loest die neueste kompatible Version auf

Benutzung:
  uv-refresh                 # im Projektverzeichnis, mit Rueckfrage
  uv-refresh --dry-run       # nur zeigen, nichts anfassen
  uv-refresh --raw           # ganz ohne Versionsangabe eintragen
  uv-refresh --path ../other # anderes Projektverzeichnis
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

try:  # bevorzugt der offizielle PEP-508-Parser
    from packaging.requirements import InvalidRequirement, Requirement
except ModuleNotFoundError:  # Fallback, damit das Skript auch nackt laeuft
    Requirement = None  # type: ignore[assignment]
    InvalidRequirement = ValueError  # type: ignore[assignment,misc]

_SPEC_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?")
_VERSION_LINE_RE = re.compile(r'(?m)^version\s*=\s*"[^"]*"')

C_OK, C_WARN, C_ERR, C_DIM, C_OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def say(msg: str, color: str = "") -> None:
    print(f"{color}{msg}{C_OFF}" if color and _USE_COLOR else msg)


def die(msg: str) -> NoReturn:
    say(f"FEHLER: {msg}", C_ERR)
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
            say(f"  uebersprungen (kein PEP-508-String): {entry!r}", C_WARN)
            continue
        spec = strip_version(entry, keep_extras, keep_markers)
        if not spec or spec.lower() in seen:
            continue
        seen.add(spec.lower())
        if "@" in spec:
            say(f"  {spec}: direkte Quelle, bleibt unveraendert", C_WARN)
        out.append(spec)
    return out


def run(cmd: list[str], cwd: Path, dry: bool) -> None:
    say(f"  $ {' '.join(cmd)}", C_DIM)
    if dry:
        return
    if subprocess.run(cmd, cwd=cwd, check=False).returncode != 0:
        raise RuntimeError(f"Befehl fehlgeschlagen: {' '.join(cmd)}")


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def restore_fields(text: str, version: str | None, readme: str | None) -> str:
    """Setzt version/readme zurueck, die 'uv init --bare' nicht uebernimmt.

    uv init setzt version IMMER auf "0.1.0" (kein --version-Flag vorhanden)
    und schreibt readme nie (nur als Datei, nicht als Feld). Ohne das hier
    wuerde eine bestehende Versionsnummer sonst still auf 0.1.0 zurueckfallen.
    """
    if version:
        text = _VERSION_LINE_RE.sub(f"version = {_toml_string(version)}", text, count=1)
    if readme:
        text = re.sub(r"(?m)^(version\s*=.*)$",
                       lambda m: f"{m.group(1)}\nreadme = {_toml_string(readme)}",
                       text, count=1)
    return text


def main() -> int:
    p = argparse.ArgumentParser(prog="uv-refresh", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--path", default=".", help="Projektverzeichnis (Default: aktuelles)")
    p.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts veraendern")
    p.add_argument("--yes", "-y", action="store_true", help="ohne Rueckfrage durchziehen")
    p.add_argument("--keep-lock", action="store_true",
                   help="uv.lock behalten (dann bevorzugt uv die alten Versionen!)")
    p.add_argument("--no-groups", action="store_true",
                   help="optional-dependencies und dependency-groups nicht uebernehmen")
    p.add_argument("--drop-extras", action="store_true",
                   help="Extras verwerfen: fastapi[standard] wird zu fastapi (selten sinnvoll!)")
    p.add_argument("--drop-markers", action="store_true",
                   help="Environment-Marker verwerfen, z. B. ; sys_platform == 'win32'")
    bounds_group = p.add_mutually_exclusive_group()
    bounds_group.add_argument("--raw", action="store_true",
                   help="Namen komplett ohne Versionsgrenze eintragen (uv add --raw)")
    bounds_group.add_argument("--bounds", choices=["lower", "major", "minor", "exact"],
                   help="Art der Versionsgrenze, die uv add setzt (Preview-Feature von uv)")
    args = p.parse_args()

    root = Path(args.path).resolve()
    pyproject = root / "pyproject.toml"
    lock = root / "uv.lock"

    if not shutil.which("uv"):
        die("uv ist nicht im PATH.")
    if not pyproject.is_file():
        die(f"Keine pyproject.toml in {root}")

    # ---- 1. lesen ---------------------------------------------------------
    try:
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        die(f"pyproject.toml ist kein gueltiges TOML: {e}")
    except UnicodeDecodeError as e:
        die(f"pyproject.toml ist nicht UTF-8-kodiert: {e}")

    project = data.get("project", {})
    if not project:
        die("Kein [project]-Abschnitt gefunden. Ist das ein uv-/PEP-621-Projekt?")

    name = project.get("name")
    requires_python = project.get("requires-python")
    description = project.get("description")
    version = project.get("version")
    readme = project.get("readme")
    keep_extras, keep_markers = not args.drop_extras, not args.drop_markers
    main_deps = specs_from(project.get("dependencies", []), keep_extras, keep_markers)

    extras: dict[str, list[str]] = {}
    groups: dict[str, list[str]] = {}
    if not args.no_groups:
        for grp, specs in (project.get("optional-dependencies") or {}).items():
            extras[grp] = specs_from(specs, keep_extras, keep_markers)
        for grp, specs in (data.get("dependency-groups") or {}).items():
            groups[grp] = specs_from(specs, keep_extras, keep_markers)

    if not main_deps and not extras and not groups:
        die("Keine Dependencies gefunden. Nichts zu tun.")

    say(f"\nProjekt: {name or '(kein Name)'}   [{root}]", C_OK)
    say(f"  dependencies      : {', '.join(main_deps) or '-'}")
    for g, n in extras.items():
        say(f"  optional [{g}]    : {', '.join(n)}")
    for g, n in groups.items():
        say(f"  group [{g}]       : {', '.join(n)}")

    # ---- Was verloren geht ------------------------------------------------
    # version/description werden aktiv wiederhergestellt (s. u.), readme nur
    # als einfacher String -- als Tabelle ({file=..., text=...}) nicht.
    safe_keys = {"name", "version", "description", "requires-python",
                 "dependencies", "optional-dependencies"}
    if isinstance(readme, str):
        safe_keys.add("readme")
    lost = [k for k in project if k not in safe_keys]
    other_tables = [k for k in data if k not in {"project", "dependency-groups"}]
    if lost or other_tables:
        say("\nACHTUNG, diese Konfiguration wird NICHT wiederhergestellt:", C_WARN)
        for k in lost:
            say(f"  [project].{k}", C_WARN)
        for k in other_tables:
            say(f"  [{k}]", C_WARN)
        say("  Das Backup liegt daneben, du musst diese Bloecke von Hand zurueckkopieren.", C_WARN)

    if args.dry_run:
        say("\n--dry-run: ab hier wuerde passieren:", C_DIM)
    elif not args.yes:
        try:
            answer = input("\nWirklich neu aufbauen? [j/N] ").strip().lower()
        except EOFError:
            die("Keine Eingabe moeglich (kein Terminal). Mit --yes ohne Rueckfrage ausfuehren.")
        if answer not in {"j", "y"}:
            say("Abgebrochen.", C_DIM)
            return 1

    # ---- 2. Backup --------------------------------------------------------
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    backup = root / ".uv-refresh-backup" / stamp
    say(f"\nBackup -> {backup}", C_DIM)
    if not args.dry_run:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pyproject, backup / "pyproject.toml")
        if lock.is_file():
            shutil.copy2(lock, backup / "uv.lock")

    try:
        # ---- 3. loeschen --------------------------------------------------
        say("Loesche pyproject.toml" + ("" if args.keep_lock else " und uv.lock"), C_DIM)
        if not args.dry_run:
            pyproject.unlink()
            if lock.is_file() and not args.keep_lock:
                lock.unlink()

        # ---- 4. uv init ---------------------------------------------------
        init = ["uv", "init", "--bare", "--no-workspace"]
        if name:
            init += ["--name", name]
        if requires_python:
            init += ["--python", requires_python]
        if description:
            init += ["--description", description]
        try:
            run(init, root, args.dry_run)
        except RuntimeError:
            if "--python" not in init:
                raise
            say("  uv init mit --python fehlgeschlagen, versuche es ohne", C_WARN)
            i = init.index("--python")
            run(init[:i] + init[i + 2:], root, args.dry_run)

        # ---- 5. uv add ----------------------------------------------------
        flags: list[str] = []
        if args.raw:
            flags.append("--raw")
        if args.bounds:
            flags += ["--bounds", args.bounds]

        if main_deps:
            run(["uv", "add", *flags, *main_deps], root, args.dry_run)
        for grp, deps in extras.items():
            if deps:
                run(["uv", "add", "--optional", grp, *flags, *deps], root, args.dry_run)
        for grp, deps in groups.items():
            if deps:
                run(["uv", "add", "--group", grp, *flags, *deps], root, args.dry_run)

        # ---- 6. version/readme zuruecksetzen -------------------------------
        # uv init setzt version immer auf 0.1.0 und schreibt readme nie, s. restore_fields()
        if not args.dry_run and (version or isinstance(readme, str)):
            text = restore_fields(pyproject.read_text(encoding="utf-8"),
                                   version, readme if isinstance(readme, str) else None)
            pyproject.write_text(text, encoding="utf-8")

    except Exception as e:  # noqa: BLE001 -- Notbremse: bei JEDEM Fehler (uv,
        # Dateisystem, ...) den alten Stand zurueckholen, daher bewusst breit
        say(f"\n{e}", C_ERR)
        if not args.dry_run:
            shutil.copy2(backup / "pyproject.toml", pyproject)
            if (backup / "uv.lock").is_file():
                shutil.copy2(backup / "uv.lock", lock)
            say("pyproject.toml aus dem Backup wiederhergestellt.", C_WARN)
        return 1

    say("\nFertig.", C_OK)
    if not args.dry_run and pyproject.is_file():
        say(pyproject.read_text(encoding="utf-8"), C_DIM)
    return 0


if __name__ == "__main__":
    sys.exit(main())
