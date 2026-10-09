# Implementation decisions

- Keep Python service and backend environments separate. The service has a frozen dependency set and official MCP1.30.0. ZenML0.97.0 has its own dependency profile; no private SDK patches.
- Use a single catalog of operation names/input schemas/scopes for HTTP and MCP. Mutations require idempotency keys, existing-resource edits use expected revisions, and denied mutations are audited.
- Use public OpenAlex metadata discovery with a fixed endpoint and bounded query/results. Add evidence explicitly; do not treat retrieval as verification or automatically fetch arbitrary URLs.
- Keep baseline evaluation private outside the public repository. Store its archive checksum and scoped findings, not private third-party project details.
- Use modelbuilder as a provisional development name, not a branding decision.
- Local Git commits capture coherent increments. GitHub connector publication may produce distinct remote commit identities; published remote SHAs are verified rather than assumed to equal local SHAs.
- No open-source license is selected on the owner's behalf. Third-party notices and licenses remain applicable; choose the product's own license before a public release.
