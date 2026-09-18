import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from uv_refresh import cli


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("requests>=2.0", "requests"),
        ('fastapi[standard]>=0.110; python_version<"3.13"',
         'fastapi[standard]; python_version < "3.13"'),
        ("pkg @ git+https://example.com/repo.git", "pkg @ git+https://example.com/repo.git"),
        ("   ", None),
    ],
)
def test_strip_version(spec, expected):
    assert cli.strip_version(spec) == expected


def test_strip_version_drop_extras_and_markers():
    spec = 'fastapi[standard]>=0.110; python_version<"3.13"'
    assert cli.strip_version(spec, keep_extras=False, keep_markers=True) == \
        'fastapi; python_version < "3.13"'
    assert cli.strip_version(spec, keep_extras=True, keep_markers=False) == "fastapi[standard]"


def test_specs_from_dedupes_case_insensitively():
    assert cli.specs_from(["Requests>=2.0", "requests==1.0"], True, True) == ["Requests"]


def test_specs_from_skips_non_string_entries(capsys):
    result = cli.specs_from([{"include-group": "x"}], True, True)
    assert result == []
    assert "skipped" in capsys.readouterr().err


def test_resolve_groups_expands_include_group():
    raw = {
        "test": ["pytest>=8"],
        "dev": [{"include-group": "test"}, "ruff>=0.6"],
    }
    result = cli.resolve_groups(raw, keep_extras=True, keep_markers=True)
    assert result["dev"] == ["pytest", "ruff"]
    assert result["test"] == ["pytest"]


def test_resolve_groups_detects_cycle():
    raw = {"a": [{"include-group": "b"}], "b": [{"include-group": "a"}]}
    with pytest.raises(RuntimeError, match="cycle"):
        cli.resolve_groups(raw, True, True)


def test_resolve_groups_missing_group():
    raw = {"dev": [{"include-group": "missing"}]}
    with pytest.raises(RuntimeError, match="does not exist"):
        cli.resolve_groups(raw, True, True)


def test_build_init_cmd_binds_flags_with_equals():
    cmd = cli.build_init_cmd("demo", ">=3.11", "a description")
    assert cmd == [
        "uv", "init", "--bare", "--no-workspace",
        "--name=demo", "--python=>=3.11", "--description=a description",
    ]


def test_build_init_cmd_binds_dash_prefixed_description():
    # regression test: a description starting with '-' must not be parseable
    # as a separate flag by uv's clap-based CLI (reproduced against real uv).
    cmd = cli.build_init_cmd(None, None, "--looks-like-a-flag")
    assert cmd == ["uv", "init", "--bare", "--no-workspace", "--description=--looks-like-a-flag"]


def test_build_init_cmd_omits_missing_fields():
    assert cli.build_init_cmd(None, None, None) == ["uv", "init", "--bare", "--no-workspace"]


_ORIGINAL_PYPROJECT = """\
[project]
name = "demo"
version = "1.2.3"
description = "a demo"
readme = "README.md"
license = "MIT"
authors = [{ name = "Zora" }]
keywords = ["a", "b"]
requires-python = ">=3.11"
dependencies = ["requests>=2.0"]

[project.urls]
Homepage = "https://example.com"

[project.scripts]
demo = "demo:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
target-version = "py311"
"""


def test_merge_dependencies_replaces_only_dependencies():
    merged = cli.merge_dependencies(_ORIGINAL_PYPROJECT, ["click>=8.1.0"], {}, {})
    result = tomllib.loads(merged)
    assert result["project"]["dependencies"] == ["click>=8.1.0"]


def test_merge_dependencies_preserves_everything_else():
    # regression test: uv-refresh used to rebuild pyproject.toml from
    # scratch via 'uv init --bare', which only carries name/version/
    # description/requires-python -- silently dropping readme, license,
    # authors, keywords, [project.urls], [project.scripts], [build-system]
    # and [tool.*] (discovered by running the tool on its own repo).
    merged = cli.merge_dependencies(_ORIGINAL_PYPROJECT, ["click>=8.1.0"], {}, {})
    result = tomllib.loads(merged)
    project = result["project"]
    assert project["version"] == "1.2.3"
    assert project["description"] == "a demo"
    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["authors"] == [{"name": "Zora"}]
    assert project["keywords"] == ["a", "b"]
    assert project["requires-python"] == ">=3.11"
    assert project["urls"]["Homepage"] == "https://example.com"
    assert project["scripts"]["demo"] == "demo:main"
    assert result["build-system"]["build-backend"] == "hatchling.build"
    assert result["tool"]["ruff"]["target-version"] == "py311"


def test_merge_dependencies_bumps_requires_python_when_given():
    merged = cli.merge_dependencies(_ORIGINAL_PYPROJECT, ["click>=8.1.0"], {}, {}, ">=3.14")
    result = tomllib.loads(merged)
    assert result["project"]["requires-python"] == ">=3.14"


def test_merge_dependencies_adds_groups():
    original = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = []\n'
    merged = cli.merge_dependencies(
        original, ["click"], {"speed": ["orjson"]}, {"dev": ["pytest", "ruff"]}
    )
    result = tomllib.loads(merged)
    assert result["project"]["optional-dependencies"] == {"speed": ["orjson"]}
    assert result["dependency-groups"] == {"dev": ["pytest", "ruff"]}


def test_merge_dependencies_removes_groups_that_are_gone():
    # e.g. what --no-groups produces: groups existed before, nothing to put
    # back this time around.
    with_groups = cli.merge_dependencies(
        _ORIGINAL_PYPROJECT, ["click"], {"speed": ["orjson"]}, {"dev": ["pytest"]}
    )
    without_groups = cli.merge_dependencies(with_groups, ["click"], {}, {})
    result = tomllib.loads(without_groups)
    assert "optional-dependencies" not in result["project"]
    assert "dependency-groups" not in result


def test_run_raises_on_timeout(tmp_path):
    with pytest.raises(RuntimeError, match="ran longer than"):
        cli.run([sys.executable, "-c", "import time; time.sleep(2)"], tmp_path,
                dry=False, timeout=0.1)


def test_run_dry_run_never_executes(tmp_path):
    # a nonexistent command would raise if actually executed
    cli.run(["definitely-not-a-real-command-xyz"], tmp_path, dry=True)


def test_ensure_backup_ignored_appends_entry(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    cli.ensure_backup_ignored(tmp_path)
    assert ".uv-refresh-backup/" in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_ensure_backup_ignored_is_idempotent(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(".uv-refresh-backup/\n", encoding="utf-8")
    cli.ensure_backup_ignored(tmp_path)
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.count(".uv-refresh-backup/") == 1


def test_ensure_backup_ignored_skips_non_git_dirs(tmp_path):
    cli.ensure_backup_ignored(tmp_path)
    assert not (tmp_path / ".gitignore").exists()


def test_ensure_backup_ignored_also_ignores_the_temp_build_dir(tmp_path):
    # a temp build dir left behind by a hard kill holds the same data as the
    # backup -- it must not end up in 'git add .' either.
    (tmp_path / ".git").mkdir()
    cli.ensure_backup_ignored(tmp_path)
    cli.ensure_backup_ignored(tmp_path)  # a second run adds nothing

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines == [".uv-refresh-backup/", ".uv-refresh-tmp-*/"]


def test_main_dry_run_leaves_project_untouched(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    original = (
        '[project]\nname = "demo"\nversion = "1.0.0"\n'
        'dependencies = ["requests>=2.0"]\n'
    )
    pyproject.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--dry-run"])

    assert cli.main() == 0
    assert pyproject.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".uv-refresh-backup").exists()
    assert not any(tmp_path.glob(".uv-refresh-tmp-*"))


def _stub_run_writing(resolved_pyproject_text, calls=None):
    """Fakes cli.run(): 'uv init' seeds a minimal pyproject.toml in the build
    dir, 'uv add' overwrites it with the given already-resolved text -- close
    enough to real uv output for build_and_swap()'s merge step to work on.
    Every command is also appended to 'calls', if given."""

    def fake_run(cmd, cwd, dry, timeout=None):
        if calls is not None:
            calls.append(cmd)
        if cmd[:2] == ["uv", "init"]:
            (cwd / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8")
        elif cmd[:2] == ["uv", "add"]:
            (cwd / "pyproject.toml").write_text(resolved_pyproject_text, encoding="utf-8")

    return fake_run


def test_main_success_swaps_pyproject_and_lock(tmp_path, monkeypatch):
    # regression test: the module docstring promises pyproject.toml is only
    # ever touched by the final atomic swap -- this is the one path
    # (main() outside --dry-run) that actually exercises that swap, and until
    # now nothing did.
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
    pyproject.write_text(original, encoding="utf-8")

    monkeypatch.setattr(cli, "run", _stub_run_writing(
        '[project]\nname = "demo"\nversion = "0.0.0"\n'
        'dependencies = ["requests==2.31.0"]\n'
    ))
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])

    assert cli.main() == 0

    result = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert result["project"]["dependencies"] == ["requests==2.31.0"]
    assert result["project"]["version"] == "1.0.0"  # untouched fields survive the merge

    backups = list((tmp_path / ".uv-refresh-backup").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "pyproject.toml").read_text(encoding="utf-8") == original
    assert not any(tmp_path.glob(".uv-refresh-tmp-*"))  # temp build dir cleaned up


def test_main_copies_python_version_and_sources_into_temp_build(tmp_path, monkeypatch):
    # regression test for the AudioSeparator bug report: without this, 'uv
    # add' in the temp dir picks the newest installed Python (ignoring the
    # real project's .python-version) and resolves pinned-index packages
    # (e.g. torch's CUDA wheels) against plain PyPI instead.
    original = (
        '[project]\nname = "demo"\nversion = "1.0.0"\n'
        'dependencies = ["torch==2.5.1"]\n\n'
        '[tool.uv.sources]\ntorch = [{ index = "pytorch-cu121" }]\n\n'
        '[[tool.uv.index]]\nname = "pytorch-cu121"\n'
        'url = "https://download.pytorch.org/whl/cu121"\nexplicit = true\n'
    )
    (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")
    (tmp_path / ".python-version").write_text("3.11\n", encoding="utf-8")

    seen_at_add = {}

    def fake_run(cmd, cwd, dry, timeout=None):
        if cmd[:2] == ["uv", "init"]:
            (cwd / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8")
        elif cmd[:2] == ["uv", "add"]:
            seen_at_add["python_version"] = (cwd / ".python-version").read_text(encoding="utf-8")
            seen_at_add["pyproject"] = tomllib.loads(
                (cwd / "pyproject.toml").read_text(encoding="utf-8"))
            (cwd / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.0.0"\n'
                'dependencies = ["torch==2.5.1"]\n', encoding="utf-8")

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])

    assert cli.main() == 0

    assert seen_at_add["python_version"] == "3.11\n"
    assert seen_at_add["pyproject"]["tool"]["uv"]["sources"]["torch"] == [{"index": "pytorch-cu121"}]
    assert seen_at_add["pyproject"]["tool"]["uv"]["index"][0]["name"] == "pytorch-cu121"


def test_build_adds_without_syncing_and_with_the_projects_uv_config(tmp_path, monkeypatch):
    # regression tests: 'uv add' used to sync a throwaway venv (every dep
    # installed, then deleted), and resolved without the project's [tool.uv]
    # -- constraints/indexes/sources were ignored (both reproduced against
    # real uv; see test_integration.py for the real-uv side).
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n\n'
        '[dependency-groups]\ndev = ["pytest>=8"]\n\n'
        '[tool.uv]\nconstraint-dependencies = ["requests<3"]\n\n'
        '[tool.ruff]\nline-length = 100\n',
        encoding="utf-8",
    )
    seen = []  # (uv add command, the build pyproject.toml as that command found it)

    def fake_run(cmd, cwd, dry, timeout=None):
        if cmd[:2] == ["uv", "init"]:
            (cwd / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = []\n', encoding="utf-8")
        elif cmd[:2] == ["uv", "add"]:
            seen.append((cmd, tomllib.loads((cwd / "pyproject.toml").read_text(encoding="utf-8"))))

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])

    assert cli.main() == 0
    assert len(seen) == 2  # main deps + the dev group
    assert all("--no-sync" in cmd for cmd, _ in seen)
    build = seen[0][1]
    assert build["tool"] == {"uv": {"constraint-dependencies": ["requests<3"]}}  # [tool.uv] only
    assert build["dependency-groups"] == {"dev": []}  # exists before its own 'uv add'


def _run_main_full(tmp_path, monkeypatch, requires_python, latest):
    """main() --full --yes on a project with the given requires-python, with
    'latest' as the newest installed Python; returns (exit code, uv commands
    run, resulting requires-python)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f'[project]\nname = "demo"\nversion = "1.0.0"\nrequires-python = "{requires_python}"\n'
        'dependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(cli, "run", _stub_run_writing(
        '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["requests==2.31.0"]\n', calls
    ))
    monkeypatch.setattr(cli, "latest_installed_python", lambda: latest)
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--full"])

    code = cli.main()
    result = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return code, calls, result["project"]["requires-python"]


def test_main_full_bumps_requires_python_and_pins_python(tmp_path, monkeypatch):
    # end-to-end through main(): --full should (a) tell 'uv init' to target
    # the newest installed Python, (b) end up with that same floor written to
    # requires-python in the real pyproject.toml, and (c) pin .python-version
    # to it once the swap has landed.
    code, calls, requires_python = _run_main_full(tmp_path, monkeypatch, ">=3.9", "3.14.0")

    assert code == 0
    assert "--python=>=3.14" in next(c for c in calls if c[:2] == ["uv", "init"])
    assert ["uv", "python", "pin", "3.14.0"] in calls
    assert requires_python == ">=3.14"
    result = tomllib.loads((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))
    assert result["project"]["dependencies"] == ["requests==2.31.0"]


def test_main_full_never_lowers_requires_python(tmp_path, monkeypatch, capsys):
    # regression test: --full used to write the newest installed Python's
    # floor unconditionally -- on a machine that only has 3.14, a '>=3.15'
    # project came out as '>=3.14' (reproduced against real uv), reported as
    # a "bump". Now requires-python and .python-version are both left alone,
    # and the dependency refresh still happens.
    code, calls, requires_python = _run_main_full(tmp_path, monkeypatch, ">=3.15", "3.14.7")

    assert code == 0
    assert requires_python == ">=3.15"
    assert "--python=>=3.15" in next(c for c in calls if c[:2] == ["uv", "init"])
    assert not any(c[:3] == ["uv", "python", "pin"] for c in calls)
    assert "doesn't satisfy requires-python >=3.15" in capsys.readouterr().err


def test_main_full_keeps_requires_python_already_at_that_minor(tmp_path, monkeypatch):
    # same minor version (or a stricter patch floor): nothing to bump, but
    # .python-version still gets re-pinned -- that part is still wanted.
    code, calls, requires_python = _run_main_full(tmp_path, monkeypatch, ">=3.14.2", "3.14.7")

    assert code == 0
    assert requires_python == ">=3.14.2"
    assert ["uv", "python", "pin", "3.14.7"] in calls


def test_main_full_exact_pin_skips_the_pin_instead_of_failing(tmp_path, monkeypatch, capsys):
    # regression test: '==3.13' is kept (a '>=3.13' rewrite would loosen
    # it), but 3.13.5 doesn't satisfy it -- 'uv python pin' would refuse
    # AFTER a successful rebuild and turn it into exit 1. Skip it up front
    # (found by checking the new helpers against packaging's specifier logic).
    code, calls, requires_python = _run_main_full(tmp_path, monkeypatch, "==3.13", "3.13.5")

    assert code == 0
    assert requires_python == "==3.13"
    assert not any(c[:3] == ["uv", "python", "pin"] for c in calls)
    assert "doesn't satisfy requires-python ==3.13" in capsys.readouterr().err


def test_main_full_bump_ignores_the_old_upper_bound(tmp_path, monkeypatch):
    # the bump replaces the whole specifier, so an old '<3.13' cap that
    # excludes the newest Python must not block it (plan_full() only checks
    # 'latest in requires-python' when requires-python is KEPT).
    code, calls, requires_python = _run_main_full(tmp_path, monkeypatch, ">=3.10,<3.13", "3.14.7")

    assert code == 0
    assert requires_python == ">=3.14"
    assert ["uv", "python", "pin", "3.14.7"] in calls


def test_main_full_resolves_the_temp_build_against_the_new_pin(tmp_path, monkeypatch):
    # the old .python-version is about to be replaced by --full's re-pin, so
    # the temp 'uv add' must already run against the new version.
    (tmp_path / ".python-version").write_text("3.11\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    stub = _stub_run_writing(
        '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["requests==2.31.0"]\n'
    )
    pins_at_add = []

    def fake_run(cmd, cwd, dry, timeout=None):
        if cmd[:2] == ["uv", "add"]:
            pins_at_add.append((cwd / ".python-version").read_text(encoding="utf-8"))
        stub(cmd, cwd, dry, timeout)

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "latest_installed_python", lambda: "3.14.7")
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--full"])

    assert cli.main() == 0
    assert pins_at_add == ["3.14.7\n"]


@pytest.mark.parametrize("flags", [["--full"], []])
def test_main_rejects_non_string_requires_python_up_front(tmp_path, monkeypatch, capsys, flags):
    # regression test: 'requires-python = 3.11' (a TOML float) crashed --full
    # with an AttributeError traceback; without --full, uv only rejected it
    # later, after a backup had already been made (both reproduced against
    # real uv).
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\nrequires-python = 3.11\n'
        'dependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(cli, "latest_installed_python", lambda: "3.14.7")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", *flags])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "requires-python must be a string" in capsys.readouterr().err
    assert not (tmp_path / ".uv-refresh-backup").exists()


_HERE = Path(__file__).resolve().parent.as_posix()  # an absolute path on every OS
_D = '[project]\nname = "d"\n'
_SOURCES = _D + "[tool.uv.sources]\n"


@pytest.mark.parametrize(
    ("pyproject", "reason"),
    [
        (_D + 'dependencies = ["x"]\n', None),
        (_D + '[tool.uv.workspace]\nmembers = ["libs/*"]\n', "uv workspace"),
        (_SOURCES + "x = { workspace = true }\n", "workspace source"),
        (_SOURCES + 'x = { path = "libs/x" }\n', "relative path source"),
        (_SOURCES + "x = [{ path = '../x', marker = 'sys_platform == \"linux\"' }]\n", "relative path"),
        (_SOURCES + f'x = {{ path = "{_HERE}" }}\n', None),  # absolute paths work from the temp dir
        (_SOURCES + 'x = { git = "https://example.com/x.git" }\n', None),
        (_D + 'dependencies = ["x @ file:///${PROJECT_ROOT}/libs/x"]\n', "${PROJECT_ROOT}"),
        (_D + '[dependency-groups]\ndev = ["x @ file:///${PROJECT_ROOT}/x"]\n', "${PROJECT_ROOT}"),
        (_D + 'dynamic = ["version"]\n', "it lists version as dynamic"),
        (_D + 'dynamic = ["version", "dependencies"]\n', "it lists dependencies, version as dynamic"),
        (_D + 'dynamic = ["readme"]\n', None),  # uv needs no build for that
    ],
)
def test_unsupported_reason(pyproject, reason):
    result = cli.unsupported_reason(tomllib.loads(pyproject))
    if reason is None:
        assert result is None
    else:
        assert result is not None and reason in result


def test_main_refuses_what_the_temp_dir_cant_rebuild_before_any_backup(tmp_path, monkeypatch, capsys):
    # regression test: a dynamic version (setuptools-scm, hatch-vcs, ...) made
    # the final 'uv lock' build the project in the temp dir, which lacks its
    # files -- a ModuleNotFoundError from setuptools, after the backup
    # (reproduced against real uv; so did relative paths/workspaces).
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\ndynamic = ["version"]\ndependencies = ["requests>=2.0"]\n'
    pyproject.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "Can't refresh this project: it lists version as dynamic" in capsys.readouterr().err
    assert pyproject.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".uv-refresh-backup").exists()


def test_main_failure_leaves_pyproject_untouched(tmp_path, monkeypatch):
    # regression test: the flip side of the guarantee above -- a failure
    # partway through (here: 'uv add' itself) must never reach the real file.
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
    pyproject.write_text(original, encoding="utf-8")

    def failing_run(cmd, cwd, dry, timeout=None):
        if cmd[:2] == ["uv", "init"]:
            (cwd / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8")
        else:
            raise RuntimeError("Command failed: uv add (simulated network error)")

    monkeypatch.setattr(cli, "run", failing_run)
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])

    assert cli.main() == 1
    assert pyproject.read_text(encoding="utf-8") == original
    assert list((tmp_path / ".uv-refresh-backup").iterdir())  # backup kept as the recovery net
    assert not any(tmp_path.glob(".uv-refresh-tmp-*"))


def test_main_confirmation_accepts_yes(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path)])
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    started = []
    monkeypatch.setattr(cli, "build_and_swap", lambda *a, **k: started.append(True))

    assert cli.main() == 0
    assert started == [True]


def test_main_confirmation_rejects_anything_else(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path)])
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    started = []
    monkeypatch.setattr(cli, "build_and_swap", lambda *a, **k: started.append(True))

    assert cli.main() == 1
    assert started == []


@pytest.mark.parametrize(("latest", "mentions_pin"), [("3.14.0", True), (None, False)])
def test_main_full_prompt_mentions_pin_only_when_pinning(tmp_path, monkeypatch, latest, mentions_pin):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--full"])
    monkeypatch.setattr(cli, "latest_installed_python", lambda: latest)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")

    assert cli.main() == 1
    assert ("re-pin Python" in prompts[0]) is mentions_pin


def test_prune_backups_keeps_newest_n(tmp_path):
    base = tmp_path / ".uv-refresh-backup"
    stamps = ["20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000"]
    for s in stamps:
        (base / s).mkdir(parents=True)

    cli.prune_backups(tmp_path, keep=2)

    assert sorted(p.name for p in base.iterdir()) == stamps[-2:]


def test_prune_backups_keep_zero_keeps_everything(tmp_path):
    base = tmp_path / ".uv-refresh-backup"
    for s in ["20260101-000000", "20260102-000000"]:
        (base / s).mkdir(parents=True)

    cli.prune_backups(tmp_path, keep=0)

    assert len(list(base.iterdir())) == 2


def test_prune_backups_missing_dir_is_a_no_op(tmp_path):
    cli.prune_backups(tmp_path, keep=5)  # must not raise


def test_prune_backups_warns_instead_of_swallowing_failure(tmp_path, monkeypatch, capsys):
    # regression test: an unremovable backup (locked file, permissions, ...)
    # must be reported, not silently dropped -- and must not stop the other
    # old backups from still being pruned.
    base = tmp_path / ".uv-refresh-backup"
    stamps = ["20260101-000000", "20260102-000000", "20260103-000000"]
    for s in stamps:
        (base / s).mkdir(parents=True)

    real_rmtree = shutil.rmtree

    def flaky_rmtree(path):
        if path.name == "20260101-000000":
            raise OSError("simulated: file in use")
        real_rmtree(path)

    monkeypatch.setattr(cli.shutil, "rmtree", flaky_rmtree)

    cli.prune_backups(tmp_path, keep=1)

    remaining = sorted(p.name for p in base.iterdir())
    assert remaining == ["20260101-000000", "20260103-000000"]  # unremovable one survives
    assert "could not remove old backup" in capsys.readouterr().err


def test_main_success_prunes_old_backups(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    backup_base = tmp_path / ".uv-refresh-backup"
    for s in ["20200101-000000", "20200102-000000", "20200103-000000"]:
        (backup_base / s).mkdir(parents=True)

    monkeypatch.setattr(cli, "run", _stub_run_writing(
        '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["requests==2.31.0"]\n'
    ))
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv",
                         ["uv-refresh", "--path", str(tmp_path), "--yes", "--keep-backups", "2"])

    assert cli.main() == 0
    assert len(list(backup_base.iterdir())) == 2


def test_restrict_to_owner_uses_chmod_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    calls = []
    monkeypatch.setattr(cli.os, "chmod", lambda path, mode: calls.append((path, mode)))

    cli.restrict_to_owner(tmp_path)

    assert calls == [(tmp_path, 0o700)]


def test_restrict_to_owner_uses_icacls_on_windows(tmp_path, monkeypatch, capsys):
    # regression test: os.chmod cannot express owner-only access on Windows
    # (it only toggles the read-only attribute) -- the backup may contain
    # credentials from direct-reference dependencies, so this must not
    # silently no-op there the way the original os.chmod(0o700) call did.
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.restrict_to_owner(tmp_path)

    assert calls and calls[0][0] == "icacls"
    assert "could not restrict" not in capsys.readouterr().err


def test_restrict_to_owner_warns_when_icacls_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.subprocess, "run",
                         lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1))

    cli.restrict_to_owner(tmp_path)

    assert "could not restrict backup permissions" in capsys.readouterr().err


def _hanging_run(cmd, **kwargs):
    raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])  # KeyError if no timeout was set


def test_restrict_to_owner_warns_when_icacls_hangs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.subprocess, "run", _hanging_run)

    cli.restrict_to_owner(tmp_path)

    assert "could not restrict backup permissions" in capsys.readouterr().err


def test_latest_installed_python_picks_first_entry(monkeypatch):
    payload = '[{"version": "3.13.5"}, {"version": "3.11.14"}]'
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=payload),
    )
    assert cli.latest_installed_python() == "3.13.5"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('[{"version": "3.15.0rc2"}, {"version": "3.14.7"}]', "3.14.7"),
        ('[{"version": "3.15.0a1"}]', None),
    ],
)
def test_latest_installed_python_skips_prereleases(monkeypatch, payload, expected):
    # regression test: the result becomes a requires-python floor under
    # --full -- an installed rc must not turn into '>=3.15' for a published
    # package (uv offers cpython-3.15.0rc2 right now, and its JSON gives the
    # version exactly like that: '3.15.0rc2').
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=payload),
    )
    assert cli.latest_installed_python() == expected


def test_latest_installed_python_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout=""),
    )
    assert cli.latest_installed_python() is None


def test_latest_installed_python_returns_none_on_timeout(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", _hanging_run)
    assert cli.latest_installed_python() is None


def test_latest_installed_python_returns_none_when_nothing_installed(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="[]"),
    )
    assert cli.latest_installed_python() is None


def test_requires_python_floor_truncates_to_major_minor():
    assert cli.requires_python_floor("3.13.5") == ">=3.13"


@pytest.mark.parametrize(
    ("requires_python", "version", "expected"),
    [
        (">=3.11", "3.14.7", ">=3.14"),
        ("", "3.14.7", ">=3.14"),
        ("<3.13", "3.14.7", ">=3.14"),         # no floor at all
        (">=3.10,<3.13", "3.14.7", ">=3.14"),
        (">=3.14", "3.14.7", None),            # already there -- no churn
        (">=3.14.0", "3.14.7", None),          # same (3.14.0 == 3.14)
        (">=3.14.2", "3.14.7", None),          # '>=3.14' would loosen it
        (">3.14", "3.14.7", None),             # same
        ("~=3.14", "3.14.7", None),            # '~=' and '==X.*' floor too
        ("==3.14.*", "3.14.7", None),
        (">=3.12,>=3.14.2", "3.14.7", None),   # the highest floor counts
        (">=3.15", "3.14.7", None),            # would LOWER it
        ("===3.15.0", "3.14.7", None),         # same, via arbitrary equality
    ],
)
def test_bumped_requires_python_only_ever_raises(requires_python, version, expected):
    assert cli.bumped_requires_python(SpecifierSet(requires_python), version) == expected


@pytest.mark.parametrize(
    ("requires_python", "latest", "expected"),
    [
        (">=3.11", "3.14.7", (">=3.14", "3.14.7")),     # bump + pin
        (None, "3.14.7", (">=3.14", "3.14.7")),
        (">=3.10,<3.13", "3.14.7", (">=3.14", "3.14.7")),  # old cap doesn't block a bump
        (">=3.14", "3.14.7", (None, "3.14.7")),          # keep + pin
        ("==3.14.*", "3.14.7", (None, "3.14.7")),
        (">=3.15", "3.14.7", (None, None)),              # older than the floor
        ("==3.13", "3.13.5", (None, None)),              # kept, but excludes 3.13.5
        (">=3.11.*", "3.14.7", (None, None)),            # can't parse -> don't guess
        (">=3.11", None, (None, None)),                  # nothing installed
    ],
)
def test_plan_full(monkeypatch, requires_python, latest, expected):
    monkeypatch.setattr(cli, "latest_installed_python", lambda: latest)
    assert cli.plan_full(requires_python) == expected


def test_plan_full_without_packaging_skips_instead_of_guessing(monkeypatch, capsys):
    # packaging is a declared dependency, but the module still imports
    # without it (e.g. 'pip install --no-deps', tried for real) -- --full
    # must then leave requires-python alone rather than parse it by hand.
    monkeypatch.setattr(cli, "SpecifierSet", None)
    monkeypatch.setattr(cli, "latest_installed_python", lambda: "3.14.7")

    assert cli.plan_full(">=3.11") == (None, None)
    assert "needs the 'packaging' module" in capsys.readouterr().err


def test_refresh_python_version_pins_the_given_version(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run", lambda cmd, cwd, dry, timeout=None: calls.append((cmd, cwd, dry)))

    cli.refresh_python_version(tmp_path, dry=False, version="3.13.5")

    assert calls == [(["uv", "python", "pin", "3.13.5"], tmp_path, False)]


def test_pin_build_interpreter_prefers_latest_python_over_existing_pin(tmp_path):
    # --full is deliberately moving the project past the old pin, so the temp
    # build should resolve against the NEW target version, not the old one.
    build_dir, root = tmp_path / "build", tmp_path / "root"
    build_dir.mkdir()
    root.mkdir()
    (root / ".python-version").write_text("3.11\n", encoding="utf-8")

    cli.pin_build_interpreter(build_dir, root, pin_python="3.14.0")

    assert (build_dir / ".python-version").read_text(encoding="utf-8") == "3.14.0\n"


def test_pin_build_interpreter_copies_existing_pin(tmp_path):
    build_dir, root = tmp_path / "build", tmp_path / "root"
    build_dir.mkdir()
    root.mkdir()
    (root / ".python-version").write_text("3.11\n", encoding="utf-8")

    cli.pin_build_interpreter(build_dir, root, pin_python=None)

    assert (build_dir / ".python-version").read_text(encoding="utf-8") == "3.11\n"


def test_pin_build_interpreter_noop_without_pin_or_full(tmp_path):
    build_dir, root = tmp_path / "build", tmp_path / "root"
    build_dir.mkdir()
    root.mkdir()

    cli.pin_build_interpreter(build_dir, root, pin_python=None)

    assert not (build_dir / ".python-version").exists()


def test_main_full_skips_bump_and_pin_when_nothing_installed(tmp_path, monkeypatch, capsys):
    # no installed Python found -> neither the requires-python bump nor the
    # .python-version pin should happen; build_and_swap still runs (a plain
    # dependency refresh), just with new_requires_python=None.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--full"])
    monkeypatch.setattr(cli, "latest_installed_python", lambda: None)

    build_calls = []
    monkeypatch.setattr(cli, "build_and_swap", lambda *a, **k: build_calls.append(a))
    refresh_calls = []
    monkeypatch.setattr(cli, "refresh_python_version", lambda *a, **k: refresh_calls.append(a))

    assert cli.main() == 0
    assert len(build_calls) == 1
    assert build_calls[0][-2:] == (None, None)  # no requires-python bump, no pin for the temp build
    assert refresh_calls == []
    assert "no installed Python found" in capsys.readouterr().err


def test_main_full_runs_build_then_pin_with_latest_version(tmp_path, monkeypatch):
    # requires-python is bumped inside build_and_swap (same atomic rebuild as
    # dependencies); .python-version is only re-pinned afterwards, once that
    # rebuild has landed -- see refresh_python_version()'s docstring for why.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--full"])
    monkeypatch.setattr(cli, "latest_installed_python", lambda: "3.14.0")

    order = []
    monkeypatch.setattr(
        cli, "build_and_swap",
        lambda root, pyproject, lock, backup, original_text, specs, args, new_requires_python, pin_python:
            order.append(("build", new_requires_python, pin_python)),
    )
    monkeypatch.setattr(
        cli, "refresh_python_version",
        lambda root, dry, version: order.append(("refresh", version)),
    )

    assert cli.main() == 0
    assert order == [("build", ">=3.14", "3.14.0"), ("refresh", "3.14.0")]


def test_main_full_pin_failure_after_build_reports_but_keeps_the_rebuild(tmp_path, monkeypatch):
    # a failed .python-version pin runs AFTER the atomic pyproject.toml swap,
    # so it must be reported but must not claim the rebuild itself failed.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--full"])
    monkeypatch.setattr(cli, "latest_installed_python", lambda: "3.14.0")

    order = []
    monkeypatch.setattr(cli, "build_and_swap", lambda *a, **k: order.append("build"))

    def failing_refresh(root, dry, version):
        order.append("refresh")
        raise RuntimeError("Command failed: uv python pin 3.14.0")

    monkeypatch.setattr(cli, "refresh_python_version", failing_refresh)

    assert cli.main() == 1
    assert order == ["build", "refresh"]


def test_quiet_suppresses_status_but_not_warnings(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_quiet", True)

    cli.say("status message")
    cli.say("warning message", cli.C_WARN)

    out, err = capsys.readouterr()
    assert "status message" not in out
    assert "warning message" in err
