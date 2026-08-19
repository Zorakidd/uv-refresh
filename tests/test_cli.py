import shutil
import subprocess
import sys
import tomllib

import pytest

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


def _stub_run_writing(resolved_pyproject_text):
    """Fakes cli.run(): 'uv init' seeds a minimal pyproject.toml in the build
    dir, 'uv add' overwrites it with the given already-resolved text -- close
    enough to real uv output for build_and_swap()'s merge step to work on."""

    def fake_run(cmd, cwd, dry, timeout=None):
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


def test_latest_installed_python_picks_first_entry(monkeypatch):
    payload = '[{"version": "3.13.5"}, {"version": "3.11.14"}]'
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=payload),
    )
    assert cli.latest_installed_python() == "3.13.5"


def test_latest_installed_python_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout=""),
    )
    assert cli.latest_installed_python() is None


def test_latest_installed_python_returns_none_when_nothing_installed(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="[]"),
    )
    assert cli.latest_installed_python() is None


def test_refresh_python_version_pins_the_newest_version(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "latest_installed_python", lambda: "3.13.5")
    calls = []
    monkeypatch.setattr(cli, "run", lambda cmd, cwd, dry, timeout=None: calls.append((cmd, cwd, dry)))

    cli.refresh_python_version(tmp_path, dry=False)

    assert calls == [(["uv", "python", "pin", "3.13.5"], tmp_path, False)]


def test_refresh_python_version_warns_when_nothing_installed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "latest_installed_python", lambda: None)
    calls = []
    monkeypatch.setattr(cli, "run", lambda *a, **k: calls.append(a))

    cli.refresh_python_version(tmp_path, dry=False)

    assert calls == []
    assert "no installed Python found" in capsys.readouterr().err


def test_main_full_stops_before_backup_on_pin_failure(tmp_path, monkeypatch):
    # a failure while re-pinning .python-version must never reach the
    # pyproject.toml/uv.lock backup+build steps.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--full"])

    def failing_refresh(root, dry):
        raise RuntimeError("Command failed: uv python pin 3.14.0")

    monkeypatch.setattr(cli, "refresh_python_version", failing_refresh)
    started = []
    monkeypatch.setattr(cli, "build_and_swap", lambda *a, **k: started.append(True))

    assert cli.main() == 1
    assert started == []
    assert not (tmp_path / ".uv-refresh-backup").exists()


def test_main_full_runs_refresh_then_build(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--full"])

    order = []
    monkeypatch.setattr(cli, "refresh_python_version", lambda root, dry: order.append("refresh"))
    monkeypatch.setattr(cli, "build_and_swap", lambda *a, **k: order.append("build"))

    assert cli.main() == 0
    assert order == ["refresh", "build"]


def test_quiet_suppresses_status_but_not_warnings(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_quiet", True)

    cli.say("status message")
    cli.say("warning message", cli.C_WARN)

    out, err = capsys.readouterr()
    assert "status message" not in out
    assert "warning message" in err
