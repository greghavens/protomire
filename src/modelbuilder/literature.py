"""Bounded public OpenAlex discovery; retrieval is not scientific verification."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class LiteratureUnavailable(RuntimeError):
    """A public source could not be queried; no fabricated fallback records."""


def search_openalex(query: str, limit: int = 5) -> list[dict]:
    """Retrieve metadata from the fixed public endpoint without following user URLs."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
        raise ValueError("query must contain 1–300 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("limit must be an integer from 1 to 10")
    url = "https://api.openalex.org/works?" + urlencode(
        {"search": query.strip(), "per-page": limit,
         "select": "id,doi,title,publication_date,authorships,primary_location"}
    )
    request = Request(url, headers={"User-Agent": "Modelbuilder/0.1 literature-metadata", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise LiteratureUnavailable("OpenAlex response exceeded metadata limit")
        body = json.loads(raw)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LiteratureUnavailable(f"OpenAlex metadata unavailable: {type(exc).__name__}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise LiteratureUnavailable("OpenAlex returned an unexpected response schema")
    results = []
    for item in body["results"][:limit]:
        if not isinstance(item, dict) or not str(item.get("id", "")).startswith("https://openalex.org/W"):
            continue
        results.append({
            "source_url": item.get("doi") or item["id"], "source_identifier": item["id"],
            "title": item.get("title") or "Untitled", "publication_date": item.get("publication_date"),
            "authors": [a.get("author", {}).get("display_name", "") for a in item.get("authorships", [])],
            "citation_metadata": {"provider": "OpenAlex", "doi": item.get("doi"),
                                  "claim_verified": False, "metadata_only": True},
        })
    return results
