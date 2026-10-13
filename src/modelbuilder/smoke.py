"""Declared-budget smoke drafts; no guessed hyperparameters or execution overrides."""
from copy import deepcopy
from jsonschema import Draft202012Validator
from .evaluation import digest

TEXT = {"type": "string", "minLength": 1}
BUDGET_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"schema": {"const": "modelbuilder.smoke-budget/v1"},
        "limits": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object", "additionalProperties": False,
            "properties": {"id": TEXT, "unit": TEXT, "parameter_path": {"type": "array", "minItems": 1, "maxItems": 16, "items": TEXT},
                           "semantics": {"const": "absolute_integer_cap"}, "minimum": {"type": "integer", "minimum": 1}},
            "required": ["id", "unit", "parameter_path", "semantics", "minimum"]}}},
    "required": ["schema", "limits"]}
LIMITS_SCHEMA = {"type": "array", "minItems": 1, "maxItems": 1000, "items": {"type": "object", "additionalProperties": False,
    "properties": {"node_id": TEXT, "limit_id": TEXT, "unit": TEXT, "maximum": {"type": "integer", "minimum": 1}},
    "required": ["node_id", "limit_id", "unit", "maximum"]}}
SMOKE_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"schema": {"const": "modelbuilder.smoke/v1"}, "parent_recipe_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "parent_recipe_revision": {"type": "integer", "minimum": 1}, "limits": LIMITS_SCHEMA,
        "budget_contract_digests": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}},
    "required": ["schema", "parent_recipe_digest", "parent_recipe_revision", "limits", "budget_contract_digests"]}


def plan(recipe, limits):
    Draft202012Validator(LIMITS_SCHEMA).validate(limits)
    if len({(item["node_id"], item["limit_id"]) for item in limits}) != len(limits):
        raise ValueError("Smoke budget limits must be distinct")
    draft = deepcopy(recipe)
    draft.pop("digest", None)
    nodes = {n["id"]: n for n in draft["nodes"]}
    changed, contracts, diagnostics = [], {}, []
    for requested in limits:
        node = nodes.get(requested["node_id"])
        budget = (node or {}).get("component", {}).get("capabilities", {}).get("smoke_budget")
        if budget is None:
            diagnostics.append({"node_id": requested["node_id"], "code": "smoke_budget_unsupported", "message": "Component has no declared budget contract; use a reviewed source adapter, not inferred parameter overrides"})
            continue
        Draft202012Validator(BUDGET_SCHEMA).validate(budget)
        matches = [item for item in budget["limits"] if item["id"] == requested["limit_id"] and item["unit"] == requested["unit"]]
        if len(matches) != 1:
            raise ValueError("Smoke budget limit/unit differs from the component declaration")
        declaration = matches[0]
        if requested["maximum"] < declaration["minimum"]:
            raise ValueError("Smoke budget falls below the component's declared minimum")
        target = node["parameters"]
        for part in declaration["parameter_path"][:-1]:
            if not isinstance(target, dict) or part not in target:
                raise ValueError("Smoke budget parameter path is absent")
            target = target[part]
        leaf = declaration["parameter_path"][-1]
        if not isinstance(target, dict) or type(target.get(leaf)) is not int or target[leaf] < declaration["minimum"]:
            raise ValueError("Smoke budget must cap an existing valid integer parameter")
        before = target[leaf]
        target[leaf] = min(before, requested["maximum"])
        Draft202012Validator(node["component"]["parameter_schema"]).validate(node["parameters"])
        changed.append({"node_id": node["id"], "parameter_path": declaration["parameter_path"], "unit": declaration["unit"], "before": before, "after": target[leaf]})
        contracts[node["id"]] = digest(budget)
    draft["execution"] = {**draft.get("execution", {}), "purpose": "smoke", "smoke": {
        "schema": "modelbuilder.smoke/v1", "parent_recipe_digest": recipe["digest"], "parent_recipe_revision": recipe["revision"],
        "limits": deepcopy(limits), "budget_contract_digests": contracts}}
    return {"supported": not diagnostics, "production_eligible": False, "execution_allowed": False,
            "recipe_id": recipe["recipe_id"], "source_recipe_revision": recipe["revision"], "source_recipe_digest": recipe["digest"],
            "recipe": draft if not diagnostics else None, "changes": changed, "diagnostics": diagnostics}


def operation_specs(spec):
    fields = {"project_id": TEXT, "recipe_id": TEXT, "expected_revision": {"type": "integer", "minimum": 1}, "limits": LIMITS_SCHEMA}
    return [spec("run.smoke.plan", "read", "Prepare a non-production smoke recipe draft using only declared component budget semantics; does not execute or save.", fields, tuple(fields)),
            spec("recipe.smoke.prepare", "modify", "Save the declared-budget smoke draft as the next immutable revision of this same recipe; execution remains run.submit.", fields, tuple(fields), True)]


def dispatch(service, operation, payload):
    recipe = service._current_recipe(payload["project_id"], payload["recipe_id"])
    service._revision(recipe, payload["expected_revision"])
    result = plan(recipe, payload["limits"])
    if operation == "recipe.smoke.prepare":
        if not result["supported"]:
            from .models import ServiceError
            raise ServiceError("smoke_budget_unsupported", "Smoke preparation requires declared budget semantics for every requested node", result)
        result["recipe"] = service._recipe("update", {**payload, "recipe": result["recipe"]})
    return result
