"""One bounded research-context contract for the workbench, HTTP and MCP."""
from .research_contract import PROJECT, REVISION, STRING

KINDS = ["evidence", "research_text", "claim", "hypothesis", "decision", "research_finding", "research_proposal"]
STATES = ["unreviewed", "supported", "contradicted", "uncertain", "proposed", "testing", "accepted",
          "rejected", "inconclusive", "abandoned", "approved", "applied", "reviewed", "dismissed", "superseded"]
SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}


def operation_specs(spec):
    return [
        spec("research.context.capabilities", "read",
             "Inspect local lexical context retrieval and adapter/resource contracts; no model load or network call.",
             PROJECT, ("project_id",)),
        spec("research.context.build", "read",
             "Assemble bounded project research as inert evidence with exact revisions, hashes, citation spans and review states. Local lexical ranking is not embeddings or graph RAG.",
             {**PROJECT, "query": {"type": "string", "minLength": 1, "maxLength": 300},
              "retriever_id": {"type": "string", "minLength": 1, "maxLength": 120},
              "source_kinds": {"type": "array", "items": {"enum": KINDS}, "minItems": 1, "uniqueItems": True},
              "source_ids": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 50, "uniqueItems": True},
              "exclude_source_ids": {"type": "array", "items": STRING, "maxItems": 50, "uniqueItems": True},
              "use_scope": {"enum": ["local_review", "external_harness"], "default": "local_review"},
              "exclude_unresolved_rights": {"type": "boolean", "default": False},
              "states": {"type": "array", "items": {"enum": STATES}, "minItems": 1, "uniqueItems": True},
              "include_history": {"type": "boolean", "default": False},
              "expected_corpus_sha256": SHA256,
              "max_items": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
              "max_bytes": {"type": "integer", "minimum": 2048, "maximum": 131072, "default": 24000},
              "passage_chars": {"type": "integer", "minimum": 80, "maximum": 4000, "default": 1200},
              "max_sources": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 256},
              "max_scan_bytes": {"type": "integer", "minimum": 1024, "maximum": 8000000, "default": 2000000}},
             ("project_id", "query")),
        spec("research.context.inspect", "read",
             "Reopen an exact historical research passage and verify its record hash; source instructions remain inert evidence.",
             {**PROJECT, "source_kind": {"enum": KINDS}, "source_id": STRING, "revision": REVISION,
              "record_sha256": SHA256, "field": STRING,
              "start": {"type": "integer", "minimum": 0},
              "length": {"type": "integer", "minimum": 1, "maximum": 4000, "default": 1200}},
             ("project_id", "source_kind", "source_id", "revision", "record_sha256", "field", "start")),
    ]
