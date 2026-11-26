"""task.optional_more_protocols.012: protocol changes stay in the adapter layer.

The shared core (tools, runtime, durable tools, arithmetic, core) must not
depend on any protocol library, and adapters must not re-implement what the
core owns: tool schemas, argument validation, result checks or grant checks.
A new protocol is therefore added by writing an adapter, never by editing the
core. Behavioural equality across adapters is checked by
tests/test_adapter_conformance.py.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "verified_execution"
CORE = ["core.py", "tools.py", "runtime.py", "durable.py", "arithmetic.py"]
ADAPTERS = {"mcp": ROOT / "server_mcp.py", "http": PACKAGE / "http_local.py"}
PROTOCOL_LIBRARIES = {"mcp", "starlette", "uvicorn", "httpx", "fastapi", "grpc", "graphql", "aiohttp"}
# Only the shared core may use these. An adapter importing one is duplicating
# core logic instead of translating it.
CORE_ONLY_NAMES = {"validated_arguments", "require_unsigned_32", "checked_add", "read_checked_add_result",
                   "validate_recorded", "checked_multiply"}


def tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(module: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def imported_names(module: ast.AST) -> set[str]:
    return {alias.name for node in ast.walk(module) if isinstance(node, ast.ImportFrom) for alias in node.names}


@pytest.mark.parametrize("name", CORE)
def test_core_imports_no_protocol_library(name):
    assert not imported_modules(tree(PACKAGE / name)) & PROTOCOL_LIBRARIES


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
def test_adapters_do_not_reimplement_core_checks(adapter):
    module = tree(ADAPTERS[adapter])
    assert not imported_names(module) & CORE_ONLY_NAMES
    # Grants are consulted through ToolRuntime.permits, never the raw authority.
    raw_grant_checks = [node for node in ast.walk(module)
                        if isinstance(node, ast.Attribute) and node.attr == "permits"
                        and isinstance(node.value, ast.Attribute) and node.value.attr == "authority"]
    assert not raw_grant_checks


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
def test_adapters_define_no_tool_schema(adapter):
    """Schemas come from contracts/ through ToolSpec; adapters hold none."""
    for node in ast.walk(tree(ADAPTERS[adapter])):
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys if isinstance(key, ast.Constant)}
            assert "properties" not in keys, f"{adapter} adapter defines a schema literal"


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
def test_adapters_name_no_tool_except_the_host_default(adapter):
    """No tool is special-cased by name; only the host's default tool appears."""
    names = {node.value for node in ast.walk(tree(ADAPTERS[adapter]))
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and node.value.startswith("checked_")}
    assert names <= {"checked_add"}, names


def test_every_adapter_reaches_tools_only_through_the_shared_runtime():
    for adapter, path in ADAPTERS.items():
        names = imported_names(tree(path))
        assert "ToolRuntime" in names, adapter
        assert {"ToolSpec", "ToolRegistry"} & names, adapter
