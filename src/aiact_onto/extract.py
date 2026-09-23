import hashlib
import json
from pathlib import Path
from typing import Any

import ollama
import yaml
from pydantic import ValidationError
from tqdm import tqdm

from aiact_onto.utils.render import render
from schemas.extraction import ExtractedEntity, ExtractionResult

DEFAULTS = {
    "host": "http://localhost:11434",
    "model": "qwen3.5:4b",
    "think": False,
    "temperature": 0,
    "num_ctx": 16384,
    "num_predict": 8192,
    "prompt": "prompts/extract.md",
    "chunks_dir": "data/chunks",
    "out_dir": "data/extracted",
}


class Extract:
    def __init__(self, config: str | Path = "./config/pipeline.yaml", client: ollama.Client | None = None):
        """Loads the extraction settings and prompt, and sets up the Ollama client.

        Args:
            config (str | Path): The path to the pipeline YAML file; its "extract" section overrides DEFAULTS.
            client (ollama.Client | None): The Ollama client, or None to connect to the configured host.
        """
        self.config = self.load_config(config)
        self.model = self.config["model"]
        self.think = self.config["think"]
        self.options = {k: self.config[k] for k in ("temperature", "num_ctx", "num_predict")}
        self.chunks_dir = Path(self.config["chunks_dir"])
        self.path = Path(self.config["out_dir"])
        self.cache_dir = self.path / ".cache"
        self.prompt = Path(self.config["prompt"]).read_text(encoding="utf-8")
        self.client = client or ollama.Client(host=self.config["host"])

    def load_config(self, path: str | Path) -> dict[str, Any]:
        """Loads the "extract" section of the pipeline config.

        Args:
            path (str | Path): The path to the pipeline YAML file.

        Returns:
            dict[str, Any]: DEFAULTS updated with any values set in the file.
        """
        try:
            with open(path, "r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
        except FileNotFoundError:
            config = {}
        return {**DEFAULTS, **(config.get("extract") or {})}

    def load_chunks(self) -> list[dict[str, Any]]:
        """Loads every chunk written by S2.

        Returns:
            list[dict[str, Any]]: The chunks, in filename order.
        """
        chunks = []
        for file in sorted(self.chunks_dir.glob("*.json")):
            with open(file, "r", encoding="utf-8") as f:
                chunks.append({**json.load(f), "file": file.name})
        return chunks

    def cache_key(self, text: str) -> str:
        """Hashes everything that determines the model's output for a chunk.

        Args:
            text (str): The rendered chunk.

        Returns:
            str: The SHA-256 hex digest of the model, its settings, the prompt and the chunk text.
        """
        payload = json.dumps([self.model, self.think, self.options, self.prompt, text], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def call(self, text: str) -> list[ExtractedEntity]:
        """Asks the local model for the entities in one rendered chunk.

        Args:
            text (str): The rendered chunk.

        Returns:
            list[ExtractedEntity]: The schema-validated entities.

        Raises:
            RuntimeError: If the output was cut off or does not match the schema.
        """
        response = self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": text},
            ],
            format=ExtractionResult.model_json_schema(),
            think=self.think,
            options=self.options,
        )

        if response.done_reason == "length":
            raise RuntimeError(f"output hit num_predict ({self.options['num_predict']}) or num_ctx ({self.options['num_ctx']})")
        if response.prompt_eval_count and response.prompt_eval_count >= self.options["num_ctx"]:
            raise RuntimeError(f"prompt filled the context window ({self.options['num_ctx']} tokens); raise num_ctx")
        try:
            return ExtractionResult.model_validate_json(response.message.content).entities
        except ValidationError as error:
            raise RuntimeError(f"output does not match the schema: {error.error_count()} errors") from error

    def extract_chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Extracts the entities in one chunk, reusing a cached response when the inputs are unchanged.

        Args:
            chunk (dict[str, Any]): The article or annex chunk.

        Returns:
            list[dict[str, Any]]: One record per entity, with the chunk's provenance attached.
        """
        text = render(chunk)
        key = self.cache_key(text)
        cached = self.cache_dir / f"{key}.json"

        if cached.is_file():
            with open(cached, "r", encoding="utf-8") as file:
                entities = [ExtractedEntity.model_validate(e) for e in json.load(file)]
        else:
            entities = self.call(text)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(cached, "w", encoding="utf-8") as file:
                json.dump([e.model_dump() for e in entities], file, ensure_ascii=False, indent=2)

        provenance = {
            "chunk_id": chunk["id"],
            "chunk_file": chunk["file"],
            "eli_uri": chunk["eli_uri"],
            "model": self.model,
            "cache_key": key,
        }
        return [{**e.model_dump(), **provenance} for e in entities]

    def save(self, chunk: dict[str, Any], records: list[dict[str, Any]]) -> Path:
        """Writes one JSONL file per chunk.

        Args:
            chunk (dict[str, Any]): The chunk the records came from.
            records (list[dict[str, Any]]): The extracted records.

        Returns:
            Path: The path of the written file, e.g. "data/extracted/art_005.jsonl".
        """
        self.path.mkdir(parents=True, exist_ok=True)
        out = self.path / Path(chunk["file"]).with_suffix(".jsonl").name
        with open(out, "w", encoding="utf-8") as file:
            for record in records:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return out

    def run(self) -> dict[str, int]:
        """Extracts every chunk and writes the results; a failed chunk is reported and skipped.

        Returns:
            dict[str, int]: The number of records written per chunk file.
        """
        counts = {}
        for chunk in tqdm(self.load_chunks(), desc="Extracting"):
            try:
                records = self.extract_chunk(chunk)
            except (ollama.ResponseError, RuntimeError) as error:
                tqdm.write(f"{chunk['file']}: skipped ({error})")
                continue
            self.save(chunk, records)
            counts[chunk["file"]] = len(records)
        return counts


if __name__ == "__main__":
    extract = Extract()
    counts = extract.run()
    print(f"Saved {sum(counts.values())} records from {len(counts)} chunks to {extract.path}")
