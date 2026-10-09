# Dependencies and license boundaries

This is an engineering inventory, not a legal opinion or approval to distribute a
finished binary. First-party product code has no owner-selected open-source
license. The extension is private/UNLICENSED. Source availability on GitHub does
not change this. Dependencies retain their own licenses.

## Pinned components

| Component | Pin | Declared license / use |
|---|---|---|
| Code - OSS source | 1.105.0 / 03c265b1adee71ac88f833e065f7bb956b60550a | MIT source, upstream notices retained; not Microsoft VS Code binary licensing |
| Electron | 37.6.0, upstream lock | MIT plus bundled third-party notices; upstream download, no security bypass |
| Node build toolchain | 22.19.0, SHA256 in distribution pin | Node license and bundled notices; local source-build toolchain |
| ZenML | 0.97.0 | Apache-2.0; separate interpreter, public APIs |
| PyTorch CPU | 2.9.0+cpu | BSD-3-Clause plus bundled notices |
| Transformers | 4.57.1 | Apache-2.0 |
| NumPy | 2.2.6 | BSD-3-Clause core; wheel also contains additional notices including BLAS and compiler runtime libraries |
| FastAPI / Pydantic | 0.115.12 / 2.11.4 | MIT |
| Uvicorn / HTTPX | 0.34.2 / 0.28.1 | BSD-3-Clause |
| Official MCP Python SDK | 1.30.0 | MIT |
| jsonschema | 4.23.0 service, 4.26.0 backend | MIT |
| Python / Jupyter / basedpyright / debugpy extensions | Exact versions and SHA256s in distribution manifest | Individually checked MIT artifacts from Open VSX; license copies retained |

The service and backend dependency graphs intentionally differ. Never install
both lockfiles into one environment. `requirements/service.lock` records the
service graph; `requirements/backend.lock` pins the independent runtime with
artifact hashes. `packages/ide_feature/package-lock.json` pins feature build
packages. Upstream Code - OSS lockfiles are not rewritten.

The installed Python distribution/license-file inventories, including hashes of
available license texts, are in `evidence/dependencies/`. Regenerate each with
that environment's interpreter and `scripts/dependency_inventory.py`. Missing
machine-readable metadata is not proof of absent obligations; a release needs
a complete transitive notice/SBOM review, not just this direct-component table.

## Model weights and portable source

The pretrained fixture downloads `prajjwal1/bert-tiny` at immutable revision
`6f75de8b60a9f8a2fdf7b69cbd86d9e64bcb3837`, verifying every declared resource
hash. Weight files are not committed or redistributed here. Model-card license
metadata is not inferred from Transformers' Apache license. Recipes retain
explicit external-dependency license/access metadata, with unresolved rights
recorded rather than invented. The fixture is an execution test, not a statement
that downstream commercial redistribution is cleared.

Portable source bundles contain only explicitly inventoried first-party test
source and dependency specifications. Import is inert. Authority to execute a
trusted local fixture does not grant redistribution rights or approve executing
arbitrary imported Python.

## Explicit exclusions

- No Microsoft Marketplace endpoint or Microsoft-branded binary is shipped.
- Pylance and proprietary Remote Development extensions are not bundled.
- Python extension packs are installed with dependency-pack expansion disabled.
- Codex CLI source licensing does not license the Codex IDE binary. Claude/Codex
  IDE licenses and exact-fork authenticated integration are separate gates.
- No existing private model repository, weights, credentials, dataset or attached
  evaluation archive is copied into the public repository.
- Signed installers, automatic updates, multi-platform packaging and production
  redistribution have not been approved by this development build.

See `distribution/README.md`, `distribution/extension_manifest/extensions.lock.json`
and its retained license texts for primary-source URLs and exact artifacts.
