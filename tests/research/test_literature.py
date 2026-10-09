import io
import json

import pytest

from modelbuilder import literature


def test_metadata_is_not_claim_proof(monkeypatch):
    payload = {"results": [{"id": "https://openalex.org/W123", "title": "A paper", "doi": None,
                            "authorships": [{"author": {"display_name": "A. Author"}}]}]}
    monkeypatch.setattr(literature, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    result = literature.search_openalex("calibration", 1)
    assert result[0]["citation_metadata"]["claim_verified"] is False
    assert result[0]["source_url"] == "https://openalex.org/W123"


@pytest.mark.parametrize("query,limit", [("", 1), ("x" * 301, 1), ("x", 0), ("x", True), ("x", 11)])
def test_reject_unbounded_requests(query, limit):
    with pytest.raises(ValueError):
        literature.search_openalex(query, limit)


def test_reject_invalid_response(monkeypatch):
    monkeypatch.setattr(literature, "urlopen", lambda *a, **k: io.BytesIO(b'{}'))
    with pytest.raises(literature.LiteratureUnavailable):
        literature.search_openalex("x")
