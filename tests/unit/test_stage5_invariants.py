"""Static architectural invariants: no execution path, no write path in Stage 5 modules, honest schemas.

These read the source instead of running it, so a future change that adds a process start or a
write to a read-only module fails here even if no behavioral test covers it.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from factory.cli import _parser
from factory.schemas.validation import SCHEMA_FILES, load_schema

SRC = Path(__file__).resolve().parents[2] / "src" / "factory"
ROOT = SRC.parents[1]
STAGE5 = ("archon", "fsread", "gitstate", "preflight", "reconcile", "refs", "runs")

_PROCESS_CALLS = {
    ("os", name)
    for name in (
        "system", "popen", "fork", "forkpty", "execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp",
        "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
        "posix_spawn", "posix_spawnp", "startfile", "kill", "killpg",
    )
}  # fmt: skip
_WRITE_CALLS = {
    ("os", name)
    for name in (
        "write", "rename", "replace", "unlink", "remove", "rmdir", "removedirs", "mkdir", "makedirs", "link",
        "symlink", "truncate", "ftruncate", "chmod", "chown", "utime", "mkfifo",
    )
} | {("shutil", name) for name in ("rmtree", "move", "copy", "copy2", "copyfile", "copytree")}  # fmt: skip
_WRITE_METHODS = {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "rename", "replace", "touch", "symlink_to"}
_WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_EXCL"}


def _modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _attribute_calls(tree: ast.AST) -> set[tuple[str, str]]:
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            calls.add((node.value.id, node.attr))
    return calls


def test_only_the_github_transport_can_start_a_process() -> None:
    for path in _modules():
        tree = _tree(path)
        imported = _imports(tree)
        if path.name != "github.py":
            assert not imported & {"subprocess", "pty", "multiprocessing", "asyncio"}, path
        assert not _attribute_calls(tree) & _PROCESS_CALLS, path


def test_the_github_transport_runs_only_gh_api_get() -> None:
    source = (SRC / "github.py").read_text()
    assert source.count("subprocess.run(") == 1
    assert '"--method",\n            "GET",' in source
    for flag in ("--field", "--raw-field", "--input", '"-f"', '"-F"', '"-X"', "POST", "PATCH", "PUT", "DELETE"):
        assert flag not in source


@pytest.mark.parametrize("module", STAGE5)
def test_stage5_modules_have_no_write_primitives(module: str) -> None:
    tree = _tree(SRC / f"{module}.py")
    assert not _attribute_calls(tree) & _WRITE_CALLS, module
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _WRITE_METHODS:
            raise AssertionError(f"{module}: .{node.attr}")
        if isinstance(node, ast.Attribute) and node.attr in _WRITE_FLAGS:
            raise AssertionError(f"{module}: os.{node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            raise AssertionError(f"{module}: builtin open()")


@pytest.mark.parametrize("module", STAGE5)
def test_stage5_modules_do_not_reach_the_process_transport(module: str) -> None:
    source = (SRC / f"{module}.py").read_text()
    assert "GhCliTransport" not in source
    assert "shell=True" not in source


def _options(parser: argparse.ArgumentParser) -> set[str]:
    found: set[str] = set()
    for action in parser._actions:
        found.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found.add(f"<command {name}>")
                found |= _options(sub)
    return found


def test_cli_has_no_execution_or_mutation_surface() -> None:
    options = _options(_parser())
    assert {"<command preflight>", "<command reconcile>"} <= options
    for word in ("exec", "dispatch", "run", "adopt", "abandon", "retry", "merge", "push", "delete", "force", "yes"):
        assert not [o for o in options if re.search(rf"\b{word}", o.strip("-<>"))], word


def test_every_schema_is_registered_and_valid() -> None:
    files = {p.name for p in (SRC / "schemas").glob("*.schema.json")}
    assert files == set(SCHEMA_FILES.values())
    for kind in SCHEMA_FILES:
        Draft202012Validator.check_schema(load_schema(kind))


_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def test_no_invisible_or_bidirectional_characters_in_the_repository() -> None:
    paths = [*ROOT.joinpath("src").rglob("*"), *ROOT.joinpath("tests").rglob("*"), *ROOT.joinpath("docs").rglob("*")]
    hits = [
        str(p.relative_to(ROOT))
        for p in paths
        if p.is_file() and p.suffix in {".py", ".json", ".md", ".yaml", ".sh"} and _INVISIBLE.search(p.read_text())
    ]
    assert hits == []
