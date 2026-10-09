# ADR 0003 Shared operations and identity

GUI and standalone MCP call one authenticated application operation service. Canonical recipe JSON has optimistic revisions and SHA256 digests. SQLite stores domain records, idempotency and audit; ZenML owns run/artifact/model-version identity. Research decisions reference actual backend IDs. Import is inert and untrusted execution fails closed. Remote spend and publication are unavailable in this slice.
