"""Pin this ordinary wrapper in a trusted project source closure before running.

The shipped implementations load local admitted dependencies only. Every node
still uses the existing component.run ABI, source admission and native ZenML.
"""
from modelbuilder.backend.components.local_inference import generate, judge, prepare_dataset

__all__ = ["generate", "judge", "prepare_dataset"]
