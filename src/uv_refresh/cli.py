"""uv-refresh
==========

Baut die pyproject.toml eines uv-Projekts neu auf, damit alle Dependencies
frisch aufgeloest werden statt alte Versionsbindungen mitzuschleppen.

Ablauf:
  1. pyproject.toml lesen und die Dependencies ohne Versionsangabe einsammeln
     (Extras und Environment-Marker bleiben erhalten, siehe --drop-extras)
  2. pyproject.toml + uv.lock in einen Backup-Ordner sichern
  3. uv init --bare + uv add <namen> in einem Temp-Verzeichnis neben dem
     Projekt ausfuehren -- die echte pyproject.toml bleibt dabei unangetastet
  4. Ergebnis atomar an die Stelle der alten pyproject.toml/uv.lock setzen

Schlaegt irgendein Schritt fehl (auch per Ctrl+C), wurde die echte
pyproject.toml nie veraendert -- der komplette Aufbau geschah im
Temp-Verzeichnis. Das Backup bleibt zusaetzlich als Referenz liegen.

Benutzung:
  uv-refresh                 # im Projektverzeichnis, mit Rueckfrage
  uv-refresh --dry-run       # nur zeigen, nichts anfassen
  uv-refresh --raw           # ganz ohne Versionsangabe eintragen
  uv-refresh --path ../other # anderes Projektverzeichnis
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from . import __version__

try:  # bevorzugt der offizielle PEP-508-Parser
    from packaging.requirements import InvalidRequirement, Requirement
except ModuleNotFoundError:  # Fallback, damit das Skript auch nackt laeuft
    Requirement = None  # type: ignore[assignment]
    InvalidRequirement = ValueError  # type: ignore[assignment,misc]

_SPEC_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?")
_VERSION_LINE_RE = re.compile(r'(?m)^version\s*=\s*"[^"]*"')

C_OK, C_WARN, C_ERR, C_DIM, C_OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _color_enabled(stream) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def say(msg: str, color: str = "") -> None:
    # Fehler/Warnungen auf stderr: sonst verschwinden sie beim Umleiten von
    # stdout (z. B. 'uv-refresh --dry-run > log.txt') spurlos.
    stream = sys.stderr if color in (C_WARN, C_ERR) else sys.stdout
    text = f"{color}{msg}{C_OFF}" if color and _color_enabled(stream) else msg
    print(text, file=stream)


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
            raise RuntimeError(f"dependency-groups: Zirkel bei include-group '{grp}'")
        if grp not in raw_groups:
            raise RuntimeError(f"dependency-groups: include-group '{grp}' existiert nicht")
        out: list = []
        for entry in raw_groups[grp]:
            if isinstance(entry, dict) and set(entry) == {"include-group"}:
                out += expand(entry["include-group"], (*chain, grp))
            else:
                out.append(entry)
        flat[grp] = out
        return out

    return {grp: specs_from(expand(grp), keep_extras, keep_markers) for grp in raw_groups}


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
    say(f"  {entry} zur .gitignore hinzugefuegt (Backup kann Zugangsdaten enthalten)", C_WARN)


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
            f"Befehl lief laenger als {timeout:.0f}s und wurde abgebrochen: {' '.join(cmd)}"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(f"Befehl fehlgeschlagen: {' '.join(cmd)}")


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def restore_fields(text: str, version: str | None, readme: str | None) -> str:
    """Setzt version/readme zurueck, die 'uv init --bare' nicht uebernimmt.

    uv init setzt version IMMER auf "0.1.0" (kein --version-Flag vorhanden)
    und schreibt readme nie (nur als Datei, nicht als Feld). Ohne das hier
    wuerde eine bestehende Versionsnummer sonst still auf 0.1.0 zurueckfallen.

    Ersetzt ueber eine Lambda-Funktion statt einen Text-String: re.sub()
    interpretiert String-Replacements selbst wieder als Muster (\\1, \\g<...>);
    eine Lambda gibt ihren Rueckgabewert dagegen immer woertlich ein. Und
    .subn() statt .sub(): passt das Muster in einer kuenftigen uv-Version
    nicht mehr (anderes Anfuehrungszeichen, andere Formatierung), gibt
    re.sub() den Text sonst kommentarlos unveraendert zurueck -- die
    Versionsnummer waere still auf 0.1.0 zurueckgefallen, ohne jede Meldung.
    """
    if version:
        text, n = _VERSION_LINE_RE.subn(lambda _m: f"version = {_toml_string(version)}", text, count=1)
        if n == 0:
            say("  WARNUNG: version-Feld konnte nicht wiederhergestellt werden "
                "(unerwartetes Format in der neuen pyproject.toml)", C_WARN)
    if readme:
        text, n = re.subn(r"(?m)^(version\s*=.*)$",
                           lambda m: f"{m.group(1)}\nreadme = {_toml_string(readme)}",
                           text, count=1)
        if n == 0:
            say("  WARNUNG: readme-Feld konnte nicht wiederhergestellt werden "
                "(unerwartetes Format in der neuen pyproject.toml)", C_WARN)
    return text


def main() -> int:
    p = argparse.ArgumentParser(prog="uv-refresh", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--path", default=".", help="Projektverzeichnis (Default: aktuelles)")
    p.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts veraendern")
    p.add_argument("--yes", "-y", action="store_true", help="ohne Rueckfrage durchziehen")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="komplette neue pyproject.toml am Ende ausgeben")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="Timeout in Sekunden je uv-Aufruf (Default: 300)")
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
        raw_groups = data.get("dependency-groups") or {}
        if raw_groups:
            try:
                groups = resolve_groups(raw_groups, keep_extras, keep_markers)
            except RuntimeError as e:
                die(str(e))

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
            answer = input("\nWirklich neu aufbauen? [j/y/N] ").strip().lower()
        except EOFError:
            die("Keine Eingabe moeglich (kein Terminal). Mit --yes ohne Rueckfrage ausfuehren.")
        if answer not in {"j", "y"}:
            say("Abgebrochen.", C_DIM)
            return 1

    # ---- 2. Backup --------------------------------------------------------
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    backup = root / ".uv-refresh-backup" / stamp
    say(f"\nBackup -> {backup}", C_DIM)
    say("Aufbau erfolgt in einem Temp-Verzeichnis; deine echte pyproject.toml/"
        "uv.lock bleiben bis zum letzten Schritt unangetastet.", C_DIM)

    build_ctx = (tempfile.TemporaryDirectory(dir=root, prefix=".uv-refresh-tmp-")
                 if not args.dry_run else contextlib.nullcontext(root))

    try:
        if not args.dry_run:
            ensure_backup_ignored(root)
            backup.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(backup, 0o700)
            except OSError:
                pass
            shutil.copy2(pyproject, backup / "pyproject.toml")
            if lock.is_file():
                shutil.copy2(lock, backup / "uv.lock")

        with build_ctx as build_dir_raw:
            build_dir = Path(build_dir_raw)
            if not args.dry_run and args.keep_lock and lock.is_file():
                shutil.copy2(lock, build_dir / "uv.lock")

            # ---- 3. uv init -----------------------------------------------
            init = build_init_cmd(name, requires_python, description)
            try:
                run(init, build_dir, args.dry_run, args.timeout)
            except RuntimeError:
                if not any(f.startswith("--python=") for f in init):
                    raise
                say("  uv init mit --python fehlgeschlagen, versuche es ohne", C_WARN)
                run([f for f in init if not f.startswith("--python=")],
                    build_dir, args.dry_run, args.timeout)

            # ---- 4. uv add --------------------------------------------------
            flags: list[str] = []
            if args.raw:
                flags.append("--raw")
            if args.bounds:
                flags += ["--bounds", args.bounds]

            if main_deps:
                run(["uv", "add", *flags, *main_deps], build_dir, args.dry_run, args.timeout)
            for grp, deps in extras.items():
                if deps:
                    run(["uv", "add", "--optional", grp, *flags, *deps],
                        build_dir, args.dry_run, args.timeout)
            for grp, deps in groups.items():
                if deps:
                    run(["uv", "add", "--group", grp, *flags, *deps],
                        build_dir, args.dry_run, args.timeout)

            # ---- 5. version/readme zuruecksetzen -----------------------------
            if not args.dry_run and (version or isinstance(readme, str)):
                text = restore_fields((build_dir / "pyproject.toml").read_text(encoding="utf-8"),
                                       version, readme if isinstance(readme, str) else None)
                (build_dir / "pyproject.toml").write_text(text, encoding="utf-8")

            # ---- 6. atomarer Tausch -------------------------------------------
            # root blieb bis hierher unveraendert: schlaegt oben etwas fehl
            # (auch per Ctrl+C), gibt es nichts zurueckzuholen.
            if not args.dry_run:
                os.replace(build_dir / "pyproject.toml", pyproject)
                new_lock = build_dir / "uv.lock"
                if new_lock.is_file():
                    os.replace(new_lock, lock)
            else:
                say("  $ (pyproject.toml/uv.lock wuerden jetzt atomar ersetzt)", C_DIM)

    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 -- Notbremse: bei
        # JEDEM Fehler (uv, Dateisystem, Interrupt, ...) klar melden. Der Aufbau
        # geschah in einem Temp-Verzeichnis, root ist daher normalerweise
        # unveraendert; das Backup bleibt als zusaetzliches Netz trotzdem liegen.
        say(f"\n{e}", C_ERR)
        if not args.dry_run:
            say(f"pyproject.toml unveraendert. Backup liegt in {backup}.", C_WARN)
        return 1

    if args.dry_run:
        say("\n--dry-run: nichts wurde veraendert.", C_OK)
        return 0

    say("\nFertig.", C_OK)
    if args.verbose and pyproject.is_file():
        say(pyproject.read_text(encoding="utf-8"), C_DIM)
    return 0


if __name__ == "__main__":
    sys.exit(main())
