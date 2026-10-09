# Pinned pretrained BERT-tiny CPU adaptation

The fixture loads the real public prajjwal1/bert-tiny base model at commit 6f75de8b60a9f8a2fdf7b69cbd86d9e64bcb3837. Config, vocabulary and PyTorch weights are independently SHA256 verified by scripts/download_model.py. Remote model code is disabled and workers load local verified files only.

The deterministic classification head is newly initialized because the upstream artifact is a base BERT model. Forty-eight synthetic sentiment sentences have distinct tokenizations using the original 30,522-token vocabulary. Splits are 32 training, eight validation and eight held-out examples.

The two recipe variants compare no KL against KL(model || frozen initialization reference), with the coefficient and exact direction recorded. Selection uses measured validation loss, not held-out accuracy. A subsequent version resumes the selected best checkpoint and records the native parent run and artifact IDs. Both planned and interrupted continuation carry optimizer, scheduler, both RNGs and minibatch position.

Exports use safetensors, reload through the public Transformers API, and require exactly equal held-out logits. Tiny templated-data accuracy is not a claim about generalization quality. Weights remain external dependencies and are never part of a recipe bundle or committed to Git.
