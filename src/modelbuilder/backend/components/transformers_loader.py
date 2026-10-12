"""Standalone offline Transformers model loader, also copied into local exports.

No Modelbuilder dependency, Hub lookup, remote Python code, or pickle loading.
The caller verifies artifact integrity before importing the exported loader.
"""
from __future__ import annotations

import json
from pathlib import Path


def load(directory):
    import torch
    from safetensors.torch import load_model
    from tokenizers import Tokenizer
    from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForSequenceClassification

    directory = Path(directory)
    settings = json.loads((directory / "adapter.json").read_text())
    raw = json.loads((directory / "config.json").read_text())
    if not isinstance(raw, dict) or any(key in raw for key in ("auto_map", "quantization_config")):
        raise ValueError("Only installed, unquantized Transformers architectures are supported")
    model_type = raw.pop("model_type")
    config = AutoConfig.for_model(model_type, **raw)
    if config.is_encoder_decoder:
        raise ValueError("Encoder-decoder tasks require a source-owned adapter")
    task = settings["task"]
    classes = {"sequence-classification": AutoModelForSequenceClassification, "causal-lm": AutoModelForCausalLM}
    if task not in classes:
        raise ValueError("Unsupported Transformers task")
    if task == "sequence-classification":
        if (len(settings["labels"]) < 2 or len(settings["labels"]) != config.num_labels
                or config.problem_type not in (None, "single_label_classification")):
            raise ValueError("Ordered labels must match the model's single-label classification head")
        config.problem_type = "single_label_classification"
    elif settings["labels"]:
        raise ValueError("Causal language modeling does not take classification labels")
    if config.pad_token_id not in (None, settings["pad_token_id"]):
        raise ValueError("Explicit padding token disagrees with model configuration")
    config.pad_token_id = settings["pad_token_id"]
    context_length = getattr(config, "max_position_embeddings", getattr(config, "n_positions", None))
    if isinstance(context_length, int) and settings["max_length"] > context_length:
        raise ValueError("max_length exceeds the model's configured context length")
    config._attn_implementation = "eager"
    # Count with meta tensors before allocating user-sized model weights. Limits
    # are explicit recipe resource budgets, never a fixture-sized model ceiling.
    with torch.device("meta"):
        probe = classes[task].from_config(config, trust_remote_code=False)
    count = sum(parameter.numel() for parameter in probe.parameters())
    del probe
    if count > settings["limits"]["max_parameters"]:
        raise ValueError("Model parameter count exceeds the recipe's declared resource limit")
    with torch.device("cpu"):
        model = classes[task].from_config(config, trust_remote_code=False).to(dtype=torch.float32)
    model.tie_weights()
    load_model(model, str(directory / "model.safetensors"), strict=True, device="cpu")
    tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
    tokenizer.no_padding()
    tokenizer.no_truncation()
    vocab = model.get_input_embeddings().num_embeddings
    if (any(token < 0 or token >= vocab for token in tokenizer.get_vocab().values())
            or not 0 <= settings["pad_token_id"] < vocab):
        raise ValueError("Tokenizer or padding token is outside the model vocabulary")
    return model.eval(), tokenizer, settings


def encode(texts, tokenizer, settings):
    """Explicit fixed-length preprocessing; truncation is an opt-in recipe choice."""
    length = settings["max_length"]
    encoded = []
    for text in texts:
        if not isinstance(text, str) or not text:
            raise ValueError("A nonempty text field is required")
        tokens = tokenizer.encode(text, add_special_tokens=True).ids
        if len(tokens) > length and not settings["truncate"]:
            raise ValueError("Tokenized text exceeds max_length; enable explicit truncation or increase it")
        tokens = tokens[:length]
        if len(tokens) < (2 if settings["task"] == "causal-lm" else 1):
            raise ValueError("Text has too few tokens for the selected task")
        encoded.append({"input_ids": tokens + [settings["pad_token_id"]] * (length - len(tokens)),
                        "attention_mask": [1] * len(tokens) + [0] * (length - len(tokens))})
    return encoded


def predict(directory, texts):
    import torch
    model, tokenizer, settings = load(directory)
    rows = encode(texts, tokenizer, settings)
    with torch.inference_mode():
        return model(**{key: torch.tensor([row[key] for row in rows]) for key in rows[0]}).logits


if __name__ == "__main__":
    import sys
    import torch
    root = Path(sys.argv[1])
    torch.set_num_threads(1)
    fixture = json.loads((root / "parity-input.json").read_text())
    result = predict(root, fixture["texts"])
    torch.save(result, root / "parity-actual.pt")
