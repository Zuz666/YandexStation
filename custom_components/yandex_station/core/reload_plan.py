"""Static source inventory for the stable lifecycle anchor.

The anchor stores only built-in data, never instances or callables from this
reloadable module. All disk access and AST parsing run in an executor.
"""

import ast
import hashlib
import importlib.util
from pathlib import Path

ROOT = "custom_components.yandex_station"


def _module_name(root: Path, path: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((ROOT, *parts))


def _bindings(tree: ast.Module) -> set[str]:
    """Top-level names retained by importlib.reload, including guarded imports."""
    names = set()

    def assigned(target):
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for part in target.elts:
                assigned(part)

    def visit(statements):
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Import):
                names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    assigned(target)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                assigned(node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                assigned(node.target)
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, (ast.If, ast.While, ast.With, ast.AsyncWith)):
                visit(node.body)
                visit(getattr(node, "orelse", ()))
            elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                visit(node.body)
                for handler in node.handlers:
                    if handler.name:
                        names.add(handler.name)
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)

    visit(tree.body)
    return names


def _imports(tree: ast.Module, module: str, modules: set[str]) -> set[str]:
    deps = set()
    package = module if module in {ROOT, ROOT + ".core", ROOT + ".hass"} else module.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package
                )
            else:
                base = node.module or ""
            candidates = [base, *(base + "." + alias.name for alias in node.names)]
        else:
            continue
        for name in candidates:
            if name in modules and name != module and name != ROOT:
                deps.add(name)
    return deps


def _protected(tree: ast.Module, module: str) -> tuple[str, ...]:
    """Freeze registration/Store contracts and their local constant values."""
    contracts = []
    assignments = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = (statement.target,)
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = statement

    # DOMAIN is imported by both storage owners and by service registrations.
    # The import AST does not reflect its value when core.const changes.
    dependencies = {"DOMAIN"} if module == ROOT + ".core.const" else set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            method = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if (module == ROOT and method in {
                "register_view", "async_register", "async_register_admin_service",
            }) or (module == ROOT and isinstance(node.func, ast.Name)
                   and node.func.id == "register"):
                contracts.append(ast.dump(node, include_attributes=False))
            if method == "Store" or (
                isinstance(node.func, ast.Name) and node.func.id == "Store"
            ):
                contracts.append(ast.dump(node, include_attributes=False))
                for argument in (*node.args, *(kw.value for kw in node.keywords)):
                    dependencies.update(
                        child.id for child in ast.walk(argument)
                        if isinstance(child, ast.Name)
                    )
        if module == ROOT and isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            if any(isinstance(target, ast.Name) and target.id in {
                "CONFIG_SCHEMA", "PLATFORMS", "SPEAKER_PLATFORMS",
            } for target in targets):
                contracts.append(ast.dump(node, include_attributes=False))
        if module.endswith(".core.cache") and isinstance(node, ast.ClassDef):
            if node.name == "StationSnapshot":
                contracts.append(ast.dump(node, include_attributes=False))

    visited = set()
    while dependencies:
        name = dependencies.pop()
        if name in visited:
            continue
        visited.add(name)
        assignment = assignments.get(name)
        if assignment is None:
            continue
        contracts.append(ast.dump(assignment, include_attributes=False))
        if assignment.value is not None:
            dependencies.update(
                child.id for child in ast.walk(assignment.value)
                if isinstance(child, ast.Name)
            )
    return tuple(sorted(contracts))


def scan(root: Path) -> dict:
    """Compile every module and return a reproducible graph and disk snapshot."""
    trees = {}
    records = {}
    for path in root.rglob("*.py"):
        name = _module_name(root, path)
        source = path.read_bytes()
        compile(source, str(path), "exec")
        tree = ast.parse(source, filename=str(path))
        trees[name] = tree
        records[name] = {
            "path": str(path),
            "digest": hashlib.sha256(source).hexdigest(),
            "bindings": frozenset(_bindings(tree)),
            "contracts": _protected(tree, name),
        }
    for name, tree in trees.items():
        records[name]["deps"] = frozenset(_imports(tree, name, set(records)))
    order = []
    pending = {name: set(record["deps"]) for name, record in records.items()}
    while pending:
        ready = sorted(name for name, deps in pending.items() if not deps)
        if not ready:
            raise ValueError("import_cycle")
        for name in ready:
            order.append(name)
            del pending[name]
        for deps in pending.values():
            deps.difference_update(ready)
    if not order or order[-1] != ROOT:
        order = [name for name in order if name != ROOT] + [ROOT]
    for filename in ("manifest.json", "services.yaml"):
        path = root / filename
        records[filename] = {
            "path": str(path),
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {"modules": records, "order": tuple(order)}


def compare(previous: dict, current: dict) -> str | None:
    """Return a stable, non-sensitive reason before any Core unload."""
    before = previous["modules"]
    after = current["modules"]
    if before.keys() != after.keys():
        return "module_set_changed"
    for name in before:
        if name in {"manifest.json", "services.yaml", ROOT + ".core.lifecycle"}:
            if before[name]["digest"] != after[name]["digest"]:
                return "stable_contract_changed"
            continue
        if before[name]["contracts"] != after[name]["contracts"]:
            return "registration_or_storage_schema_changed"
        if before[name]["bindings"] - after[name]["bindings"]:
            return "top_level_binding_removed"
    return None


def unchanged(current: dict) -> bool:
    """Reject changes between preflight and import, including resource files."""
    return all(
        hashlib.sha256(Path(record["path"]).read_bytes()).hexdigest() == record["digest"]
        for record in current["modules"].values()
    )
