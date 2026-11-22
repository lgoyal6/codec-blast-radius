"""Refine coverage with observations from the unmodified baseline."""

import ast
import gzip
import json
from functools import cache

from semantic_edits import (
    COMPARISONS, ORDERING, UNCHANGED, argument_defaults, compare_edit, copy_toggle,
)


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


def factory_calls(tree, name):
    return sorted(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )


def effective_defaults(arguments, call, defaults):
    positional = arguments.posonlyargs + arguments.args
    if any(arg.arg in defaults for arg in positional[:len(call.args)]):
        return None
    effective = dict(defaults)
    for keyword in call.keywords:
        if keyword.arg is None:
            return None
        if keyword.arg in effective:
            if (
                not isinstance(keyword.value, ast.Constant)
                or type(keyword.value.value) is not type(defaults[keyword.arg])
            ):
                return None
            effective[keyword.arg] = keyword.value.value
    return effective


def composed_factory_calls(before, after, name, known_parameters):
    candidates = [
        [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
        for tree in (before, after)
    ]
    if any(len(nodes) != 1 for nodes in candidates):
        return None
    (earlier,), (later,) = candidates
    old_defaults = argument_defaults(earlier.args)
    new_defaults = argument_defaults(later.args)
    if old_defaults.keys() != new_defaults.keys():
        return None
    changed = {key for key, value in old_defaults.items() if new_defaults[key] != value}
    if (
        not changed or not changed <= known_parameters
        or any(arg.arg in changed for arg in earlier.args.posonlyargs)
        or any(type(value) is not type(new_defaults[key]) for key, value in old_defaults.items())
    ):
        return None
    calls_before = factory_calls(before, name)
    calls_after = factory_calls(after, name)
    if not calls_before or len(calls_before) != len(calls_after):
        return None
    updates = []
    for old_call, new_call in zip(calls_before, calls_after):
        old_values = effective_defaults(earlier.args, old_call, {key: old_defaults[key] for key in changed})
        new_values = effective_defaults(later.args, new_call, {key: new_defaults[key] for key in changed})
        if old_values is None or new_values is None:
            return None
        updates.append((old_call, old_values != new_values))
    old_kw = [call.keywords for call in calls_before]
    new_kw = [call.keywords for call in calls_after]
    original_defaults, original_kw_defaults = later.args.defaults, later.args.kw_defaults
    try:
        later.args.defaults, later.args.kw_defaults = earlier.args.defaults, earlier.args.kw_defaults
        for call in calls_before + calls_after:
            call.keywords = [kw for kw in call.keywords if kw.arg not in changed]
        if ast.dump(before) == ast.dump(after):
            return updates
    finally:
        later.args.defaults, later.args.kw_defaults = original_defaults, original_kw_defaults
        for call, keywords in zip(calls_before + calls_after, old_kw + new_kw):
            call.keywords = keywords
    return None


class Semantics:
    def __init__(self, path, revision, ids):
        profile = json.loads(gzip.decompress(path.read_bytes()))
        if profile["revision"] != revision or profile["ids"] != list(ids):
            raise ValueError("semantic profile and coverage map differ")
        self.seen = int(profile["seen"], 16)
        self.operands = profile["operands"]
        self.attributes = profile["attributes"]
        self.enum_fields = profile.get("enum_fields", {})
        self.literal_containers = frozenset(profile.get("runtime_literal_containers", ()))
        self.configuration_keys = profile.get("configuration_keys", {})
        self.dictionary_membership = profile.get("dictionary_membership", {})
        self.parser_dispatch_keys = profile.get("parser_dispatch_keys", {})
        self.token_dispatch_keys = profile.get("token_dispatch_keys", {})
        self.class_namespace_users = profile.get("class_namespace_users", {})
        self.token_dispatch_functions = frozenset(profile.get("token_dispatch_functions", ()))
        self.registry = profile["registry"]
        additions = profile.get("added_module_profile", {})
        self.added_modules = (
            int(additions.get("seen", "0"), 16),
            int(additions.get("enumerated", "0"), 16),
            frozenset(additions.get("looked_up", ())),
        )
        handlers = profile.get("handler_profile", {})
        self.handler_seen = int(handlers.get("seen", "0"), 16)
        self.handlers = {
            path: {site: int(mask, 16) for site, mask in sites.items()}
            for path, sites in handlers.get("sites", {}).items()
        }
        self.fresh_copies = profile.get("fresh_copies", {})
        self.direct = profile["direct"]
        self.schemas = profile["schemas"]
        self.schema_validations = profile.get("schema_validations", {})
        self.lookups = profile["lookups"]
        imports = profile.get("import_profile", {})
        self.stable_modules = frozenset(imports.get("stable_stdlib_modules", ()))
        self.stable_collection_modules = frozenset(imports.get("stable_collection_modules", ()))
        self.lazy_modules = imports.get("lazy_module_users", {})
        self.warm_imports = {
            path: frozenset(names) for path, names in imports.get("module_warm_imports", {}).items()
        }
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

    def composed_factory_users(self, path, old, new, executed):
        for qualified, (readers, fields) in self.factory_defaults.items():
            file, name = qualified.rsplit(":", 1)
            if file != path:
                continue
            calls = composed_factory_calls(old.tree, new.tree, name, fields.keys())
            if calls is None:
                continue
            hits = (
                (executed & ~(self.seen & self.default_seen))
                | int(readers, 16)
                | self.factory_readers
            )
            for call, changed in calls:
                mask = self.registry.get(f"{path}:{call.lineno}:{call.col_offset}")
                if mask is None:
                    return None
                if changed:
                    hits |= int(mask, 16)
            return hits
        return None

    def default_users(self, path, name, changed, called):
        evidence = self.default_functions.get(f"{path}:{name}")
        if evidence is None:
            return None
        observed, metadata, fields = evidence
        keep = (called & ~(self.default_seen & int(observed, 16))) | int(metadata, 16) | self.default_direct
        for parameter in changed:
            keep |= int(fields.get(parameter, "0"), 16)
        return keep


    def fresh_copy_users(self, path, old, new, removed, added):
        sites = self.fresh_copies.get(path)
        if not sites:
            return 0
        edit = copy_toggle(old, new, removed, added)
        if edit is None:
            return 0
        line, column = edit
        return int(sites.get(f"{line}:{column}", "0"), 16)

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
