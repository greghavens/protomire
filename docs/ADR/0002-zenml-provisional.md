# ADR 0002 Provisional ZenML execution

Use ZenML0.97.0 public SDK for authoritative pipeline, artifact and model-version identity. The application owns only model-project/recipe/research/control-operation records and limited reconciled projections. No duplicate MLflow/ClearML registry. Runtime supervisor may handle actual local OS-process lifecycle; it must never invent successful backend completion. Final go/no-go depends on independent product tests, not baseline evidence.
