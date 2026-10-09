"""Explicit, inert source-closure authoring through the shared operation catalog."""
from __future__ import annotations


def operation_specs(spec):
    fields = {
        "project_id": {"type": "string", "minLength": 1},
        "recipe_id": {"type": "string", "minLength": 1},
        "expected_revision": {"type": "integer", "minimum": 1},
        "expected_project_revision": {"type": "integer", "minimum": 1},
        "source_repository_id": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
        "commit": {"type": "string", "pattern": r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"},
        "paths": {"type": "array", "minItems": 1, "maxItems": 1024, "uniqueItems": True,
                  "items": {"type": "string", "minLength": 1, "maxLength": 512}},
    }
    return [
        spec("recipe.source.pin.preview", "read", "Review explicit committed source files from the model's approved checkout without changing its recipe or executing code.",
             fields, tuple(fields)),
        spec("recipe.source.pin", "modify", "Apply a reviewed source closure as the next immutable revision of this model's one recipe; never commit, install or execute source.",
             {**fields, "review_digest": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
              "confirm_pin": {"const": True}}, (*tuple(fields), "review_digest", "confirm_pin"), True, ("read",)),
    ]
