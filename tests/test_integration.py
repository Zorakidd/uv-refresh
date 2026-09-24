"""End-to-end tests against the real uv CLI.

Everything in test_cli.py stubs out cli.run() -- yet every bug found while
building --full, and each one these cover, only showed up once real uv ran.
Needs uv on PATH and network access to PyPI; skip with -m 'not integration'.
"""

import shutil
import sys
import tomllib

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from uv_refresh import cli

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uv") is None, reason="needs the uv CLI on PATH"),
]

_HEADER = '[project]\nname = "demo"\nversion = "1.0.0"\nrequires-python = ">=3.11"\n'


def _refresh(project, monkeypatch, pyproject_text):
    """Runs the real main() --yes on 'project'; returns (exit code, the
    resulting dependencies parsed as Requirements)."""
    pyproject = project / "pyproject.toml"
    pyproject.write_text(pyproject_text, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(project), "--yes"])
    code = cli.main()
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    return code, {r.name: r for r in map(Requirement, deps)}


def _floor(req):
    return min(Version(s.version) for s in req.specifier)


def test_refresh_resolves_a_fresh_bound_and_locks(tmp_path, monkeypatch):
    code, deps = _refresh(tmp_path, monkeypatch, _HEADER + 'dependencies = ["packaging>=20"]\n')

    assert code == 0
    assert _floor(deps["packaging"]) > Version("20")
    assert (tmp_path / "uv.lock").is_file()
    assert not any(tmp_path.glob(".uv-refresh-tmp-*"))
    assert not (tmp_path / ".venv").exists()


def test_refresh_respects_tool_uv_constraints(tmp_path, monkeypatch):
    # regression test: [tool.uv] never reached the temp build, so 'uv add'
    # wrote 'packaging>=26.3' despite this constraint, and the final 'uv lock'
    # then failed (reproduced against real uv).
    code, deps = _refresh(
        tmp_path,
        monkeypatch,
        _HEADER + 'dependencies = ["packaging>=20"]\n\n'
        '[tool.uv]\nconstraint-dependencies = ["packaging<25"]\n',
    )

    assert code == 0
    assert _floor(deps["packaging"]) < Version("25")


def test_refresh_resolves_path_sources_locally(tmp_path, monkeypatch):
    # regression test: without [tool.uv.sources] in the temp build, 'uv add
    # mylib' resolved an unrelated 'mylib' on PyPI and wrote its 'mylib>=0.0.1'
    # bound (reproduced against real uv). Absolute paths work from the temp
    # directory; relative ones are refused up front (see test_cli.py).
    lib = tmp_path / "libs" / "mylib"
    (lib / "src" / "mylib").mkdir(parents=True)
    (lib / "src" / "mylib" / "__init__.py").write_text("", encoding="utf-8")
    (lib / "pyproject.toml").write_text(
        '[project]\nname = "mylib"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n\n'
        '[build-system]\nrequires = ["uv_build>=0.8,<0.13"]\nbuild-backend = "uv_build"\n',
        encoding="utf-8",
    )
    project = tmp_path / "demo"
    project.mkdir()

    code, deps = _refresh(
        project,
        monkeypatch,
        _HEADER + f'dependencies = ["mylib"]\n\n[tool.uv.sources]\nmylib = {{ path = "{lib.as_posix()}" }}\n',
    )

    assert code == 0
    assert _floor(deps["mylib"]) == Version("0.1.0")


_WHEEL = (
    "https://files.pythonhosted.org/packages/ef/a6/62565a6e1cf69e10f5727360368e451d4b7f58beeac6173dc9db836a5b46/"
    "iniconfig-2.0.0-py3-none-any.whl"
)


def test_refresh_keeps_direct_references(tmp_path, monkeypatch):
    # regression test: 'uv add' moved the URL into the temp build's
    # [tool.uv.sources], which is never merged back -- the entry came out as a
    # bare 'iniconfig' and uv.lock took it from PyPI (reproduced against real uv).
    code, deps = _refresh(
        tmp_path,
        monkeypatch,
        _HEADER + f'dependencies = ["iniconfig@{_WHEEL}", "packaging>=20"]\n',
    )

    assert code == 0
    assert deps["iniconfig"].url == _WHEEL
    assert _floor(deps["packaging"]) > Version("20")
    lock = tomllib.loads((tmp_path / "uv.lock").read_text(encoding="utf-8"))
    iniconfig = next(p for p in lock["package"] if p["name"] == "iniconfig")
    assert iniconfig["source"] == {"url": _WHEEL}


def _refresh_text(project, monkeypatch, pyproject_text):
    """Like _refresh(), for what packaging can't parse: returns (exit code,
    the resulting pyproject.toml as text)."""
    pyproject = project / "pyproject.toml"
    pyproject.write_text(pyproject_text, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["uv-refresh", "--path", str(project), "--yes"])
    code = cli.main()
    return code, pyproject.read_text(encoding="utf-8")


def _lock_sources(project, name):
    lock = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))
    return [p["source"] for p in lock["package"] if p["name"] == name]


def test_refresh_keeps_a_direct_reference_with_no_space_before_its_marker(tmp_path, monkeypatch):
    # regression test: packaging rejects 'pkg @ url; marker' -- PEP 508 wants
    # a space before the ';' -- but uv takes it. The entry wasn't seen as a
    # direct reference, went to 'uv add' without --raw, and a bare
    # 'iniconfig ; ...' resolved from PyPI replaced it (reproduced).
    original = _HEADER + f"dependencies = [\"iniconfig @ {_WHEEL}; sys_platform == 'linux'\"]\n"

    code, text = _refresh_text(tmp_path, monkeypatch, original)

    assert code == 0
    assert text == original
    assert _lock_sources(tmp_path, "iniconfig") == [{"url": _WHEEL}]


def test_refresh_keeps_registry_and_direct_entries_of_one_package_apart(tmp_path, monkeypatch, capfd):
    # regression test: uv rewrites both markers, so only the package name
    # matched -- in uv's order. The registry entry's slot got the direct
    # reference, the file ended up with the URL twice and without the
    # dependency for Python < 3.12; the report said 'No bounds changed'.
    registry = "iniconfig>=1.0 ; python_version < '3.12'"
    direct = f"iniconfig @ {_WHEEL} ; python_version >= '3.12'"
    original = _HEADER + f'dependencies = [\n    "{registry}",  # old Pythons\n    "{direct}",\n]\n'

    code, text = _refresh_text(tmp_path, monkeypatch, original)

    assert code == 0
    deps = tomllib.loads(text)["project"]["dependencies"]
    assert deps[1] == direct
    fresh = Requirement(deps[0])
    assert fresh.url is None
    assert str(fresh.marker) == 'python_version < "3.12"'
    assert _floor(fresh) > Version("1.0")
    assert '",  # old Pythons' in text.splitlines()[5]
    assert "iniconfig  >=1.0 -> >=" in capfd.readouterr().out


def test_refresh_only_changes_the_bound(tmp_path, monkeypatch):
    # uv writes its own spelling of names and markers; the file keeps the
    # user's, and every comment stays with the entry it was written for --
    # also when uv sorts the two packaging entries the other way round
    original = _HEADER + (
        "dependencies = [\n"
        "    \"packaging>=20; python_version >= '3.12'\",  # modern line\n"
        "    \"packaging>=19; python_version < '3.12'\",  # old line\n"
        '    "Typing_Extensions>=4.0",\n'
        "]\n"
    )

    code, text = _refresh_text(tmp_path, monkeypatch, original)

    assert code == 0
    lines = text.splitlines()[5:8]
    assert lines[0].endswith("; python_version >= '3.12'\",  # modern line")
    assert lines[1].endswith("; python_version < '3.12'\",  # old line")
    assert lines[2].startswith('    "Typing_Extensions>=')
    deps = [Requirement(d) for d in tomllib.loads(text)["project"]["dependencies"]]
    assert _floor(deps[0]) > Version("20")
    assert _floor(deps[1]) > Version("19")
    assert _floor(deps[2]) > Version("4.0")


def test_refresh_keeps_include_groups(tmp_path, monkeypatch):
    # regression test: the include-group was written back expanded -- a
    # copy of the other group's packages instead of the reference to it
    original = _HEADER + (
        'dependencies = ["packaging>=20"]\n\n'
        '[dependency-groups]\ntest = ["iniconfig>=1.0"]\n'
        'dev = [{include-group = "test"}, "pluggy>=1.0"]\n'
    )

    code, text = _refresh_text(tmp_path, monkeypatch, original)

    assert code == 0
    groups = tomllib.loads(text)["dependency-groups"]
    assert groups["dev"][0] == {"include-group": "test"}
    assert len(groups["dev"]) == 2
    assert _floor(Requirement(groups["dev"][1])) > Version("1.0")
    assert _floor(Requirement(groups["test"][0])) > Version("1.0")


def test_refresh_does_not_report_a_project_version_change(tmp_path, monkeypatch, capfd):
    # regression test: the temp build kept uv init's version 0.1.0, so the
    # final 'uv lock' ended every run with 'Updated demo v0.1.0 -> v1.0.0'.
    code, _ = _refresh(tmp_path, monkeypatch, _HEADER + 'dependencies = ["packaging>=20"]\n')

    assert code == 0
    out, err = capfd.readouterr()
    assert "v0.1.0" not in out + err
