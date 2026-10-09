# Evaluation evidence and safe model comparison

New executions can publish `metrics.evaluation_records`, keyed by metric name, or
one `metrics.evaluation_record`. Each sealed `modelbuilder.evaluation/v1` record
contains the scalar measurement and exact dataset revision/content/benchmark,
split name/role/sample IDs, evaluator implementation/version/configuration,
preprocessing and seed, metric implementation/version/parameters/aggregation and
direction, model/checkpoint/calibration/reference identities. Optional predictions
are keyed by unique item IDs and must cover the full sample set. The digest seals
all fields; it is content identity, not a signature or a claim that user code is
honest. Trusted custom evaluators remain responsible for accurate observations.

Use `modelbuilder.evaluation.make_evaluation_record` in an ordinary user-owned
harness. There is no requirement to use fixture data or a particular model
framework. Each metric gets its own record because validation loss and test
accuracy may consume different splits. Selection metrics measured at a historical
training step must not be relabeled as measurements of the current checkpoint.

`run.compare`, the native `model.compare` step, and model-version promotion use
the same compatibility predicate. The default allows model and checkpoint
variation. `comparison_contract.allowed_variations` can additionally declare
architecture, objective, calibration or reference as intended treatment
variables. No setting permits changed dataset/benchmark/sample/split, metric
semantics, preprocessing or evaluation protocol to be ranked as the same
experiment. Configuration such as forecast horizon is part of that protocol.
Training-specific model architecture/objective/reference state may be contained
within the model identity; an evaluation judge/reference is a separate treatment.
Fixture records include a digest of the actual frozen training reference inside
model provenance. Model seeds are model provenance, not a fictitious stochastic
evaluation seed for deterministic inference.

A rejected comparison returns `compatible: false`, field-level diagnostics and
null winner fields while retaining the original measurements. Promotion rejects
before metadata/stage mutation and records the successful comparison and evidence
digests when allowed. Multiple native evaluators publishing conflicting records
under one metric name require explicit disambiguation rather than silently using
the last record.

## Historical runs and migration

Do not rewrite old metrics with identities inferred from the current recipe or
current files. Missing historical evidence is `unknown_identity`, not comparable.
Keep its native run/artifact IDs, scalar values, recipe and time intact. Reevaluate
the original pinned checkpoint on a fixed dataset and protocol to create a new
record; link the original run/checkpoint in its provenance. If the checkpoint or
source is unavailable, the historical measurement remains viewable but cannot
automatically win or promote. Existing valid evidence can be validated without
mutating history using `validate_evaluation_record`.

## Paired records and calibration

`comparison_contract.paired: true` requires a finite per-item `value` in each
prediction record. Items are aligned by their IDs; a changed sample set is refused.
Only decomposable mean metrics receive mean paired deltas. RMSE, quantiles and
other nondecomposable aggregates cannot masquerade as means. Reports contain
observed deltas, not unsupported statistical significance claims.

`backend.calibration.fit_temperature` is a bounded local adapter over PyTorch
LBFGS and cross entropy. It records model identity, fit-data/input digest, disjoint
fit/evaluation sample IDs, library version/configuration and fitted temperature.
`apply_temperature` verifies the artifact/model binding and refuses fit/evaluation
leakage. It does not promise test-score improvements. Include its digest in the
calibration treatment, and include the artifact in the user harness's export
manifest assets. Raw-versus-calibrated evaluation requires the same benchmark and
an explicit calibration intervention. No credentials, provider calls or external
weights are needed.
