"""Explicit, scoped local identities. No anonymous or auto-generated admin token."""
from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping

from .models import Actor, OPERATIONS, ServiceError


class TokenAuth:
    def __init__(self, tokens: Mapping[str, Actor | dict] | None = None):
        self._tokens: dict[str, Actor] = {}
        for token, identity in (tokens or {}).items():
            if not token:
                raise ValueError("Empty bearer tokens are forbidden")
            if isinstance(identity, Actor):
                # Each token has its own immutable grant identity, even when a
                # caller reuses the same Actor value for several token entries.
                actor = Actor(id=identity.id, scopes=identity.scopes, project_ids=identity.project_ids)
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

    def authorize_research(self, actor_id: str, project_id: str) -> bool:
        """Resolve current grants each time; never persist or replay bearer tokens.

        Schedules belong to the actor, not one token. Removing all of an actor's
        modifying grants (or project access) revokes its scheduled work.
        """
        return any(actor.id == actor_id and "modify" in actor.scopes
                   and (actor.project_ids is None or project_id in actor.project_ids)
                   for actor in tuple(self._tokens.values()))

    def authorize_operation(self, operation: str, actor: Actor, project_id: str) -> bool:
        """Recheck the original authenticated grant after unlocked evidence I/O.

        A different token for the same actor cannot revive a revoked request.
        No bearer token is retained in service requests or evidence handles.
        """
        return any(current is actor and set(OPERATIONS[operation].required_scopes).issubset(current.scopes)
                   and (current.project_ids is None or project_id in current.project_ids)
                   for current in tuple(self._tokens.values()))

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
