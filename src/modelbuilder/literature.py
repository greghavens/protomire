"""Bounded public OpenAlex discovery; retrieval is not scientific verification."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request


class LiteratureUnavailable(RuntimeError):
    """A public source could not be queried; no fabricated fallback records."""


FETCH_TIMEOUT_SECONDS = 20


def urlopen(request, timeout=FETCH_TIMEOUT_SECONDS):
    """Return bounded bytes; a subprocess deadline includes DNS and slow reads.

    The name stays injectable for controlled provider fixtures. Production
    never runs provider I/O in a Python thread that cannot be cancelled.
    """
    query = parse_qs(urlsplit(request.full_url).query)
    options = {"query": query["search"][0], "limit": int(query["per-page"][0])}
    environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
    try:
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("literature_fetch.py"))],
                                input=json.dumps(options).encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=min(timeout, FETCH_TIMEOUT_SECONDS),
                                env=environment, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise LiteratureUnavailable("OpenAlex request exceeded its bounded fetch window or could not start") from exc
    if result.returncode or len(result.stdout) > 2_000_000:
        raise LiteratureUnavailable("OpenAlex metadata retrieval failed")
    return io.BytesIO(result.stdout)


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
