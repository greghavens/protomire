# Custom PyTorch CPU lifecycle fixture

This repository-owned example is an ordinary small gated-residual classifier, not a new training framework. Its recipe connects native dataset, initialization, training, evaluation and export steps. The training step consumes the actual upstream dataset artifact and verifies its canonical SHA256 before using any row.

- 240 deterministic synthetic rows; 180 train, 30 validation, 30 held out
- Width 12 baseline and width 20 architecture variant
- Explicit KL(reference || model), with the connected initialization checkpoint frozen
- Real dropout, shuffled minibatches, Adam and exponential learning-rate scheduling
- Every update saves model, reference, optimizer, scheduler, Torch RNG, Python RNG, sample permutation, cursor and epoch atomically
- Separate last/recovery and best-validation checkpoints
- Full-state continuation preserves the original horizon-independent schedule; warm start is explicitly a different mode
- Export is reloaded and must produce exactly equal held-out logits

The strong-KL comparison in integration tests deliberately produces a negative validation outcome. These tiny synthetic measurements demonstrate execution and provenance only.

Setup: scripts/bootstrap.sh --backend. Run real fixture tests with .venv-backend/bin/python -m pytest tests/integration/test_cpu_fixtures.py. Native lifecycle tests are in tests/integration/test_native_lifecycle.py. Recipes must be source-pinned; a changed source file is rejected before submission.
