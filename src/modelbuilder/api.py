"""Thin HTTP transport. All resource behavior lives in OperationService.invoke."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi

from .auth import TokenAuth
from .models import Actor, ModelProject, Evidence, Claim, Hypothesis, Decision, OPERATIONS, ServiceError
from .service import OperationService


MAX_BODY_BYTES = 16 * 1024 * 1024


class BoundedRequestBody:
    """Bound actual streamed bytes before JSON parsing, including chunked requests."""

    def __init__(self, app, limit: int = MAX_BODY_BYTES):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = -1
            if length < 0 or length > self.limit:
                return await self._reject(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                return await self._reject(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body, supplied = b"".join(chunks), False

        async def bounded_receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, bounded_receive, send)

    async def _reject(self, scope, receive, send):
        response = JSONResponse(ServiceError("payload_too_large", "Request body exceeds the configured limit",
                                             status_code=413).as_dict(), status_code=413)
        return await response(scope, receive, send)


def create_app(service: OperationService | None = None, tokens: dict | TokenAuth | None = None) -> FastAPI:
    if service is None:
        db_path = os.environ.get("MODELBUILDER_DB_PATH", ".modelbuilder/modelbuilder.sqlite3")
        backend = None
        if os.environ.get("MODELBUILDER_BACKEND", "zenml") == "zenml":
            try:
                from .backend import ZenMLBackend
            except ImportError:
                pass
            else:
                backend = ZenMLBackend(data_dir=Path(db_path).parent)
        service = OperationService(db_path=db_path, backend=backend,
                                   source_root=os.environ.get("MODELBUILDER_SOURCE_ROOT"))
    auth = tokens if isinstance(tokens, TokenAuth) else (TokenAuth(tokens) if tokens is not None else TokenAuth.from_environment())
    app = FastAPI(title="ModelBuilder shared operation service", version="0.1.0",
                  description="GUI and native MCP use the same revisioned operation catalog.")
    app.state.service = service
    app.state.auth = auth

    app.add_middleware(BoundedRequestBody)

    @app.exception_handler(ServiceError)
    async def operation_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(exc.as_dict(), status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"error": {"code": "validation_error", "message": "Invalid HTTP request",
                                       "details": {"errors": [{"path": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]}}}, status_code=400)

    def identity(authorization: str | None = Header(default=None)) -> Actor:
        return auth.authenticate(authorization)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "modelbuilder", "version": "0.1.0", "authentication_configured": auth.configured}

    @app.get("/v1/catalog")
    def catalog(actor: Actor = Depends(identity)):
        return {"schema_version": "0", "operations": service.catalog(actor)}

    @app.post("/v1/operations/{operation}")
    def invoke(operation: str, payload: dict = Body(...), actor: Actor = Depends(identity)):
        return service.invoke(operation, payload, actor)

    @app.get("/v1/projects")
    def projects(actor: Actor = Depends(identity)):
        return service.invoke("project.list", {}, actor)

    @app.post("/v1/projects")
    def create_project(payload: dict = Body(...), actor: Actor = Depends(identity)):
        return service.invoke("project.create", payload, actor)

    @app.get("/v1/projects/{project_id}")
    def project(project_id: str, actor: Actor = Depends(identity)):
        return service.invoke("project.get", {"project_id": project_id}, actor)

    @app.patch("/v1/projects/{project_id}")
    def update_project(project_id: str, payload: dict = Body(...), actor: Actor = Depends(identity)):
        _match_identity(payload, "project_id", project_id)
        return service.invoke("project.update", {**payload, "project_id": project_id}, actor)

    @app.get("/v1/projects/{project_id}/history")
    def history(project_id: str, actor: Actor = Depends(identity)):
        return service.invoke("project.history", {"project_id": project_id}, actor)

    @app.get("/v1/projects/{project_id}/recipes")
    def recipes(project_id: str, actor: Actor = Depends(identity)):
        return service.invoke("recipe.list", {"project_id": project_id}, actor)

    @app.post("/v1/projects/{project_id}/recipes")
    def create_recipe(project_id: str, payload: dict = Body(...), actor: Actor = Depends(identity)):
        _match_identity(payload, "project_id", project_id)
        return service.invoke("recipe.create", {**payload, "project_id": project_id}, actor)

    @app.get("/v1/projects/{project_id}/recipes/{recipe_id}")
    def recipe(project_id: str, recipe_id: str, revision: int | None = Query(default=None), actor: Actor = Depends(identity)):
        payload = {"project_id": project_id, "recipe_id": recipe_id}
        if revision is not None:
            payload["revision"] = revision
        return service.invoke("recipe.get", payload, actor)

    @app.put("/v1/projects/{project_id}/recipes/{recipe_id}")
    def update_recipe(project_id: str, recipe_id: str, payload: dict = Body(...), actor: Actor = Depends(identity)):
        _match_identity(payload, "project_id", project_id)
        _match_identity(payload, "recipe_id", recipe_id)
        return service.invoke("recipe.update", {**payload, "project_id": project_id, "recipe_id": recipe_id}, actor)

    @app.get("/v1/projects/{project_id}/runs")
    def runs(project_id: str, actor: Actor = Depends(identity)):
        return service.invoke("run.list", {"project_id": project_id}, actor)

    @app.get("/v1/runs/{backend_run_id}")
    def run(backend_run_id: str, project_id: str, actor: Actor = Depends(identity)):
        return service.invoke("run.status", {"project_id": project_id, "backend_run_id": backend_run_id}, actor)

    @app.get("/v1/operations/{operation_id}")
    def operation(operation_id: str, project_id: str, actor: Actor = Depends(identity)):
        return service.invoke("operation.get", {"project_id": project_id, "operation_id": operation_id}, actor)

    def shared_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["BearerAuth"] = {"type": "http", "scheme": "bearer"}
        definitions = components.setdefault("schemas", {})
        for model in (ModelProject, Evidence, Claim, Hypothesis, Decision):
            definitions[model.__name__] = model.model_json_schema()
        definitions["OperationError"] = {"type": "object", "required": ["error"], "properties": {"error": {
            "type": "object", "required": ["code", "message", "details"], "properties": {
                "code": {"type": "string"}, "message": {"type": "string"}, "details": {}}}}}
        # Concrete documented paths use the same single runtime dispatcher and contract.
        for name, operation in OPERATIONS.items():
            responses = {"200": {"description": "Structured authoritative operation result", "content": {
                "application/json": {"schema": {"type": "object"}}}}}
            for code, description in [(400, "Invalid request"), (401, "Unauthenticated"), (403, "Forbidden"),
                                      (404, "Not found"), (409, "Revision or idempotency conflict"),
                                      (422, "Execution precondition failed"), (503, "Capability unavailable")]:
                responses[str(code)] = {"description": description, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/OperationError"}}}}
            schema["paths"][f"/v1/operations/{name}"] = {"post": {
                "operationId": name.replace(".", "_"), "summary": operation.description,
                "x-required-scope": operation.scope, "x-required-scopes": list(operation.required_scopes), "security": [{"BearerAuth": []}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": operation.input_schema}}},
                "responses": responses}}
        for path, methods in schema["paths"].items():
            if path.startswith("/v1/"):
                for definition in methods.values():
                    if isinstance(definition, dict):
                        definition.setdefault("security", [{"BearerAuth": []}])
        app.openapi_schema = schema
        return schema

    app.openapi = shared_openapi
    return app


def _match_identity(payload, key, expected):
    if key in payload and payload[key] != expected:
        raise ServiceError("validation_error", "URL and body resource identities differ", {"field": key})


def main():
    import uvicorn
    uvicorn.run("modelbuilder.api:create_app", factory=True,
                host=os.environ.get("MODELBUILDER_HOST", "127.0.0.1"),
                port=int(os.environ.get("MODELBUILDER_PORT", "8765")))


if __name__ == "__main__":
    main()
