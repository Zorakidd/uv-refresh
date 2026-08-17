# uv-refresh

Baut die `pyproject.toml` eines uv-Projekts neu auf, damit alle Dependencies
frisch aufgelöst werden statt alte Versionsbindungen mitzuschleppen.

## Ablauf

1. `pyproject.toml` lesen, Dependencies ohne Versionsangabe einsammeln
   (Extras und Environment-Marker bleiben erhalten; `include-group`-Einträge
   aus `dependency-groups` werden dabei aufgelöst, nicht verworfen)
2. `pyproject.toml` und `uv.lock` nach `.uv-refresh-backup/<zeitstempel>/` sichern
3. `uv init --bare` + `uv add <pakete>` in einem Temp-Verzeichnis neben dem
   Projekt ausführen -- die echte `pyproject.toml` bleibt dabei unangetastet
4. Nur `dependencies`/`optional-dependencies`/`dependency-groups` aus dem
   Ergebnis in eine Kopie der ORIGINALEN `pyproject.toml` einmergen -- alles
   andere bleibt unangetastet
5. Ergebnis atomar an die Stelle der alten `pyproject.toml`/`uv.lock` setzen

Schlägt ein Schritt fehl -- auch per Ctrl+C --, wurde die echte
`pyproject.toml` nie verändert, weil der komplette Aufbau im
Temp-Verzeichnis geschah. Das Backup liegt zusätzlich als Referenz bereit.

## Installation

    uv tool install uv-refresh

Macht `uv-refresh` dauerhaft als Befehl verfügbar (global im PATH).

Für einen einmaligen Testlauf, ganz ohne etwas zu installieren:

    uvx uv-refresh --dry-run

Oder direkt aus dem Repo, z. B. um einen unveröffentlichten Stand zu testen:

    uv tool install git+https://github.com/Zorakidd/uv-refresh

## Benutzung

    uv-refresh --dry-run     # nur anzeigen, nichts verändern
    uv-refresh               # mit Rückfrage durchziehen
    uv-refresh -y            # ohne Rückfrage

## Optionen

| Flag | Wirkung |
| --- | --- |
| `--path PFAD` | anderes Projektverzeichnis (Default: aktuelles) |
| `--dry-run` | zeigt nur, was passieren würde |
| `-y`, `--yes` | keine Rückfrage |
| `-v`, `--verbose` | die komplette neue `pyproject.toml` am Ende ausgeben |
| `--timeout SEK` | Timeout je `uv`-Aufruf, Default 300s |
| `--raw` | Pakete ganz ohne Versionsgrenze eintragen |
| `--bounds {lower,major,minor,exact}` | Art der Versionsgrenze, die `uv add` setzt |
| `--keep-lock` | `uv.lock` behalten (dann bevorzugt uv die alten Versionen) |
| `--no-groups` | optional-dependencies und dependency-groups ignorieren |
| `--drop-extras` | `fastapi[standard]` zu `fastapi` eindampfen |
| `--drop-markers` | Environment-Marker verwerfen |
| `--version` | Version von uv-refresh anzeigen |

## Achtung

Nur `dependencies`, `optional-dependencies` und `dependency-groups` werden
neu geschrieben. Alles andere -- `description`, `readme`, `license`,
`authors`, `keywords`, `[project.urls]`, `[project.scripts]`,
`[build-system]`, `[tool.*]` und so weiter -- bleibt unverändert, weil es nie
gelöscht wird: das Tool baut nur temporär eine minimale `pyproject.toml`
zum Auflösen der Versionen, übernimmt daraus aber nur die frisch
aufgelösten Dependency-Listen und schreibt die in eine Kopie der
ursprünglichen Datei zurück.

Einzige Ausnahme: mit `--no-groups` werden vorhandene
`optional-dependencies`/`dependency-groups` absichtlich entfernt (das Tool
warnt vorher). Einzelne Einträge, die sich nicht als PEP-508-String
interpretieren lassen, werden pro Eintrag übersprungen und gemeldet.
