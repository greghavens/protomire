"""Bounded subprocess for the fixed public metadata endpoint (stdlib only).

Invoked by literature.py with an isolated interpreter and a sanitized environment.
No database, credentials, provider inference or project source files are read.
"""
from __future__ import annotations

import json
import os
import threading
import sys
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    options = json.loads(sys.stdin.buffer.read(4096))
    query, limit = options["query"], options["limit"]
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
        return 1
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        return 1
    url = "https://api.openalex.org/works?" + urlencode({"search": query, "per-page": limit,
        "select": "id,doi,title,publication_date,authorships,primary_location"})
    request = Request(url, headers={"User-Agent": "Modelbuilder/0.1 literature-metadata", "Accept": "application/json"})
    # No credential-bearing ambient proxy and no redirect to another recipient.
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=18) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        return 1
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    # Keep a wall deadline even if the parent is killed before it can reap us.
    watchdog = threading.Timer(20, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    try:
        code = main()
    except Exception:
        code = 1
    finally:
        watchdog.cancel()
    raise SystemExit(code)
