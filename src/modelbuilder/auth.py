"""Explicit, scoped local identities. No anonymous or auto-generated admin token."""
from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping

from .models import Actor, ServiceError


class TokenAuth:
    def __init__(self, tokens: Mapping[str, Actor | dict] | None = None):
        self._tokens: dict[str, Actor] = {}
        for token, identity in (tokens or {}).items():
            if not token:
                raise ValueError("Empty bearer tokens are forbidden")
            if isinstance(identity, Actor):
                actor = identity
            else:
                actor = Actor(id=identity.get("id", identity.get("actor_id", "")),
                              scopes=frozenset(identity.get("scopes", [])),
                              project_ids=identity.get("project_ids"))
            self._tokens[token] = actor

    @classmethod
    def from_environment(cls) -> "TokenAuth":
        tokens = json.loads(os.environ.get("MODELBUILDER_TOKENS_JSON", "{}"))
        if not isinstance(tokens, dict):
            raise ValueError("MODELBUILDER_TOKENS_JSON must map bearer tokens to scoped identities")
        single = os.environ.get("MODELBUILDER_TOKEN")
        if single:
            tokens[single] = {"id": os.environ.get("MODELBUILDER_ACTOR", "local-owner"),
                             "scopes": ["read", "modify", "execute"]}
        return cls(tokens)

    @property
    def configured(self) -> bool:
        return bool(self._tokens)

    def authenticate(self, authorization: str | None) -> Actor:
        if not authorization or not authorization.startswith("Bearer "):
            raise ServiceError("unauthenticated", "A configured bearer token is required", status_code=401)
        candidate = authorization[7:]
        for token, actor in self._tokens.items():
            if hmac.compare_digest(candidate.encode(), token.encode()):
                return actor
        raise ServiceError("unauthenticated", "Bearer token is invalid", status_code=401)
