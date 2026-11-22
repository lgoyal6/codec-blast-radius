"""Refine coverage with observations from the unmodified baseline."""

import ast
import gzip
import json
from functools import cache

from semantic_edits import COMPARISONS, ORDERING, UNCHANGED, compare_edit


def primitive(encoded):
    kind, value = encoded
    if kind == "float":
        return float.fromhex(value)
    if kind == "bytes":
        return bytes.fromhex(value)
    if kind == "tuple":
        return tuple(primitive(item) for item in value)
    if kind in ("bool", "int", "str", "NoneType", "str-enum"):
        return value
    raise ValueError(kind)


@cache
def same_result(record, before, after):
    if record == "?":
        return False
    identical, left, right = json.loads(record)
    kinds = {left[0], right[0]}
    if "identity" in kinds:
        equal = identical
    else:
        equal = primitive(left) == primitive(right)
        singleton = {"bool", "NoneType", "str-enum"}
        if equal and not kinds <= singleton and ({before, after} & {"Is", "IsNot"}):
            # Identity of an equal, non-singleton value can depend on interning.
            return False
    outcomes = {
        "Eq": equal,
        "NotEq": not equal,
        "Is": identical,
        "IsNot": not identical,
    }
    return outcomes[before] == outcomes[after]


def compare_outcomes(record, before, after, replacement):
    if record == "?":
        return False
    if replacement is UNCHANGED and before not in ORDERING and after not in ORDERING:
        return same_result(record, before, after)
    _, left, right = json.loads(record)
    valid = {"float", "int", "bool", "tuple", "str", "bytes", "NoneType"}
    if (
        not {left[0], right[0]} <= valid
        or before in ("Is", "IsNot")
        or after in ("Is", "IsNot")
    ):
        return False
    try:
        a, b = primitive(left), primitive(right)
        updated = b if replacement is UNCHANGED else replacement
        return COMPARISONS[before](a, b) == COMPARISONS[after](a, updated)
    except TypeError:
        return False


def is_closure_factory(tree, name):
    definitions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(definitions) != 1:
        return False
    function = definitions[0]
    if function.decorator_list or len(function.body) != 2:
        return False
    nested, result = function.body
    if not isinstance(nested, ast.FunctionDef) or nested.decorator_list:
        return False
    if nested.args.defaults or any(nested.args.kw_defaults):
        return False
    postponed = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    return (
        postponed
        and isinstance(result, ast.Return)
        and isinstance(result.value, ast.Name)
        and result.value.id == nested.name
    )


def closure_factory(selector, shape, name):
    imports = [
        (node.module, alias.name)
        for node in shape.tree.body
        if isinstance(node, ast.ImportFrom)
        and not node.level
        and node.module
        and node.module.startswith("sqlglot.")
        for alias in node.names
        if (alias.asname or alias.name) == name
    ]
    if imports:
        if len(imports) != 1:
            return False
        module, identifier = imports[0]
        return is_closure_factory(
            selector.shape(module.replace(".", "/") + ".py").tree, identifier
        )
    return is_closure_factory(shape.tree, name)


def registry_edit(selector, old, new, removed, added):
    if not removed or not added:
        return None
    matches = []
    for shape, lines in ((old, removed), (new, added)):
        calls = []
        for key, assignment in shape.assignments.items():
            if assignment is None or not key[1].endswith("_PARSERS"):
                continue
            if not isinstance(assignment.value, ast.Dict):
                continue
            for name, value in zip(assignment.value.keys, assignment.value.values):
                if not (
                    isinstance(name, ast.Constant)
                    and type(name.value) is str
                    and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and min(lines) >= value.lineno
                    and max(lines) <= value.end_lineno
                    and all(item.arg is not None for item in value.keywords)
                ):
                    continue
                if all(isinstance(node, ast.Constant) for node in call_values(value)):
                    calls.append((key, name.value, value))
        if len(calls) != 1:
            return None
        matches.append(calls[0])
    (old_owner, old_key, earlier), (new_owner, new_key, later) = matches
    if (
        old_owner != new_owner
        or old_key != new_key
        or earlier.func.id != later.func.id
        or len(earlier.args) != len(later.args)
        or [item.arg for item in earlier.keywords]
        != [item.arg for item in later.keywords]
        or [type(item.value) for item in call_values(earlier)]
        != [type(item.value) for item in call_values(later)]
        or not closure_factory(selector, old, earlier.func.id)
    ):
        return None
    args, keywords = later.args, later.keywords
    try:
        later.args, later.keywords = earlier.args, earlier.keywords
        if ast.dump(old.tree) == ast.dump(new.tree):
            return earlier.lineno, earlier.col_offset
    finally:
        later.args, later.keywords = args, keywords
    return None


def call_values(node):
    return node.args + [keyword.value for keyword in node.keywords]


class Semantics:
    def __init__(self, path, revision, ids):
        profile = json.loads(gzip.decompress(path.read_bytes()))
        if profile["revision"] != revision or profile["ids"] != list(ids):
            raise ValueError("semantic profile and coverage map differ")
        self.seen = int(profile["seen"], 16)
        self.operands = profile["operands"]
        self.attributes = profile["attributes"]
        self.registry = profile["registry"]
        self.direct = profile["direct"]
        self.schemas = profile["schemas"]
        self.schema_validations = profile.get("schema_validations", {})
        self.lookups = profile["lookups"]
        imports = profile.get("import_profile", {})
        self.stable_modules = frozenset(imports.get("stable_stdlib_modules", ()))
        self.lazy_modules = imports.get("lazy_module_users", {})
        defaults = profile.get("default_profile", {})
        self.default_seen = int(defaults.get("seen", "0"), 16)
        self.default_direct = int(defaults.get("direct", "0"), 16)
        self.default_functions = defaults.get("functions", {})
        self.factory_defaults = defaults.get("factory_defaults", {})
        self.factory_readers = int(defaults.get("factory_readers", "0"), 16)

    def factory_default_users(self, path, name, changed, executed):
        evidence = self.factory_defaults.get(f"{path}:{name}")
        if evidence is None:
            return None
        readers, fields = evidence
        if any(parameter not in fields for parameter in changed):
            return None
        keep = (
            (executed & ~(self.seen & self.default_seen))
            | int(readers, 16)
            | self.factory_readers
        )
        for parameter in changed:
            keep |= int(fields[parameter], 16)
        return keep

    def default_users(self, path, name, changed, called):
        evidence = self.default_functions.get(f"{path}:{name}")
        if evidence is None:
            return None
        observed, metadata, fields = evidence
        keep = (called & ~(self.default_seen & int(observed, 16))) | int(metadata, 16) | self.default_direct
        for parameter in changed:
            keep |= int(fields.get(parameter, "0"), 16)
        return keep


    def equivalent(self, path, lines, old, new, removed, added):
        edit = compare_edit(old, new, removed, added)
        if edit is None:
            return 0
        line, column, before, after, replacement = edit
        entry = self.operands.get(f"{path}:{line}:{column}")
        if entry is None:
            return 0
        reached, groups = entry
        result = self.seen & ~int(reached, 16) & ~lines.get(line, 0)
        for mask, facts in groups:
            if all(
                compare_outcomes(fact, before, after, replacement) for fact in facts
            ):
                result |= int(mask, 16)
        return result

    def lookup_users(self, path, owner, keys):
        module = path.removesuffix(".py").replace("/", ".")
        table = self.lookups.get(f"{module}:{owner}")
        if table is None or any(table.get(key) == "collection" for key in keys):
            return None
        hits = int(self.direct.get("NUMERIC_ESCAPES", "0"), 16)
        for key in keys:
            hits |= int(table.get(key, "0"), 16)
        return hits

    def registry_users(self, selector, path, old, new, removed, added):
        if not path.startswith("sqlglot/parsers/"):
            return None
        edit = registry_edit(selector, old, new, removed, added)
        if edit is None:
            return None
        line, column = edit
        mask = self.registry.get(f"{path}:{line}:{column}")
        return None if mask is None else int(mask, 16)

    def attribute_users(self, path, members):
        module = path.removesuffix(".py").replace("/", ".")
        result = 0
        for owner, attribute in members:
            mask = self.attributes.get(f"{module}:{owner}:{attribute}")
            if mask is None:
                return None
            result |= int(mask, 16) | int(self.direct.get(attribute, "0"), 16)
        return result

    def schema_flag_users(self, path, owner, keys):
        module = path.removesuffix(".py").replace("/", ".")
        evidence = self.schema_validations.get(f"{module}:{owner}")
        if evidence is None:
            return None
        collection, uncertain, missing = evidence
        if keys.intersection(collection):
            return None
        hits = int(uncertain, 16)
        for field in ("arg_types", "required_args"):
            hits |= int(self.direct.get(field, "0"), 16)
        for key in keys:
            hits |= int(missing.get(key, "0"), 16)
        return hits

    def schema_users(self, path, owner):
        module = path.removesuffix(".py").replace("/", ".")
        mask = self.schemas.get(f"{module}:{owner}")
        return None if mask is None else int(mask, 16)
