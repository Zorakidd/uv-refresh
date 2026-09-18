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
        tmp_path, monkeypatch,
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
        project, monkeypatch,
        _HEADER + 'dependencies = ["mylib"]\n\n'
        f'[tool.uv.sources]\nmylib = {{ path = "{lib.as_posix()}" }}\n',
    )

    assert code == 0
    assert _floor(deps["mylib"]) == Version("0.1.0")
