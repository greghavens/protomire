# Existing-repository mapping

The built-in layout is version 1. Use it as an index of the existing repository; do not force destructive moves:

- `modelbuilder.project.json`: one existing/new project identity and its canonical recipe identity, created by the native project flow.
- `recipe/current.modelrecipe.json`, `recipe/revisions/`: projections of service-owned canonical revisions. They are not a second authority. Inspect service revisions and native projection drift warnings before editing.
- `src/`, `tests/`: preferred locations for new wrappers/tests. Existing package paths remain valid through source binding.
- `research/`, `research/evidence/`: Markdown notes and small evidence/provenance references. Register documents/claims through available operations; retain URLs, revisions, citations and rights status.
- `manifests/models/`, `manifests/datasets/`: immutable artifact references validated against the project's bundled artifact-reference schema. Use exact provider revisions, paths, byte sizes, hashes and licenses; retain local roots for data not uploaded.
- `.modelbuilder/layout.v1.json`: managed layout receipt. Do not fabricate it or edit managed hashes to conceal conflicts.
- Large `data/`, `models/`, `checkpoints/`, `runs/`, `logs/` and caches stay outside ordinary Git by default. Existing tracked blobs are not removed by adding ignore rules; report them separately and seek the appropriate migration authorization.

Preferred formats are JSON manifests/recipes, Markdown notes, Parquet for tabular data and JSONL for simple streams, and safetensors for tensor weights. Existing valid image/audio/video/custom datasets and framework-native resume checkpoints retain their formats. Never convert or discard optimizer state simply to match a preferred extension.

Map these independently:

1. Model architecture, base/initialization and licensing.
2. Native train/eval/export entrypoints, parameters and locked environments.
3. Data sources, splits, filtering/sampling, overlap rules and functionality families.
4. Training-stage lineage, exact checkpoint selection and resume capability.
5. Evaluation protocol, full held-out cohort, per-family support and quality/regression requirements.
6. Research evidence and unresolved decisions.

New adapters must emit the measured, identity-bound evidence their selected guardrails require. A legacy metric or checkpoint can be preserved without pretending it meets the current evidence contract. Keep unknown historic runs visible as legacy/unverified, not fabricated successful native runs.
