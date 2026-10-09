"""Versioned operation contracts for the inert, private recipe registry.

Publication always requires a review of the exact audience, version and content.
Import remains a separate reviewed File-menu action, never a workspace selector.
"""
from __future__ import annotations

STRING = {"type": "string", "minLength": 1, "maxLength": 200}
DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
REVISION = {"type": "integer", "minimum": 1}
REGISTRY = {"registry_id": STRING}
PUBLICATION = {**REGISTRY, "publication_id": DIGEST}
METADATA = {
    "type": "object", "additionalProperties": False, "required": ["title", "description", "tags"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "description": {"type": "string", "maxLength": 16000},
        "tags": {"type": "array", "maxItems": 20, "uniqueItems": True,
                 "items": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,39}$"}},
    },
}
PUBLISH_FIELDS = {
    **REGISTRY, "project_id": STRING, "recipe_id": STRING, "expected_revision": REVISION,
    "package_name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,99}$"},
    "package_version": {"type": "string", "pattern": r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*)?$",
                        "maxLength": 100},
    "visibility": {"const": "private"}, "metadata": METADATA,
}
PUBLISH_REQUIRED = tuple(PUBLISH_FIELDS)
TARGET = {
    "oneOf": [
        {"type": "object", "additionalProperties": False,
         "required": ["mode", "project_id", "expected_revision"],
         "properties": {"mode": {"const": "existing_project"}, "project_id": STRING,
                        "expected_revision": REVISION}},
        {"type": "object", "additionalProperties": False,
         "required": ["mode", "slug", "model_name"],
         "properties": {"mode": {"const": "new_project"},
                        "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,79}$"},
                        "model_name": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": {"type": "string", "maxLength": 16000}}},
    ],
}
FORK_FIELDS = {**PUBLICATION, "target": TARGET}
CONFIRMATION = {"review_digest": DIGEST, "acknowledge_unresolved_dependencies": {"type": "boolean"}}
REGISTRY_OPERATIONS = frozenset({
    "recipe.registry.list", "recipe.registry.get", "recipe.registry.imported_bundle", "recipe.publish.preview", "recipe.publish",
    "recipe.discover", "recipe.fork.preview", "recipe.fork",
})


def operation_specs(spec):
    """Use the sole application's OperationSpec factory, HTTP/MCP and auth path."""
    return [
        spec("recipe.registry.list", "read", "List actual configured recipe-only registry capabilities; public publication is unavailable."),
        spec("recipe.discover", "read", "Browse authorized immutable private recipe metadata, outside the current workspace.",
             {**REGISTRY, "query": {"type": "string", "maxLength": 300},
              "tags": {"type": "array", "maxItems": 20, "items": STRING, "uniqueItems": True},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100},
              "offset": {"type": "integer", "minimum": 0, "maximum": 100000}}, ("registry_id",)),
        spec("recipe.registry.get", "read", "Inspect a verified publication, dependencies and exact inert bundle JSON; never install or execute.",
             PUBLICATION, tuple(PUBLICATION)),
        spec("recipe.registry.imported_bundle", "read", "Read the original verified source bundle retained by a specific imported recipe revision; no source files are written.",
             {"project_id": STRING, "recipe_id": STRING, "revision": REVISION},
             ("project_id", "recipe_id", "revision")),
        spec("recipe.publish.preview", "read", "Review exact private destination, audience, immutable version, rights, bytes and unresolved dependencies.",
             PUBLISH_FIELDS, PUBLISH_REQUIRED),
        spec("recipe.publish", "publish", "Explicitly publish the reviewed recipe-only version to this service's private SQLite registry; no public upload.",
             {**PUBLISH_FIELDS, **CONFIRMATION, "confirm_publish": {"const": True}},
             (*PUBLISH_REQUIRED, "review_digest", "confirm_publish"), True, ("read",)),
        spec("recipe.fork.preview", "read", "Review an inert import into one existing stable recipe revision or a new project before opening its workspace.",
             FORK_FIELDS, tuple(FORK_FIELDS)),
        spec("recipe.fork", "modify", "Import the exact reviewed private publication into one recipe/new project; trust and execution are separate.",
             {**FORK_FIELDS, **CONFIRMATION, "confirm_import": {"const": True}},
             (*tuple(FORK_FIELDS), "review_digest", "confirm_import"), True, ("read",)),
    ]
