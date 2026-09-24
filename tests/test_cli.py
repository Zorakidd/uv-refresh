import shlex
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
        ('fastapi[standard]>=0.110; python_version<"3.13"', 'fastapi[standard]; python_version < "3.13"'),
        ("pkg @ git+https://example.com/repo.git", "pkg @ git+https://example.com/repo.git"),
        ("   ", None),
    ],
)
def test_strip_version(spec, expected):
    assert cli.strip_version(spec) == expected


def test_strip_version_drop_extras_and_markers():
    spec = 'fastapi[standard]>=0.110; python_version<"3.13"'
    assert cli.strip_version(spec, keep_extras=False, keep_markers=True) == 'fastapi; python_version < "3.13"'
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
        "uv",
        "init",
        "--bare",
        "--no-workspace",
        "--name=demo",
        "--python=>=3.11",
        "--description=a description",
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
    merged = cli.merge_dependencies(original, ["click"], {"speed": ["orjson"]}, {"dev": ["pytest", "ruff"]})
    result = tomllib.loads(merged)
    assert result["project"]["optional-dependencies"] == {"speed": ["orjson"]}
    assert result["dependency-groups"] == {"dev": ["pytest", "ruff"]}


def test_merge_dependencies_keeps_comments_in_dependency_lists():
    # regression test: the lists used to be swapped out wholesale, dropping
    # every comment in them -- the note above a dependency and the one
    # between two groups (reported from a real project).
    original = """\
[project]
name = "demo"
dependencies = [
    # bcrypt is needed for passphrase-protected OpenSSH keys
    "asyncssh[bcrypt]>=2.17",
    "fastapi>=0.115",  # pinned by the API layer
]

[dependency-groups]
dev = [
    "pytest>=8",
]
# end-to-end tests only
e2e = [
    "playwright>=1.45",
]
"""
    merged = cli.merge_dependencies(
        original,
        ["asyncssh[bcrypt]>=2.24.0", "fastapi>=0.141.1"],
        {},
        {"dev": ["pytest>=9.1.1"], "e2e": ["playwright>=1.63.0"]},
    )
    assert merged == original.replace(">=2.17", ">=2.24.0").replace(">=0.115", ">=0.141.1").replace(
        ">=8", ">=9.1.1"
    ).replace(">=1.45", ">=1.63.0")


def test_merge_dependencies_keeps_original_order_and_slots():
    # uv sorts what it adds; each fresh spec still lands in the slot of the
    # entry it replaces, and only its bound changes. uv's output here is what
    # real uv writes: markers rewritten to python_full_version, ' ; ' spacing.
    original = """\
[project]
name = "demo"
dependencies = [
    "zlib-ng>=0.4",
    "numpy>=1.26; python_version < '3.12'",  # last numpy with 3.11 wheels
    "numpy>=2; python_version >= '3.12'",
    "old-pkg>=1",
]
"""
    merged = cli.merge_dependencies(
        original,
        [
            "new-pkg>=1",
            "numpy>=1.26.4 ; python_full_version < '3.12'",
            "numpy>=2.3 ; python_full_version >= '3.12'",
            "zlib-ng>=0.5",
        ],
        {},
        {},
    )
    assert (
        merged
        == """\
[project]
name = "demo"
dependencies = [
    "zlib-ng>=0.5",
    "numpy>=1.26.4; python_version < '3.12'",  # last numpy with 3.11 wheels
    "numpy>=2.3; python_version >= '3.12'",
    "new-pkg>=1",
]
"""
    )


def test_merge_dependencies_matches_same_package_entries_by_what_their_marker_means():
    # regression test: matched by package name alone, in uv's (sorted) order,
    # the two numpy entries swapped places -- and each comment ended up on
    # the other entry (reproduced against real uv)
    original = """\
[project]
name = "demo"
dependencies = [
    "numpy>=2; python_version >= '3.12'",  # modern line
    "numpy>=1.26; python_version < '3.12'",  # last numpy with 3.11 wheels
]
"""
    merged = cli.merge_dependencies(
        original,
        ["numpy>=1.26.4 ; python_full_version < '3.12'", "numpy>=2.3 ; python_full_version >= '3.12'"],
        {},
        {},
    )
    assert merged == original.replace(">=2;", ">=2.3;").replace(">=1.26;", ">=1.26.4;")


def test_merge_dependencies_never_trades_a_registry_entry_for_a_direct_reference():
    # regression test: pass 2 paired the registry entry's slot with uv's
    # direct reference and the other way around; the direct reference's slot
    # kept its text, so the file ended up with the URL twice and the
    # registry dependency for Python < 3.12 gone (reproduced against real uv)
    wheel = "https://example.com/priv-1.0-py3-none-any.whl"
    original = (
        '[project]\nname = "demo"\ndependencies = [\n'
        "    \"priv>=1.0 ; python_version < '3.12'\",\n"
        f"    \"priv @ {wheel} ; python_version >= '3.12'\",\n"
        "]\n"
    )
    merged = cli.merge_dependencies(
        original,
        [f"priv @ {wheel} ; python_full_version >= '3.12'", "priv>=2.3.0 ; python_full_version < '3.12'"],
        {},
        {},
    )
    assert merged == original.replace("priv>=1.0", "priv>=2.3.0")


@pytest.mark.parametrize(
    ("original", "from_uv", "expected"),
    [
        # uv's own spelling of the name and marker stays out of the file
        ("Typing_Extensions>=4.0", "typing-extensions>=4.16.0", "Typing_Extensions>=4.16.0"),
        (
            "pkg>=1; python_version > '3.10'",
            "pkg>=2 ; python_full_version >= '3.11'",
            "pkg>=2; python_version > '3.10'",
        ),
        # no bound yet: it goes after the name/extras, not after the space before the marker
        (
            "numpy ; python_version<'3.13'",
            "numpy>=2.3.0 ; python_full_version < '3.13'",
            "numpy>=2.3.0 ; python_version<'3.13'",
        ),
        ("fastapi[standard]", "fastapi[standard]>=0.141.1", "fastapi[standard]>=0.141.1"),
        # the same bound, only spaced differently: the entry isn't touched at all
        ("requests >= 2.32.0", "requests>=2.32.0", "requests >= 2.32.0"),
        # --raw: the bound goes away, the rest stays
        ("requests>=2.0 ; os_name == 'nt'", "requests ; os_name == 'nt'", "requests ; os_name == 'nt'"),
        ("requests (>=2.0)", "requests>=2.32.0", "requests >=2.32.0"),
        ("pkg>=1,<2", "pkg>=1.5.0,<2.0.0", "pkg>=1.5.0,<2.0.0"),
    ],
)
def test_merge_dependencies_only_swaps_the_bound(original, from_uv, expected):
    merged = cli.merge_dependencies(
        f'[project]\nname = "demo"\ndependencies = ["{original}"]\n', [from_uv], {}, {}
    )
    assert tomllib.loads(merged)["project"]["dependencies"] == [expected]


def test_merge_dependencies_takes_uvs_text_for_what_a_drop_flag_changed():
    # --drop-markers: two numpy entries became one plain 'numpy' -- that change
    # is the point, so uv's text replaces the first and the second goes
    original = (
        '[project]\nname = "demo"\ndependencies = [\n'
        "    \"numpy>=1.26; python_version < '3.12'\",\n"
        "    \"numpy>=2; python_version >= '3.12'\",\n"
        "]\n"
    )
    merged = cli.merge_dependencies(original, ["numpy>=2.3"], {}, {})
    assert tomllib.loads(merged)["project"]["dependencies"] == ["numpy>=2.3"]


def test_merge_dependencies_keeps_literal_strings_literal():
    original = """[project]\nname = "demo"\ndependencies = ['numpy>=1; python_version < "3.12"']\n"""
    merged = cli.merge_dependencies(original, ["numpy>=2 ; python_full_version < '3.12'"], {}, {})
    assert merged == original.replace(">=1", ">=2")


def test_merge_dependencies_keeps_include_groups():
    # regression test: resolve_groups() expands an include-group for uv, and
    # the merge wrote the expansion back -- the include-group was replaced by
    # a copy of the other group's packages, which then drifted apart from it
    original = """\
[project]
name = "demo"
dependencies = ["a>=1"]

[dependency-groups]
test = ["pytest>=8"]
dev = [
    {include-group = "test"},  # shared test tooling
    "ruff>=0.5",
]
"""
    merged = cli.merge_dependencies(
        original, ["a>=2"], {}, {"test": ["pytest>=9"], "dev": ["pytest>=9", "ruff>=0.6"]}
    )
    assert merged == original.replace("a>=1", "a>=2").replace(">=8", ">=9").replace(">=0.5", ">=0.6")


def test_merge_dependencies_follows_nested_include_groups():
    original = (
        '[project]\nname = "demo"\ndependencies = []\n'
        '[dependency-groups]\nlint = ["ruff>=0.5"]\n'
        'test = [{include-group = "lint"}, "pytest>=8"]\n'
        'dev = [{include-group = "test"}, "ipython>=8"]\n'
    )
    merged = cli.merge_dependencies(
        original,
        [],
        {},
        {
            "lint": ["ruff>=0.6"],
            "test": ["pytest>=9", "ruff>=0.6"],
            "dev": ["ipython>=9", "pytest>=9", "ruff>=0.6"],
        },
    )
    assert tomllib.loads(merged)["dependency-groups"] == {
        "lint": ["ruff>=0.6"],
        "test": [{"include-group": "lint"}, "pytest>=9"],
        "dev": [{"include-group": "test"}, "ipython>=9"],
    }


_WHEEL_URL = "https://example.com/priv-1.0-py3-none-any.whl"


@pytest.mark.parametrize(
    "original_spec",
    [
        f"priv@{_WHEEL_URL}",
        # regression test: packaging rejects this spelling (PEP 508 wants a
        # space before the ';'), uv accepts it -- the entry wasn't recognised
        # as a direct reference at all, and a bare 'priv' took its place
        f"priv @ {_WHEEL_URL}; sys_platform == 'linux'",
        f"priv@{_WHEEL_URL};sys_platform == 'linux'",
    ],
)
@pytest.mark.parametrize("from_uv", ["priv", "priv @ {url} ; sys_platform == 'linux'", "priv @ {url}"])
def test_merge_dependencies_keeps_direct_references_verbatim(original_spec, from_uv):
    # regression test: uv writes a direct reference back as a bare 'priv'
    # (URL moved to [tool.uv.sources], never merged) -- which then resolved
    # from PyPI -- or at best respaced. Either way the original text stays.
    original = f'[project]\nname = "demo"\ndependencies = ["{original_spec}", "a>=1"]\n'
    merged = cli.merge_dependencies(original, [from_uv.format(url=_WHEEL_URL), "a>=2"], {}, {})
    assert merged == original.replace("a>=1", "a>=2")


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
    # the real value, not rounded: '0.1' used to read 'longer than 0s'
    with pytest.raises(RuntimeError, match=r"ran longer than 0\.1s"):
        cli.run([sys.executable, "-c", "import time; time.sleep(2)"], tmp_path, dry=False, timeout=0.1)


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


@pytest.mark.parametrize("layout", ["worktree", "subdirectory"])
def test_ensure_backup_ignored_finds_every_kind_of_repo(tmp_path, layout):
    # regression test: only 'root/.git' as a directory counted -- in a git
    # worktree or submodule (.git is a file there) and in any project below
    # a repo's top level, a backup holding credentials was never ignored.
    if layout == "worktree":
        root = tmp_path
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    else:
        (tmp_path / ".git").mkdir()
        root = tmp_path / "packages" / "demo"
        root.mkdir(parents=True)
    cli.ensure_backup_ignored(root)
    assert ".uv-refresh-backup/" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()


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


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_ensure_backup_ignored_leaves_a_subproject_alone_when_the_repo_already_ignores_it(tmp_path):
    # regression test: every subproject of a monorepo got its own .gitignore
    # (and a warning), even with both patterns in the repo's root .gitignore
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".uv-refresh-backup/\n.uv-refresh-tmp-*/\n", encoding="utf-8")
    root = tmp_path / "packages" / "demo"
    root.mkdir(parents=True)

    cli.ensure_backup_ignored(root)

    assert not (root / ".gitignore").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_ensure_backup_ignored_adds_only_what_the_repo_does_not_ignore_yet(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".uv-refresh-backup/\n", encoding="utf-8")
    root = tmp_path / "packages" / "demo"
    root.mkdir(parents=True)

    cli.ensure_backup_ignored(root)

    assert (root / ".gitignore").read_text(encoding="utf-8").splitlines() == [".uv-refresh-tmp-*/"]


def test_main_dry_run_leaves_project_untouched(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
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
                '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8"
            )
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

    monkeypatch.setattr(
        cli,
        "run",
        _stub_run_writing(
            '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["requests==2.31.0"]\n'
        ),
    )
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
                '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8"
            )
        elif cmd[:2] == ["uv", "add"]:
            seen_at_add["python_version"] = (cwd / ".python-version").read_text(encoding="utf-8")
            seen_at_add["pyproject"] = tomllib.loads((cwd / "pyproject.toml").read_text(encoding="utf-8"))
            (cwd / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["torch==2.5.1"]\n',
                encoding="utf-8",
            )

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
        "[tool.ruff]\nline-length = 100\n",
        encoding="utf-8",
    )
    seen = []  # (uv add command, the build pyproject.toml as that command found it)

    def fake_run(cmd, cwd, dry, timeout=None):
        if cmd[:2] == ["uv", "init"]:
            (cwd / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = []\n', encoding="utf-8"
            )
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
    assert build["project"]["version"] == "1.0.0"  # not uv init's, see seed_build_pyproject()


def _main_add_calls(tmp_path, monkeypatch, pyproject_text):
    """main() --yes with a stubbed uv; returns every 'uv add' command it ran."""
    (tmp_path / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    calls = []
    monkeypatch.setattr(cli, "run", _stub_run_writing(pyproject_text, calls))
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])
    assert cli.main() == 0
    return [c for c in calls if c[:2] == ["uv", "add"]]


@pytest.mark.parametrize(
    "wheel",
    [
        "priv @ https://example.com/priv-1.0-py3-none-any.whl",
        # regression test: invalid PEP 508 (no space before ';'), but uv takes
        # it -- it went to uv without --raw, and the URL got lost
        "priv @ https://example.com/priv-1.0-py3-none-any.whl; sys_platform == 'linux'",
    ],
)
def test_build_adds_direct_references_first_and_raw(tmp_path, monkeypatch, wheel):
    # without --raw, uv moves the URL into [tool.uv.sources], which the merge
    # never carries back; first, since a regular dependency may need them.
    adds = _main_add_calls(
        tmp_path,
        monkeypatch,
        '[project]\nname = "demo"\nversion = "1.0.0"\n'
        f'dependencies = ["a>=1", "{wheel}", "b>=1"]\n\n'
        '[dependency-groups]\ndev = ["pytest>=8"]\n',
    )
    assert adds == [
        ["uv", "add", "--no-sync", "--raw", "--", wheel],
        ["uv", "add", "--no-sync", "--", "a", "b"],
        ["uv", "add", "--no-sync", "--group", "dev", "--", "pytest"],
    ]


def test_build_never_hands_a_dependency_to_uv_as_an_option(tmp_path, monkeypatch):
    # an invalid entry containing '@' is passed on as-is (see strip_version)
    # -- without '--', uv took this one as its own --index-url flag.
    adds = _main_add_calls(
        tmp_path,
        monkeypatch,
        '[project]\nname = "demo"\nversion = "1.0.0"\n'
        'dependencies = ["--index-url=https://evil.example/@x"]\n',
    )
    assert adds == [["uv", "add", "--no-sync", "--", "--index-url=https://evil.example/@x"]]


def test_specs_from_only_calls_real_direct_references_so(capsys):
    specs = cli.specs_from(
        ["--index-url=https://evil.example/@x", "priv @ https://example.com/p.whl; os_name == 'nt'"],
        True,
        True,
    )
    err = capsys.readouterr().err
    assert len(specs) == 2
    assert "--index-url=https://evil.example/@x: direct reference" not in err
    assert "priv @ https://example.com/p.whl; os_name == 'nt': direct reference, left unchanged" in err


def test_main_says_so_when_it_fails_after_the_swap(tmp_path, monkeypatch, capsys):
    # regression test: the change report ran after the swap but inside the
    # error handling -- a Ctrl+C there read 'pyproject.toml unchanged' for a
    # file that had already been replaced (reproduced)
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
    pyproject.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli, "run", _stub_run_writing(original.replace(">=2.0", ">=2.32.0")))
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])
    real = cli.build_and_swap

    def interrupted_after_swap(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "build_and_swap", interrupted_after_swap)

    assert cli.main() == 1
    err = capsys.readouterr().err
    assert "already replaced" in err
    assert "unchanged" not in err
    assert ">=2.32.0" in pyproject.read_text(encoding="utf-8")


def test_main_reports_changes_only_once_the_run_succeeded(tmp_path, monkeypatch, capsys):
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
    pyproject.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli, "run", _stub_run_writing(original.replace(">=2.0", ">=2.32.0")))
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])

    assert cli.main() == 0
    out = capsys.readouterr().out
    assert out.index(">=2.0 -> >=2.32.0") < out.index("Done.")


def _locked_temp_build(monkeypatch, unlock_after=None):
    """Makes shutil.rmtree fail on the temp build, the way Windows refuses
    while a killed uv still holds it -- for good, or for the first
    'unlock_after' tries. Returns the list of tries made."""
    real_rmtree, tries = shutil.rmtree, []

    def rmtree(path, ignore_errors=False, **kwargs):
        if ".uv-refresh-tmp-" not in str(path):
            return real_rmtree(path, ignore_errors=ignore_errors, **kwargs)
        tries.append(path)
        if unlock_after is not None and len(tries) > unlock_after:
            return real_rmtree(path, ignore_errors=ignore_errors, **kwargs)
        return None  # 'ignore_errors': the directory simply stays

    monkeypatch.setattr(cli.shutil, "rmtree", rmtree)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    return tries


def _main_with_stub(tmp_path, monkeypatch, fail=False):
    original = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
    (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")
    stub = _stub_run_writing(original.replace(">=2.0", ">=2.32.0"))

    def run(cmd, cwd, dry, timeout=None):
        if fail and cmd[:2] == ["uv", "add"]:
            raise RuntimeError("Command ran longer than 0.05s and was aborted: uv add")
        stub(cmd, cwd, dry, timeout)

    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])
    return cli.main()


def test_main_warns_about_a_temp_build_it_could_not_remove(tmp_path, monkeypatch, capsys):
    # a leftover temp build must not fail a refresh that already landed
    tries = _locked_temp_build(monkeypatch)

    assert _main_with_stub(tmp_path, monkeypatch) == 0
    assert len(tries) == cli._CLEANUP_ATTEMPTS
    assert "could not remove the temp build" in capsys.readouterr().err


def test_main_failure_keeps_its_own_error_and_reports_a_leftover_temp_build(tmp_path, monkeypatch, capsys):
    # regression test: on Windows, the uv --timeout killed still held the temp
    # build -- its removal failed with '[WinError 32]', which replaced the
    # timeout message (reproduced); ignoring that instead left the directory
    # behind without a word
    _locked_temp_build(monkeypatch)

    assert _main_with_stub(tmp_path, monkeypatch, fail=True) == 1
    err = capsys.readouterr().err
    assert "Command ran longer than 0.05s" in err
    assert "could not remove the temp build" in err
    assert "pyproject.toml unchanged" in err


def test_main_retries_a_temp_build_that_is_only_briefly_locked(tmp_path, monkeypatch, capsys):
    tries = _locked_temp_build(monkeypatch, unlock_after=3)

    assert _main_with_stub(tmp_path, monkeypatch, fail=True) == 1
    assert len(tries) == 4
    assert "could not remove the temp build" not in capsys.readouterr().err
    assert not list(tmp_path.glob(".uv-refresh-tmp-*"))


def test_replaced_since_without_a_backup_is_false(tmp_path):
    (tmp_path / "pyproject.toml").write_text("x", encoding="utf-8")
    assert cli._replaced_since(tmp_path / "pyproject.toml", tmp_path / "no-backup") is False


def test_main_verbose_prints_no_bom(tmp_path, monkeypatch, capsys):
    # regression test: the kept BOM was printed as U+FEFF, which crashed the
    # finished run on a console that can't encode it (Windows cp1252, redirected)
    text = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
    (tmp_path / "pyproject.toml").write_bytes(b"\xef\xbb\xbf" + text.encode())
    monkeypatch.setattr(cli, "run", _stub_run_writing(text.replace(">=2.0", ">=2.32.0")))
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--verbose"])

    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "﻿" not in out
    assert 'dependencies = ["requests>=2.32.0"]' in out


@pytest.mark.parametrize(("bom", "newline"), [(False, "\n"), (True, "\n"), (False, "\r\n"), (True, "\r\n")])
def test_main_keeps_bom_and_line_endings(tmp_path, monkeypatch, bom, newline):
    # regression test: read_text()/write_text() wrote the platform's line
    # endings (an LF file came back CRLF on Windows), and a UTF-8 BOM, which
    # uv accepts, was rejected as 'not valid TOML' (both reproduced).
    text = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.replace("\n", newline).encode())
    monkeypatch.setattr(cli, "run", _stub_run_writing(text.replace(">=2.0", ">=2.32.0")))
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes"])

    assert cli.main() == 0
    expected = text.replace(">=2.0", ">=2.32.0").replace("\n", newline).encode()
    assert pyproject.read_bytes() == (b"\xef\xbb\xbf" if bom else b"") + expected


def test_say_redacts_credentials_in_urls(capsys):
    # regression test: a direct reference's token was printed verbatim in the
    # preview, the '$ uv add' echo, errors and the change report.
    cli.say("  $ uv add 'priv @ git+https://alice:ghp_SECRET@github.com/acme/priv'")
    cli.say("priv @ https://ghp_SECRET@example.com/x.whl: direct reference", cli.C_WARN)
    cli.say("index https://pypi.org/simple and pkg @ file:///C:/wheels/x.whl")
    out, err = capsys.readouterr()
    assert "SECRET" not in out + err
    assert "git+https://***@github.com/acme/priv" in out
    assert "https://***@example.com/x.whl" in err
    assert "https://pypi.org/simple and pkg @ file:///C:/wheels/x.whl" in out  # nothing else touched


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
    monkeypatch.setattr(
        cli,
        "run",
        _stub_run_writing(
            '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["requests==2.31.0"]\n', calls
        ),
    )
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
        (_D + '[tool.uv]\nfind-links = ["./wheels"]\n', "relative index or find-links path ('./wheels')"),
        (_D + '[tool.uv]\nindex-url = "idx"\n', "relative index or find-links path"),
        (_D + '[tool.uv]\nextra-index-url = ["../idx"]\n', "relative index or find-links path"),
        (_D + '[[tool.uv.index]]\nname = "i"\nurl = "./idx"\nformat = "flat"\n', "relative index"),
        (_D + f'[tool.uv]\nfind-links = ["{_HERE}"]\n', None),
        (_D + '[tool.uv]\nfind-links = ["https://download.pytorch.org/whl/torch_stable.html"]\n', None),
        (_D + '[[tool.uv.index]]\nname = "i"\nurl = "https://example.com/simple"\n', None),
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


@pytest.mark.parametrize(
    ("tool_uv", "blocker"),
    [
        ("", None),
        ('[tool.uv]\ndefault-groups = ["lint"]\n', "default-groups lists lint"),
        # legacy dev-dependencies doesn't satisfy it either (reproduced against real uv)
        ('[tool.uv]\ndev-dependencies = ["y"]\ndefault-groups = ["dev"]\n', "default-groups lists dev"),
        ('[tool.uv]\ndefault-groups = "all"\n', None),
        ('[tool.uv]\nconflicts = [[{ extra = "a" }, { extra = "b" }]]\n', None),  # uv accepts that
        ('[tool.uv.sources]\nx = [{ index = "cu", extra = "gpu" }]\n', "only applies to extra 'gpu'"),
        ('[tool.uv.sources]\nx = { git = "https://example.com/x.git", group = "lint" }\n', "group 'lint'"),
        # ... but it does satisfy a source's 'group = "dev"' -- only if it lists
        # that very package (both reproduced against real uv)
        (
            '[tool.uv]\ndev-dependencies = ["My_Pkg>=1"]\n\n'
            '[tool.uv.sources]\nmy-pkg = { git = "https://example.com/x.git", group = "dev" }\n',
            None,
        ),
        (
            '[tool.uv]\ndev-dependencies = ["other"]\n\n'
            '[tool.uv.sources]\nx = { git = "https://example.com/x.git", group = "dev" }\n',
            "group 'dev'",
        ),
        ('[tool.uv.sources]\nx = { git = "https://example.com/x.git" }\n', None),
    ],
)
def test_no_groups_blocker(tool_uv, blocker):
    result = cli.no_groups_blocker(tomllib.loads(tool_uv).get("tool", {}).get("uv", {}))
    if blocker is None:
        assert result is None
    else:
        assert result is not None and blocker in result


def test_main_no_groups_refuses_a_tool_uv_that_names_a_group_before_any_backup(tmp_path, monkeypatch, capsys):
    # regression test: --no-groups removed [dependency-groups] but kept
    # [tool.uv] default-groups naming one -- 'uv add' then failed with
    # "Default group `lint` ... is not defined", after the backup (reproduced
    # against real uv).
    pyproject = tmp_path / "pyproject.toml"
    original = (
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n\n'
        '[dependency-groups]\nlint = ["ruff"]\n\n[tool.uv]\ndefault-groups = ["lint"]\n'
    )
    pyproject.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--no-groups"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "Can't use --no-groups here: [tool.uv] default-groups lists lint" in capsys.readouterr().err
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
                '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8"
            )
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


def test_main_confirmation_aborts_on_no(tmp_path, monkeypatch):
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

    monkeypatch.setattr(
        cli,
        "run",
        _stub_run_writing(
            '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["requests==2.31.0"]\n'
        ),
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--yes", "--keep-backups", "2"])

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


@pytest.mark.skipif(sys.platform != "win32", reason="icacls is Windows-only")
# pytest turns the reader thread's exception into this warning instead of
# letting it print -- so the warning is what has to fail the test
@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_restrict_to_owner_is_quiet_for_a_path_with_umlauts(tmp_path, capfd):
    # regression test: icacls answers in the OEM code page, text=True decoded
    # that as ANSI -- 'ü' is 0x81 in cp850/cp437 and nothing in cp1252, and
    # the reader thread printed a UnicodeDecodeError traceback on every run
    # from a path like C:\Users\Jürgen\... (reproduced)
    backup = tmp_path / "Jürgen Müller"
    backup.mkdir()

    cli.restrict_to_owner(backup)

    out, err = capfd.readouterr()
    assert "Traceback" not in out + err
    assert "could not restrict" not in err


def test_latest_installed_python_reads_uvs_output_as_utf8(monkeypatch):
    # uv writes UTF-8; the locale's code page (cp1252 on Windows) garbled the
    # interpreter paths in its JSON and failed on some ('Á' is C3 81 in UTF-8)
    seen = {}
    payload = '[{"version": "3.13.5", "path": "C:/Ágnes/python.exe"}]'

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=payload)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.latest_installed_python() == "3.13.5"
    assert seen.get("encoding") == "utf-8"


def test_restrict_to_owner_warns_when_icacls_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1))

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
        cli.subprocess,
        "run",
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
        cli.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=payload),
    )
    assert cli.latest_installed_python() == expected


def test_latest_installed_python_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout=""),
    )
    assert cli.latest_installed_python() is None


def test_latest_installed_python_returns_none_on_timeout(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", _hanging_run)
    assert cli.latest_installed_python() is None


def test_latest_installed_python_returns_none_when_nothing_installed(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
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
        ("<3.13", "3.14.7", ">=3.14"),  # no floor at all
        (">=3.10,<3.13", "3.14.7", ">=3.14"),
        (">=3.14", "3.14.7", None),  # already there -- no churn
        (">=3.14.0", "3.14.7", None),  # same (3.14.0 == 3.14)
        (">=3.14.2", "3.14.7", None),  # '>=3.14' would loosen it
        (">3.14", "3.14.7", None),  # same
        ("~=3.14", "3.14.7", None),  # '~=' and '==X.*' floor too
        ("==3.14.*", "3.14.7", None),
        (">=3.12,>=3.14.2", "3.14.7", None),  # the highest floor counts
        (">=3.15", "3.14.7", None),  # would LOWER it
        ("===3.15.0", "3.14.7", None),  # same, via arbitrary equality
    ],
)
def test_bumped_requires_python_only_ever_raises(requires_python, version, expected):
    assert cli.bumped_requires_python(SpecifierSet(requires_python), version) == expected


@pytest.mark.parametrize(
    ("requires_python", "latest", "expected"),
    [
        (">=3.11", "3.14.7", (">=3.14", "3.14.7")),  # bump + pin
        (None, "3.14.7", (">=3.14", "3.14.7")),
        (">=3.10,<3.13", "3.14.7", (">=3.14", "3.14.7")),  # old cap doesn't block a bump
        (">=3.14", "3.14.7", (None, "3.14.7")),  # keep + pin
        ("==3.14.*", "3.14.7", (None, "3.14.7")),
        (">=3.15", "3.14.7", (None, None)),  # older than the floor
        ("==3.13", "3.13.5", (None, None)),  # kept, but excludes 3.13.5
        (">=3.11.*", "3.14.7", (None, None)),  # can't parse -> don't guess
        (">=3.11", None, (None, None)),  # nothing installed
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


def test_pin_build_interpreter_dry_run_only_says_so(tmp_path, capsys):
    # --dry-run builds "in" the project root itself (nothing is created), so
    # it must only report the pin, never write one
    build_dir, root = tmp_path / "build", tmp_path / "root"
    build_dir.mkdir()
    root.mkdir()
    (root / ".python-version").write_text("3.11\n", encoding="utf-8")

    cli.pin_build_interpreter(build_dir, root, pin_python=None, dry=True)

    assert not (build_dir / ".python-version").exists()
    assert "temp build would be pinned to Python 3.11" in capsys.readouterr().out


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
        cli,
        "build_and_swap",
        lambda root, pyproject, lock, backup, original_text, specs, args, new_requires_python, pin_python: (
            order.append(("build", new_requires_python, pin_python))
        ),
    )
    monkeypatch.setattr(
        cli,
        "refresh_python_version",
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


# ---- console output -------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("requests>=2.0", ("requests", ">=2.0")),
        ("requests", ("requests", "")),
        ("Flask_Login>=1", ("flask-login", ">=1")),
        ('fastapi[standard]>=0.110; python_version<"3.13"', ("fastapi", ">=0.110")),
        ("pkg @ git+https://example.com/r.git", ("pkg", "@ git+https://example.com/r.git")),
        ("!!!", None),
    ],
)
def test_name_and_bound(spec, expected):
    assert cli._name_and_bound(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "pkg @ https://example.com/x.whl; os_name == 'nt'",
        "pkg@https://example.com/x.whl;os_name == 'nt'",
        "pkg[extra] @ git+https://example.com/r.git@v1; python_version >= '3.12'",
    ],
)
def test_direct_references_packaging_rejects_are_still_direct_references(spec):
    # regression test: packaging wants a space before a ';' after a URL, uv
    # doesn't -- None here sent the entry to uv without --raw, and its URL was
    # replaced by a bare, PyPI-resolved 'pkg' (reproduced against real uv)
    assert cli._is_direct_reference(spec)
    parsed = cli._name_and_bound(spec)
    assert parsed is not None
    assert parsed[0] == "pkg"


def test_parse_entry_normalizes_what_matching_compares():
    a = cli._parse_entry("Fast_API[Standard,all] >= 0.110 ; python_version<'3.13'")
    b = cli._parse_entry('fast-api[all,standard]>=0.110; python_version < "3.13"')
    assert (
        a == b == cli._Entry("fast-api", frozenset({"standard", "all"}), ">=0.110", 'python_version < "3.13"')
    )


@pytest.mark.parametrize(
    ("original", "from_uv"),
    [
        # what real uv writes for these (see the integration tests)
        ("python_version < '3.12'", "python_full_version < '3.12'"),
        ("python_version > '3.10'", "python_full_version >= '3.11'"),
        ("python_version == '3.11'", "python_full_version == '3.11.*'"),
        (
            "python_version <= '3.11' and sys_platform == 'win32'",
            "python_full_version < '3.12' and sys_platform == 'win32'",
        ),
    ],
)
def test_marker_signature_sees_through_uvs_rewrite(original, from_uv):
    assert cli._marker_signature(original) == cli._marker_signature(from_uv) is not None


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("python_version < '3.12'", "python_version >= '3.12'"),
        ("sys_platform == 'linux'", "sys_platform == 'darwin'"),
        ("platform_machine == 'arm64'", "platform_machine == 'x86_64'"),
        ("implementation_name == 'pypy'", None),
    ],
)
def test_marker_signature_tells_different_markers_apart(a, b):
    assert cli._marker_signature(a) != cli._marker_signature(b)


def test_marker_signature_gives_up_on_what_it_cannot_evaluate():
    assert cli._marker_signature("not a marker") is None


def test_collect_bounds_spans_every_section():
    data = tomllib.loads(
        '[project]\nname = "d"\ndependencies = ["a>=1"]\n'
        '[project.optional-dependencies]\nweb = ["b==2"]\n'
        '[dependency-groups]\ndev = ["c"]\n'
    )
    assert cli.collect_bounds(data) == {
        ("dependencies", "a"): (">=1",),
        ("optional [web]", "b"): ("==2",),
        ("group [dev]", "c"): ("",),
    }


def test_collect_bounds_ignores_non_string_entries():
    data = {"dependency-groups": {"dev": [{"include-group": "other"}, "a>=1"]}}
    assert cli.collect_bounds(data) == {("group [dev]", "a"): (">=1",)}


def test_report_changes_lists_every_moved_bound(capsys):
    old = '[project]\nname = "d"\ndependencies = ["a>=1.0", "b", "c>=3.0"]\n'
    new = '[project]\nname = "d"\ndependencies = ["a>=2.0", "b>=9.1", "c>=3.0"]\n'

    cli.report_changes(old, new)

    out = capsys.readouterr().out
    assert "Updated 2 of 3 dependencies:" in out
    assert ">=1.0 -> >=2.0" in out
    assert "(none) -> >=9.1" in out
    assert "(1 unchanged)" in out
    assert ">=3.0" not in out  # c never moved, so it gets no row of its own


def test_report_changes_says_so_when_nothing_moved(capsys):
    same = '[project]\nname = "d"\ndependencies = ["a>=1.0"]\n'

    cli.report_changes(same, same)

    assert "1 dependency already current" in capsys.readouterr().out


def test_report_changes_names_removed_dependencies(capsys):
    old = '[project]\nname = "d"\ndependencies = ["a>=1.0"]\n[dependency-groups]\ndev = ["b>=2"]\n'
    new = '[project]\nname = "d"\ndependencies = ["a>=1.0"]\n'

    cli.report_changes(old, new)

    out = capsys.readouterr().out
    assert "removed : b" in out
    # regression test: next to a removal, nothing was said about the bounds at all
    assert "No bounds changed -- 1 dependency already current." in out


def test_report_changes_stays_silent_on_unparseable_toml(capsys):
    cli.report_changes("[project", '[project]\nname = "d"\n')

    assert capsys.readouterr().out == ""


def test_report_changes_sees_every_entry_of_a_package_listed_twice(capsys):
    # regression test: keyed by (section, name), the second numpy entry
    # overwrote the first, so a move in the first read 'No bounds changed'
    old = (
        '[project]\nname="d"\ndependencies=[\n'
        "  \"numpy>=1.26; python_version < '3.12'\",\n"
        "  \"numpy>=2; python_version >= '3.12'\",\n]\n"
    )

    cli.report_changes(old, old.replace(">=1.26", ">=1.26.4"))

    out = capsys.readouterr().out
    assert "Updated 1 of 1 dependency:" in out
    assert "numpy  >=1.26, >=2 -> >=1.26.4, >=2" in out


def test_report_changes_looks_past_a_direct_reference_of_the_same_package(capsys):
    # the direct reference has no bound to compare -- but it hid the move of
    # the registry entry next to it (reproduced against real uv)
    old = (
        '[project]\nname="d"\ndependencies=[\n'
        "  \"iniconfig>=1.0; sys_platform != 'linux'\",\n"
        "  \"iniconfig @ https://example.com/x.whl ; sys_platform == 'linux'\",\n]\n"
    )

    cli.report_changes(old, old.replace(">=1.0", ">=2.0.0"))

    out = capsys.readouterr().out
    assert "iniconfig  >=1.0 -> >=2.0.0" in out
    assert "added" not in out


def test_collect_bounds_counts_a_direct_reference_as_present_without_a_bound():
    data = tomllib.loads(
        '[project]\nname="d"\ndependencies=["priv @ https://example.com/x.whl; os_name == \'nt\'"]\n'
    )
    assert cli.collect_bounds(data) == {("dependencies", "priv"): ()}


def test_run_echoes_a_shell_quotable_command(capsys):
    argv = ["uv", "add", "--no-sync", "fastapi[standard]", 'httpx; sys_platform == "win32"']

    cli.run(argv, Path("."), dry=True)

    echoed = capsys.readouterr().out.strip().removeprefix("$ ").strip()
    # unquoted, the ';' alone made this a different command when pasted
    assert shlex.split(echoed) == argv


def test_run_echoes_single_quoted_markers_readably(capsys):
    # regression test: shlex's escape for a ' made the echo of uv's own
    # marker style read  == '"'"'linux'"'"''  -- and work in no Windows shell
    argv = ["uv", "add", "--raw", "--", "priv @ https://example.com/x.whl ; sys_platform == 'linux'"]

    cli.run(argv, Path("."), dry=True)

    echoed = capsys.readouterr().out.strip().removeprefix("$ ").strip()
    assert echoed.endswith("\"priv @ https://example.com/x.whl ; sys_platform == 'linux'\"")
    assert shlex.split(echoed) == argv


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("plain", "plain"),
        ("a b", "'a b'"),
        ("x ; os_name == 'nt'", "\"x ; os_name == 'nt'\""),
        ("x ; os_name != 'nt'", "\"x ; os_name != 'nt'\""),  # bash/zsh never expand '!='
        # anything a double-quoted string would expand keeps shlex's form
        ("it's $HOME", None),
        ("it's `cmd`", None),
        ('it\'s "quoted"', None),
        ("it's a\\b", None),
        ("it's !history", None),
    ],
)
def test_shell_quote(arg, expected):
    quoted = cli._shell_quote(arg)
    assert quoted == (expected or shlex.quote(arg))
    assert shlex.split(quoted) == [arg]


def test_run_passes_quiet_through_to_uv(monkeypatch):
    monkeypatch.setattr(cli, "_quiet", True)
    seen = []
    monkeypatch.setattr(
        cli.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )

    cli.run(["uv", "add", "x"], Path("."), dry=False)

    assert seen == [["uv", "--quiet", "add", "x"]]


def test_run_leaves_non_uv_commands_alone(monkeypatch):
    monkeypatch.setattr(cli, "_quiet", True)
    seen = []
    monkeypatch.setattr(
        cli.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )

    cli.run(["icacls", "x"], Path("."), dry=False)

    assert seen == [["icacls", "x"]]


def test_say_row_aligns_on_the_shared_width(capsys):
    width = len("group [integration]")
    cli.say_row("dependencies", ["a"], width)
    cli.say_row("group [integration]", ["b"], width)

    first, second = capsys.readouterr().out.splitlines()
    assert first.index(":") == second.index(":")


def test_say_row_wraps_long_lists_with_a_hanging_indent(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_wrap_width", lambda: 40)

    cli.say_row("dependencies", [f"package-{i}" for i in range(12)], len("dependencies"))

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 1
    assert all(len(line) <= 40 for line in lines)
    indent = lines[0].index(":") + 2
    assert all(line.startswith(" " * indent) for line in lines[1:])


def test_say_row_never_breaks_inside_an_item(capsys, monkeypatch):
    # regression test: 'numpy;' ended one line, 'python_version >= "3.12"'
    # started the next -- no telling where one dependency ends
    monkeypatch.setattr(cli, "_wrap_width", lambda: 60)
    items = [
        'httpx; sys_platform == "win32"',
        'numpy; python_version >= "3.12"',
        "rich",
        'uvloop; os_name != "nt"',
    ]

    cli.say_row("dependencies", items, len("dependencies"))

    # each line, minus label and indent, is a run of whole items
    body = [line.split(" : ", 1)[-1].strip().rstrip(",") for line in capsys.readouterr().out.splitlines()]
    assert len(body) > 1
    assert [item for line in body for item in line.split(", ")] == items


@pytest.mark.parametrize("answer", ["y", "yes", "Y", " YES "])
def test_confirm_accepts_yes(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert cli.confirm("go? ") is True


@pytest.mark.parametrize("answer", ["n", "no", "", "   "])
def test_confirm_treats_no_and_empty_as_decline(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert cli.confirm("go? ") is False


def test_confirm_reasks_until_the_answer_is_recognisable(monkeypatch, capsys):
    answers = iter(["maybe", "wat", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli.confirm("go? ") is True
    assert capsys.readouterr().err.count("Please answer y or n.") == 2


def test_confirm_dies_on_eof(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError))

    with pytest.raises(SystemExit):
        cli.confirm("go? ")
    assert "Use --yes to run without confirmation" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["-5", "0", "-0.1"])
def test_positive_seconds_rejects_non_positive(value):
    with pytest.raises(cli.argparse.ArgumentTypeError, match="greater than 0"):
        cli.positive_seconds(value)


@pytest.mark.parametrize("value", ["abc", "nan", "NaN"])
def test_positive_seconds_rejects_non_numbers(value):
    with pytest.raises(cli.argparse.ArgumentTypeError, match="not a number"):
        cli.positive_seconds(value)


@pytest.mark.parametrize("value", ["inf", "1e10", "86401"])
def test_positive_seconds_rejects_what_no_platform_can_wait_for(value):
    # regression test: accepted, then the first uv call on Windows failed
    # with 'cannot convert float infinity to integer' -- after the backup
    with pytest.raises(cli.argparse.ArgumentTypeError, match="at most 86400"):
        cli.positive_seconds(value)


@pytest.mark.parametrize(("value", "expected"), [("12.5", 12.5), ("86400", 86400.0)])
def test_positive_seconds_accepts_a_real_timeout(value, expected):
    assert cli.positive_seconds(value) == expected


def test_main_aligns_the_dependency_rows(tmp_path, monkeypatch, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
        '[dependency-groups]\ntype-checking-and-linting = ["mypy"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path), "--dry-run"])

    assert cli.main() == 0

    rows = [ln for ln in capsys.readouterr().out.splitlines() if " : " in ln]
    assert len({row.index(" : ") for row in rows}) == 1


def test_main_reassures_before_asking_not_after(tmp_path, monkeypatch, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/uv")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(tmp_path)])
    seen = []
    monkeypatch.setattr("builtins.input", lambda prompt: seen.append(capsys.readouterr().out) or "n")

    assert cli.main() == 1
    # everything printed before input() was called
    assert cli.TEMP_NOTE in seen[0]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("requests>=2.0", ("requests", ">=2.0")),
        ("requests", ("requests", "")),
        ("Flask_Login>=1", ("flask-login", ">=1")),
        ('fastapi[standard]>=0.110; python_version<"3.13"', ("fastapi", ">=0.110")),
        ("pkg @ git+https://example.com/r.git", ("pkg", "@ git+https://example.com/r.git")),
        ("pkg[extra] @ https://example.com/x.whl", ("pkg", "@ https://example.com/x.whl")),
        ("pkg @ https://example.com/x.whl; os_name == 'nt'", ("pkg", "@ https://example.com/x.whl")),
        ("torch==2.1.0+cu118", ("torch", "==2.1.0+cu118")),
        ("!!!", None),
        ("foo bar @ https://example.com/x.whl", None),
    ],
)
def test_name_and_bound_fallback_matches_packaging(monkeypatch, spec, expected):
    # the whole no-packaging branch is dead code under the test suite otherwise
    monkeypatch.setattr(cli, "Requirement", None)
    assert cli._name_and_bound(spec) == expected


def test_report_changes_sees_a_move_in_the_second_section(capsys):
    # keyed by name alone, the unchanged main-dep copy hid the group's move
    # and the run reported 'nothing changed'
    old = '[project]\nname="d"\ndependencies=["pytest>=9.0"]\n[dependency-groups]\ndev=["pytest>=6.0"]\n'
    new = '[project]\nname="d"\ndependencies=["pytest>=9.0"]\n[dependency-groups]\ndev=["pytest>=9.0"]\n'

    cli.report_changes(old, new)

    out = capsys.readouterr().out
    assert "Updated 1 of 1 dependency:" in out
    assert ">=6.0 -> >=9.0" in out


def test_report_changes_counts_one_package_once(capsys):
    old = '[project]\nname="d"\ndependencies=["pytest>=6.0"]\n[dependency-groups]\ndev=["pytest>=6.0"]\n'
    new = '[project]\nname="d"\ndependencies=["pytest>=9.0"]\n[dependency-groups]\ndev=["pytest>=9.0"]\n'

    cli.report_changes(old, new)

    out = capsys.readouterr().out
    assert "Updated 1 of 1 dependency:" in out
    assert out.count(">=6.0 -> >=9.0") == 1


def test_report_changes_names_the_section_when_a_package_moved_two_ways(capsys):
    old = '[project]\nname="d"\ndependencies=["pytest>=6.0"]\n[dependency-groups]\ndev=["pytest>=7.0"]\n'
    new = '[project]\nname="d"\ndependencies=["pytest>=9.0"]\n[dependency-groups]\ndev=["pytest>=8.0"]\n'

    cli.report_changes(old, new)

    out = capsys.readouterr().out
    assert "pytest (dependencies)" in out
    assert "pytest (group [dev])" in out


def test_report_changes_does_not_call_a_package_removed_while_it_remains(capsys):
    # --no-groups drops the group copy, but pytest is still a main dependency
    old = '[project]\nname="d"\ndependencies=["pytest>=6.0"]\n[dependency-groups]\ndev=["pytest>=6.0"]\n'
    new = '[project]\nname="d"\ndependencies=["pytest>=6.0"]\n'

    cli.report_changes(old, new)

    assert "removed" not in capsys.readouterr().out


def test_say_row_keeps_the_prefix_when_the_value_is_empty(capsys):
    # textwrap.fill('') returns '' -- the row used to print as a blank line
    cli.say_row("dependencies", [], len("dependencies"))

    assert capsys.readouterr().out.strip() == "dependencies :"


@pytest.mark.parametrize(
    ("n", "expected"), [(0, "0 dependencies"), (1, "1 dependency"), (2, "2 dependencies")]
)
def test_deps_pluralises(n, expected):
    assert cli._deps(n) == expected


def test_report_changes_is_silent_for_a_project_with_no_dependencies(capsys):
    empty = '[project]\nname = "d"\n'

    cli.report_changes(empty, empty)

    assert capsys.readouterr().out == ""
