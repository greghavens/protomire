"""Inert evidence-scoped compatibility inventory, never an execution allowlist.

The descriptor protocol accepts additional source-owned frameworks without an
entry here. A metadata match or upstream documentation is not a training test.
"""
from copy import deepcopy

_DOCS = {
    "pytorch": "https://docs.pytorch.org/tutorials/beginner/basics/optimization_tutorial.html",
    "transformers": "https://huggingface.co/docs/transformers/v4.57.1/en/main_classes/trainer",
    "accelerate": "https://huggingface.co/docs/accelerate/en/basic_tutorials/launch",
    "trl": "https://huggingface.co/docs/trl/en/index",
    "peft": "https://huggingface.co/docs/peft/en/index",
    "diffusers": "https://huggingface.co/docs/diffusers/en/training/overview",
    "sentence-transformers": "https://sbert.net/docs/sentence_transformer/training_overview.html",
    "lightning": "https://github.com/Lightning-AI/pytorch-lightning/tree/master/docs/source-pytorch",
    "axolotl": "https://docs.axolotl.ai/docs/getting-started.html",
    "torchtune": "https://docs.pytorch.org/torchtune/main/tutorials/first_finetune_tutorial.html",
    "deepspeed": "https://www.deepspeed.ai/getting-started/",
    "fsdp": "https://docs.pytorch.org/docs/stable/distributed.fsdp.html",
    "jax": "https://docs.jax.dev/en/latest/quickstart.html",
    "flax": "https://flax.readthedocs.io/en/latest/quick_start.html",
    "tensorflow": "https://www.tensorflow.org/guide/keras/training_with_built_in_methods",
    "mlx": "https://ml-explore.github.io/mlx/build/html/examples.html",
}


def compatibility_catalog() -> list[dict]:
    entries = []
    categories = {
        "pytorch": "framework", "transformers": "training-library", "accelerate": "launcher",
        "trl": "training-library", "peft": "fine-tuning-library", "diffusers": "training-library",
        "sentence-transformers": "training-library", "lightning": "training-library",
        "axolotl": "training-library", "torchtune": "training-library", "deepspeed": "distributed-strategy",
        "fsdp": "distributed-strategy", "jax": "framework", "flax": "training-library",
        "tensorflow": "framework", "mlx": "framework",
    }
    for name, category in categories.items():
        entry = {"id": name, "category": category, "documentation": _DOCS[name],
            "route": "source_pinned_component_with_operator_environment",
            "operations": {key: "requires_framework_specific_adapter_and_test"
                           for key in ("train", "evaluate", "generate", "export")},
            "tested_versions": [], "hardware_acceptance": [],
            "resume": {"warm_start": "adapter_declaration_and_native_checkpoint_required",
                       "exact_state": "unsupported_in_runner_v1"},
            "launcher": "single_python_process_only",
            "telemetry": "native_attempt_logs_and_artifacts; no_synthetic_checkpoint_progress",
            "live_evaluation": "unsupported_across_runner_environments",
            "cleanup": "existing_native_attempt_process_tree_supervision",
            "verification": "contract_only_no_framework_training_claim"}
        if name == "pytorch":
            entry.update(route="existing_source_pinned_component_abi_or_runner_environment",
                tested_versions=["2.9.0+cpu"], hardware_acceptance=["linux-x86_64-cpu"],
                verification="existing_custom_cnn_and_gru_cpu_training_evaluation_export_tests")
            entry["operations"].update(train="existing_custom_source_cpu_tests", evaluate="existing_custom_source_cpu_tests",
                                       export="existing_custom_source_fresh_process_parity_tests")
        elif name == "transformers":
            entry.update(route="existing_modelbuilder.transformers_adapters_or_source_runner",
                tested_versions=["4.57.1"], hardware_acceptance=["linux-x86_64-cpu"],
                verification="existing_offline_trainer_and_inference_cpu_tests")
            entry["operations"].update(train="existing_classification_and_causal_lm_adapter_tests",
                evaluate="existing_held_out_identity_tests", generate="existing_generation_adapter_tests",
                export="existing_native_safetensors_fresh_process_parity_tests")
            entry["resume"]["warm_start"] = "existing_weights_only_adapter_tests"
            entry["live_evaluation"] = "existing_default_environment_adapter; unsupported_cross_environment"
        elif name == "accelerate":
            entry.update(tested_versions=["1.11.0"], hardware_acceptance=["linux-x86_64-cpu"],
                verification="source_owned_Accelerator_prepare_accumulate_backward_CPU_smoke; launcher_CLI_unverified")
            entry["operations"]["train"] = "two_actual_CPU_updates_with_AcceleratedOptimizer_and_native_safetensors_checkpoint"
        elif name == "mlx":
            entry.update(route="existing_reviewed_MLX_bridge; not_generic_runner_v1",
                verification="boundary_contract_tests_only_in_Linux_acceptance",
                launcher="existing_operator_selected_macOS_ARM64_worker",
                live_evaluation="see_existing_MLX_adapter_limits")
        elif name in {"fsdp", "deepspeed"}:
            entry.update(route="not_admitted_by_runner_v1", launcher="distributed_execution_unverified")
            entry["operations"] = {key: "unsupported_in_runner_v1" for key in entry["operations"]}
        entries.append(entry)
    return deepcopy(entries)
