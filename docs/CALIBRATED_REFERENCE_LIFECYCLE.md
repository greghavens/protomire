# Calibrated frozen references

The generic source-bound component contract can carry a separately selected
native checkpoint and temperature artifact into a user-owned Torch objective.
This is an adapter/evidence workflow, not a platform training engine or a new
artifact registry. The same project, model identity and immutable recipe history
remain authoritative; ZenML owns run, model-version and artifact identities.

## Supported composition

1. Produce or select a checkpoint through a declared source-bound component.
2. A source-bound utility component calls `backend.calibration.fit_temperature`
   on a distinct calibration split. The artifact pins the exact model, fit
   dataset, ordered sample IDs, implementation/configuration, fitted temperature
   and before/after fit NLL. A fixed transform may instead declare a fixed
   temperature and explicitly state that no fitting occurred.
3. Bind the chosen checkpoint and calibration native artifacts to a training
   component's typed inputs. Its own framework code chooses the objective, KL
   direction, coefficient and output semantics, freezes the reference and owns
   optimizer/checkpoint state. Input content is part of exact-resume identity.
4. `run.resume(mode=exact_state)` retains the parent's selected-node cut and
   public input pins. The service validates the selected trainer before resolving
   dataset dependencies. The backend freshly checks native ownership and content;
   saved host paths are not reused. Unselected reference/calibration producers
   and their dataset imports are not silently executed.
5. A warm start uses the current recipe. A genuine cut must still bind its
   selected inputs; a full, unbound parent's warm start uses the full current
   graph, including graph edits. Exact-state input replacements are incompatible.
   Use an explicit new-stage submission to choose different reference/calibration
   bindings. Generic component exact resume keeps the original training budget.
6. Evaluate raw/calibrated predictions on the same untouched sample IDs, declaring
   calibration as the comparison intervention. Export the selected checkpoint
   with its calibration as a required hashed asset and verify fresh-process
   prediction parity. Exporting the newest checkpoint implicitly is not allowed.

The temperature adapter rejects a recomputed-digest artifact with zero,
negative, boolean, nonnumeric or unrepresentable temperature, rejects nonfinite
metadata during canonical identity checking, and refuses nonfinite transformed
predictions. This narrow check is not a claim that arbitrary re-authored metadata
or an entirely rehashed forged package is authenticated by its own digest.

## Independent local acceptance

`tests/integration/test_calibrated_reference_lifecycle.py` copies the independently
authored three-class model and standalone loader into a new Git repository. It
runs normal `OperationService` operations and actual pinned local ZenML/Torch
processes; there are no provider calls or external weights/data. The workload has
24 training, 12 fitting and 12 evaluation examples. Reference preparation uses
two updates; policy configurations use six updates.

The integration exercises:

- Separately selected, frozen reference checkpoints with a fixed temperature.
  Both KL directions are compared with an independent scalar math calculation;
  identical calibrated distributions give zero KL. Reference gradients stay
  absent and parameter/artifact bytes stay unchanged.
- Identical policy initialization with zero/nonzero coefficient, checking actual
  objective and parameter effects. Real cancellation after a committed update,
  exact continuation and equality of model, optimizer, scheduler, RNG, cursor and
  history against uninterrupted execution. Warm start honors the current objective.
- Refusal of changed reference/calibration exact-continuation bindings, followed
  by a distinct successful stage using an earlier policy and its fitted calibration
  as the new reference. Multiple training components coexist in the canonical
  recipe, while continuation executes only the selected policy node.
- Real fitting on the dedicated split, explicit native fit/eval leakage rejection,
  raw/calibrated NLL, accuracy and five-bin equal-width L1 ECE on untouched
  evaluation samples. Positive scalar temperature preserves argmax accuracy;
  improved held-out metrics are not assumed.
- Earlier selected checkpoint export after a newer policy run exists; native
  package assembly/read, clean-process standalone loading, exact prediction
  parity, missing/tampered calibration refusal, and native binding integrity
  rejection before altered calibration-artifact storage is loaded.

Run with the existing pinned backend environment:

```sh
PYTHONPATH="$PWD/src" .venv-backend/bin/python -m pytest \
  tests/integration/test_calibrated_reference_lifecycle.py -q -s
```

The test is explicitly included in `scripts/test_all.sh` when
`MODELBUILDER_RUN_INTEGRATION=1`. Raw run/source/artifact IDs, measurements and logs
are written only to pytest's local temporary directory. They are not checked in
or uploaded by the test. Focused service tests additionally guard full-graph warm
starts, historical enriched binding normalization, incompatible ports, mixed
unselected built-in trainers and missing unselected registered dataset files.

This establishes a local native service/component path when its integration test
passes. It does not establish fresh GUI interaction, native multi-OS execution,
GPU/provider training, public publication, a calibration GUI operation family,
or every part of the larger generic lifecycle milestone.
