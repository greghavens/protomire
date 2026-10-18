# Functionality-family coverage contract

`modelbuilder.family_coverage` separates three questions:

1. Does the **final materialized dataset** contain each required family after sampling, filtering, deduplication, and limits?
2. Has the **specific checkpoint** actually trained on enough presentations of each family?
3. Does a **per-family held-out evaluation** have adequate support and meet its selected thresholds?

Passing one does not imply passing another. The generic contract contains no application-specific taxonomy. Research or the user supplies the catalog, mapping version, provenance references, and quotas. Nothing infers a family from prose, task order, or aggregate scores.

## Policy

```json
{
  "schema_version": "modelbuilder.family-policy.v1",
  "mapping": {"version": "reviewed-taxonomy-v1", "field": "functionality_family"},
  "research_sources": ["https://example.org/research-description"],
  "families": [
    {
      "id": "parse",
      "min_rows": 20,
      "min_exposure": 40,
      "min_eval_support": 10,
      "metrics": {"cross_entropy": {"max": 0.8, "max_regression": 0.05, "direction": "lower"}}
    }
  ]
}
```

The mapping reads one exact, top-level scalar string field. Missing, non-string, multi-label, and unknown labels are rejected. The mapping version is a user-owned semantic version; changing the mapping or catalog changes the policy identity. `research_sources` are optional references, not evidence that research has been validated.

All catalog entries are assessed. Set row and exposure minima to at least one to require representation and exposure. A zero minimum deliberately waives that phase's quota; it does not silently create a positive requirement. Even with zero minimum, exposure assessment needs an explicit measured zero rather than missing evidence. Evaluation support must always be at least one. Metrics are optional: a policy without metric rules establishes support only, not model quality.

Bounds: at most 256 families, 256 metric rules per family, and 256 source references; labels, mapping fields/versions and metric names are at most 200 characters; references at most 2048. Counts are actual nonnegative integers, never booleans. All numeric metrics and thresholds must be finite. Duplicate family IDs and unknown policy keys are rejected. The exported JSON Schema covers structural constraints; `validate_policy` additionally checks semantic constraints such as duplicate IDs and contradictory thresholds.

## API

- `validate_policy(policy)` raises `ValueError`; it does not mutate the policy.
- `canonical_policy_identity(policy)` returns a SHA-256-prefixed canonical JSON identity. Object key order, family order, and reference order do not affect identity.
- `family_for_row(row, policy)` extracts one validated family.
- `summarize_rows(rows, policy)` assesses the final row iterable.
- `assess_composition(policy, measured_counts)` assesses a complete final composition count without materializing rows. Omitted families count as zero. Callers bind this report to the final dataset identity and split; applying it to an input inventory does not verify the output.
- `assess_exposure(policy, measured_counts)` assesses observed training presentations for one checkpoint. Omitted families are unknown. It never substitutes available rows, planned batches, gradient-accumulation inputs, or total training steps for committed exposure. Repeated presentations count separately; distinct-row coverage requires separate evidence.
- `assess_evaluation(policy, measured, baseline=None)` expects `{family_id: {support: integer, metrics: {metric_name: number}}}`. Missing support or configured metrics are unknown; inadequate support or failed thresholds are failed. Regression rules require both `max_regression` and `direction` (`higher` or `lower`), and a baseline meeting that family's minimum support. Missing/unsupported baseline is unknown. Tolerances are absolute metric-unit degradation, not relative percentages. Auxiliary metrics may be present.

Reports have `status` (`passed`, `failed`, or `unknown`), `policy_id`, per-family details, `failures`, and `unknowns`. A known failure dominates unknown evidence in overall status, but both reason lists remain visible. Neither failed nor unknown is success. Composition reports additionally expose `counts` and `total_rows`.

## Checkpoint gates and stopping

The built-in rule `family_exposure/v1` has `{id, kind, policy}`. Its facts have exactly `policy_id`, `checkpoint_digest`, `expected_checkpoint_digest`, and `counts`. The checkpoint digests are identical 64-character lowercase SHA-256 values. A policy mismatch, checkpoint mismatch, insufficient exposure, or missing exposure evidence blocks the rule.

These helpers perform deterministic assessment, not authentication. A digest identifies content; a caller claiming `verification: verified` is not trusted provenance. Runtime integrations must independently verify the final artifact, checkpoint receipt, and measurement source, then bind them to the exact target. A receipt for a later checkpoint does not establish exposure for an earlier selected checkpoint. Likewise, baseline comparisons must use a compatible evaluation definition and verified baseline identity; the numeric helper cannot establish that compatibility.

A convergence criterion alone must not authorize successful early stopping or promotion when any selected exposure or evaluation gate is failed/unknown. An elapsed budget, cancellation, or unsupported continuation may legitimately end execution, but the result remains incomplete/blocked rather than successfully covered. This module does not extend budgets, schedule more training, or overwrite the caller's resource policy.

The existing `representation/v1` rule still checks sampling strata and allocation. It does not measure optimizer-committed exposure and is not a substitute for this contract.

## Held-out records and scientific gate configuration

The Transformers evaluator accepts optional `family_policy`. Its ordinary `evaluation_records` collection retains aggregate metrics and additionally publishes one exact item-addressed record for each supported family/metric. `modelbuilder.family_evaluation.family_metric_id(family_id, metric)` creates collision-safe selector names using the full SHA-256 identity of the two strings. Per-family records pin policy identity, family ID, minimum item support, exact cohort, and original admitted evaluation cohort. Classification supports accuracy and mean cross-entropy; causal-LM cross-entropy is the sum of shifted-label negative log likelihood divided by the total effective supervised-token count, with perplexity its exponential. Zero-target items are rejected. Missing or undersized families are explicitly unavailable in `family_evaluation`; their absent evaluation records make scientific decisions unknown, never passed.

Use the existing `modelbuilder.scientific.gate` baseline/candidate native inputs and scientific policy, with optional `family_policy` and required companion `family_metrics`. Every configured family/metric must have a rule. The policy's configured absolute and regression limits are enforced in addition to the selected scientific method. Scalar thresholds are descriptive; they make no statistical significance claim. Family cohorts must be disjoint, and different metrics for a family must use the same cohort. Existing native selectors, source binding, attestation and exact checkpoint/model binding apply without a separate passed-flag API.

For a required **full-stage benchmark**, configure `required_evaluation` as `{dataset_digest, split_digest, sample_count}`, derived with `evaluation_cohort_identity(dataset, split)` from the exact registered full held-out split. The evaluator's protocol and consumption provenance must both match that identity, and the family slices must form its exact disjoint union. The separate optional `required_benchmark` checks a label only; it cannot establish complete benchmark coverage. A subset reusing a benchmark label does not satisfy `required_evaluation`. This requirement is configurable per scientific policy and has no product-specific benchmark constant.

Scientific policies and native selector bindings currently support at most 32 rules. A family/metric cross-product exceeding 32 is explicitly rejected; nothing truncates the family inventory. Configure a supported policy or extend the shared bounded rule/binding contracts before requesting a larger scientific gate.

Required exposure uses the existing native `modelbuilder.rules.gate` with `family_exposure/v1` and its `target` checkpoint input. The gate re-reads verified checkpoint assets and compares facts to the exact checkpoint's asset-backed receipt; neither a caller's count map nor a passed flag suffices. Historical fixture `best`/`last` container selections without independent exposure receipts are unsupported and fail closed. Gating a later checkpoint does not authorize an earlier selected checkpoint.

## Implemented integration and limits

- Registered manifests accept `family_policy`. Final train splits are checked after all transformations, including filters after sampling. Eager and SQLite-indexed streaming paths bind the final report to their split identities. Validation/test quotas are assessed separately by evaluation, not borrowed from training counts.
- Transformers v1 and v2 measure exposure only on the supported **single-process CPU, float32, no-AMP** path. Selecting a family policy on an unsupported execution mode is rejected. Pending microbatches, prefetched examples, skipped/nonfinite optimizer updates, and causal rows without shifted supervised targets do not earn exposure credit. Repeated presentations count toward `min_exposure`; unique sample IDs are reported separately. This does not guarantee every unique row has been seen.
- v1 can end at its step budget with incomplete family coverage. v2 defers ordinary convergence while exposure is incomplete and budget remains, but preserves cancellation, custom/safety stops and hard limits. Completion of the process is distinct from passing a coverage gate.
- Every native checkpoint has its own asset-verified exposure receipt. Exact v2 resume restores its policy and counters. A later checkpoint's receipt cannot qualify an earlier target. Fixture-container `best`/`last` selection is deliberately rejected by the family gate until it supplies independent selection-specific evidence; pass the exact supported checkpoint artifact instead.
- Configure `family_exposure/v1` in the existing activated rule/required-guard system for downstream execution, promotion or export. An arbitrary custom training adapter is not automatically intercepted by attaching a dataset policy. Unsupported producers cannot satisfy the native exposure guard; extensions must implement equivalent verified measurements before claiming support.
- The evaluator accepts an explicit `family_policy`, reports collision-safe family metric IDs, actual item support, and token-weighted causal-LM metrics. Scientific policies pair `family_policy` with `family_metrics`; every requested family/metric needs a rule, with a visible 32-rule ceiling rather than truncation. Missing or undersized evidence blocks a selected gate. Family floors and regression tolerances are descriptive constraints, not automatically statistical significance claims.
- `required_benchmark` checks a label only. To require a complete stage evaluation, set `required_evaluation` to the expected dataset digest, split digest and sample count, derived by `evaluation_cohort_identity`. The gate verifies the admitted parent cohort and requires disjoint family slices whose union covers it exactly. Both baseline and candidate must satisfy the pinned identity.
- `research.context.build` accepts `family_policy` plus explicit `family_sources` links. Its hash binds the policy and links; the IDE shows represented, missing and budget-omitted families. This inventories evidence retrieval, not scientific adequacy or training/evaluation completion. The same operation is available through HTTP and MCP.

No family taxonomy, balancing temperature, universal quality threshold, or automatic inference-based label assignment is imposed. This version supports one scalar family label per row; hierarchical/multi-label taxonomies and GPU/MLX/distributed exposure adapters remain extension work. Coverage checks detect unmet obligations; they do not silently change the chosen sampler, expand budgets, or infer a new training curriculum.
