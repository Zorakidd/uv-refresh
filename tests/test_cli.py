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
