"""Attribute a private parser factory's literal defaults to its baseline callbacks."""

import ast

from build_map import COLLECTION, union
from semantic_edits import argument_defaults
from semantics import is_closure_factory


def registry_calls(tree):
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.decorator_list:
            continue
        for statement in cls.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            if (
                not isinstance(target, ast.Name)
                or not (target.id == "FUNCTIONS" or target.id.endswith("_PARSERS"))
                or not isinstance(statement.value, ast.Dict)
            ):
                continue
            for key, call in zip(statement.value.keys, statement.value.values):
                if (
                    isinstance(key, ast.Constant)
                    and type(key.value) is str
                    and isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and not any(isinstance(arg, ast.Starred) for arg in call.args)
                    and all(kw.arg is not None for kw in call.keywords)
                ):
                    yield call


def factory_default_profile(trees, calls, metadata, baseline):
    # Qualified references, imports, and literal reflective lookups can bypass a
    # module's visible call sites. Keep the original selection for those names.
    escaping = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                escaping.add(node.attr)
            elif isinstance(node, ast.alias):
                escaping.update((node.name, node.asname))
            elif isinstance(node, ast.Constant) and type(node.value) is str:
                escaping.add(node.value)

    def mask(contexts):
        return union(baseline.indices.get(test, 0) for test in contexts)

    result = {}
    for path, tree in trees.items():
        if not path.startswith("sqlglot/parsers/"):
            continue
        nodes = tuple(ast.walk(tree))
        constructors = tuple(registry_calls(tree))
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            name = function.name
            if (
                not name.startswith("_")
                or name in escaping
                or not is_closure_factory(tree, name)
            ):
                continue
            defaults = argument_defaults(function.args)
            if not defaults:
                continue
            if any(
                node is not function
                and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == name
                for node in nodes
            ):
                continue
            uses = {node for node in nodes if isinstance(node, ast.Name) and node.id == name}
            registry = [call for call in constructors if call.func.id == name and call.lineno > function.end_lineno]
            if not registry or uses != {call.func for call in registry}:
                continue
            qualified = f"{path}:{name}"
            readers = metadata.get(qualified, ())
            if COLLECTION in readers:
                continue
            positional = [arg.arg for arg in function.args.posonlyargs + function.args.args]
            witnesses = {}
            for parameter in defaults:
                affected = set()
                for call in registry:
                    supplied = set(positional[:len(call.args)]) | {kw.arg for kw in call.keywords}
                    if parameter in supplied:
                        continue
                    site = f"{path}:{call.lineno}:{call.col_offset}"
                    if site not in calls or COLLECTION in calls[site]:
                        break
                    affected.update(calls[site])
                else:
                    witnesses[parameter] = format(mask(affected), "x")
            if witnesses:
                result[qualified] = [format(mask(readers), "x"), witnesses]
    return result
