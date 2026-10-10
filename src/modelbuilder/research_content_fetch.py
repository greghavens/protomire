"""Isolated fixed-origin Europe PMC OA XML GET. No credentials or user URLs.

Only the public article identifier is sent. The parent and watchdog enforce a
wall deadline. Stdlib XML parsing happens in the parent after bounded retrieval.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 4_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retrieve(identifier, opener=None):
    if not isinstance(identifier, str) or not re.fullmatch(r"PMC[1-9][0-9]{0,9}", identifier):
        return 1, b""
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{identifier}/fullTextXML"
    request = Request(url, headers={"User-Agent": "Modelbuilder/0.1 research-content",
                                   "Accept": "application/xml, text/xml", "Accept-Encoding": "identity"})
    try:
        with (opener or build_opener(ProxyHandler({}), NoRedirect())).open(request, timeout=18) as response:
            if response.geturl() != url:
                return 3, b""
            if response.headers.get_content_type() not in {"application/xml", "text/xml"}:
                return 4, b""
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                return 4, b""
            length = response.headers.get("Content-Length")
            if length and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                return 2, b""
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return 2, b""
            return 0, raw
    except HTTPError as exc:
        return (3 if 300 <= exc.code < 400 else 5 if exc.code in {401, 403} else 1), b""


def main():
    options = json.loads(sys.stdin.buffer.read(4096))
    code, raw = retrieve(options.get("source_identifier"))
    if not code:
        sys.stdout.buffer.write(raw)
    return code


if __name__ == "__main__":
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
