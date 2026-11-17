"""Build-time preparation: fetch the pinned model and precompute embeddings.

    python -m app.prepare fetch-model     # needs network; run at image build time
    python -m app.prepare build-index     # offline; needs the model on disk

The running service never downloads anything.
"""

from __future__ import annotations

import json
import sys

from .config import MODEL_ID, MODEL_REVISION, get_settings

# Only the files needed to run the model with PyTorch on CPU.
MODEL_FILES = [
    "modules.json",
    "config.json",
    "config_sentence_transformers.json",
    "sentence_bert_config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
    "1_Pooling/config.json",
    "README.md",
]


def fetch_model() -> None:
    from huggingface_hub import snapshot_download

    settings = get_settings()
    settings.model_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=str(settings.model_dir),
        allow_patterns=MODEL_FILES,
    )
    print(json.dumps({"model_id": MODEL_ID, "revision": MODEL_REVISION, "path": str(settings.model_dir)}))


def build_index() -> None:
    from .retrieval.semantic import SentenceTransformerEmbedder, write_artifact
    from .tickets import load_corpus

    settings = get_settings()
    corpus = load_corpus(settings.tickets_path, settings.searchable_status_set)
    if not corpus.tickets:
        raise SystemExit("no valid tickets to index")
    embedder = SentenceTransformerEmbedder(
        settings.model_dir, MODEL_ID, MODEL_REVISION, num_threads=settings.torch_num_threads
    )
    manifest = write_artifact(corpus, embedder, settings.artifacts_dir)
    summary = {key: value for key, value in manifest.items() if key != "rows"}
    summary["tickets"] = len(corpus.tickets)
    print(json.dumps(summary))


def main(argv: list[str]) -> None:
    commands = {"fetch-model": fetch_model, "build-index": build_index}
    if len(argv) != 2 or argv[1] not in commands:
        raise SystemExit(f"usage: python -m app.prepare [{' | '.join(commands)}]")
    commands[argv[1]]()


if __name__ == "__main__":
    main(sys.argv)
