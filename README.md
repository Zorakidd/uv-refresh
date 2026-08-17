# uv-refresh

Baut die `pyproject.toml` eines uv-Projekts neu auf, damit alle Dependencies
frisch aufgeloest werden statt alte Versionsbindungen mitzuschleppen.

## Ablauf

1. `pyproject.toml` lesen, Dependencies ohne Versionsangabe einsammeln
   (Extras und Environment-Marker bleiben erhalten)
2. `pyproject.toml` und `uv.lock` nach `.uv-refresh-backup/<zeitstempel>/` sichern
3. beide loeschen
4. `uv init --bare` fuer eine neue, minimale `pyproject.toml`
5. `uv add <pakete>`, uv loest die neueste kompatible Version auf

Bricht ein Schritt ab, wird der alte Stand automatisch aus dem Backup
wiederhergestellt.

## Installation

    uv tool install git+https://github.com/Zorakidd/uv-refresh

Oder ohne Installation, direkt aus dem Repo:

    uvx --from git+https://github.com/Zorakidd/uv-refresh uv-refresh --dry-run

## Benutzung

    uv-refresh --dry-run     # nur anzeigen, nichts veraendern
    uv-refresh               # mit Rueckfrage durchziehen
    uv-refresh -y            # ohne Rueckfrage

## Optionen

| Flag | Wirkung |
| --- | --- |
| `--path PFAD` | anderes Projektverzeichnis (Default: aktuelles) |
| `--dry-run` | zeigt nur, was passieren wuerde |
| `-y`, `--yes` | keine Rueckfrage |
| `--raw` | Pakete ganz ohne Versionsgrenze eintragen |
| `--bounds {lower,major,minor,exact}` | Art der Versionsgrenze, die `uv add` setzt |
| `--keep-lock` | `uv.lock` behalten (dann bevorzugt uv die alten Versionen) |
| `--no-groups` | optional-dependencies und dependency-groups ignorieren |
| `--drop-extras` | `fastapi[standard]` zu `fastapi` eindampfen |
| `--drop-markers` | Environment-Marker verwerfen |

## Achtung

Alles ausser `[project]` und `[dependency-groups]` geht verloren, also
`[build-system]`, `[tool.ruff]`, `[project.scripts]` und so weiter. Das Tool
warnt vorher und listet die betroffenen Bloecke auf. Zurueckkopieren musst du
sie von Hand aus dem Backup.
