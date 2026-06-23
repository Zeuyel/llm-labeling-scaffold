# Artifact Contracts

This scaffold separates four label layers:

1. `llm_label`: raw structured model output.
2. `human_label`: adjudication patch layer.
3. `gold_label`: versioned training/evaluation set after merge and adjudication.
4. `model_prediction`: local model extrapolation output.

Do not overwrite one layer with another. Every layer must carry enough manifest metadata to trace it back to task config, prompt/schema, model/provider, run id, and source records.
