import hashlib
import json
import re
from pathlib import Path
from typing import Any

import ollama
import yaml
from pydantic import ValidationError
from rapidfuzz import fuzz
from tqdm import tqdm

from aiact_onto.utils.render import render
from schemas.extraction import CriticVerdict

DEFAULTS = {
    "host": "http://localhost:11434",
    "model": "qwen3.5:9b",
    "think": False,
    "temperature": 0,
    "num_ctx": 16384,
    "num_predict": 2048,
    "prompt": "prompts/critic.md",
    "fuzzy_threshold": 95,
    "dedup_threshold": 90,
    "chunks_dir": "data/chunks",
    "in_dir": "data/extracted",
    "out_dir": "data/verified",
}
QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"'})
VERDICT_RANK = {"supported": 0, "partially": 1, "unsupported": 2, None: 3}
EXTRACTED_FIELDS = (
    "term", "entity_type", "label", "definition", "article_ref",
    "source_quote", "bearer", "applies_to", "confidence",
)


def normalise(s: str) -> str:
    """Collapses whitespace and replaces typographic quotes with straight ones.

    Args:
        s (str): The raw text.

    Returns:
        str: The normalised text.
    """
    return " ".join(s.translate(QUOTES).split())


def ground(record: dict[str, Any], chunk: dict[str, Any], threshold: float = 95) -> dict[str, Any]:
    """Checks a record against its source chunk without any model (README §6, S4 step 1).

    The quote must appear in the chunk text, exactly or with a fuzzy ratio of at least `threshold`
    after normalisation, and the article reference must name the chunk's article or annex.

    Args:
        record (dict[str, Any]): The extracted record.
        chunk (dict[str, Any]): The chunk the record was extracted from.
        threshold (float): The minimum rapidfuzz partial ratio for a fuzzy match.

    Returns:
        dict[str, Any]: "grounded", "grounding_method" ("exact", "fuzzy" or None),
            "grounding_score" and "grounding_reason" (None when grounded).
    """
    quote, source = normalise(record["source_quote"]), normalise(chunk["text"])
    if chunk["type"] == "article":
        ref_pattern = rf"^Art\. {re.escape(chunk['article_number'])}(\(|$)"
    else:
        ref_pattern = rf"^Annex {re.escape(chunk['annex_number'])}(\(|$)"

    if not quote:
        method, score = None, 0.0
    elif quote in source:
        method, score = "exact", 100.0
    else:
        score = round(fuzz.partial_ratio(quote, source), 1)
        method = "fuzzy" if score >= threshold else None

    reasons = []
    if method is None:
        reasons.append(f"quote not found in source (best match {score})")
    if not re.match(ref_pattern, record["article_ref"].strip()):
        reasons.append(f"article_ref {record['article_ref']!r} does not match chunk {chunk['id']}")

    return {
        "grounded": not reasons,
        "grounding_method": method,
        "grounding_score": score,
        "grounding_reason": "; ".join(reasons) or None,
    }


class Verify:
    def __init__(self, config: str | Path = "./config/pipeline.yaml", client: ollama.Client | None = None):
        """Loads the verification settings and critic prompt, and sets up the Ollama client.

        Args:
            config (str | Path): The path to the pipeline YAML file; its "verify" section overrides DEFAULTS.
            client (ollama.Client | None): The Ollama client, or None to connect to the configured host.
        """
        self.config = self.load_config(config)
        self.model = self.config["model"]
        self.think = self.config["think"]
        self.options = {k: self.config[k] for k in ("temperature", "num_ctx", "num_predict")}
        self.fuzzy_threshold = float(self.config["fuzzy_threshold"])
        self.dedup_threshold = float(self.config["dedup_threshold"])
        self.chunks_dir = Path(self.config["chunks_dir"])
        self.in_dir = Path(self.config["in_dir"])
        self.path = Path(self.config["out_dir"])
        self.cache_dir = self.path / ".cache"
        self.prompt = Path(self.config["prompt"]).read_text(encoding="utf-8")
        self.client = client or ollama.Client(host=self.config["host"])

    def load_config(self, path: str | Path) -> dict[str, Any]:
        """Loads the "verify" section of the pipeline config.

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
        return {**DEFAULTS, **(config.get("verify") or {})}

    def load_records(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Loads every extracted record together with the chunk it came from.

        Returns:
            list[tuple[dict[str, Any], dict[str, Any]]]: (record, chunk) pairs, in chunk filename order.
        """
        pairs = []
        for file in sorted(self.in_dir.glob("*.jsonl")):
            with open(file, "r", encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
            if not records:
                continue
            with open(self.chunks_dir / records[0]["chunk_file"], "r", encoding="utf-8") as f:
                chunk = json.load(f)
            pairs.extend((record, chunk) for record in records)
        return pairs

    def cache_key(self, text: str, record: dict[str, Any]) -> str:
        """Hashes everything that determines the critic's output for a record.

        Args:
            text (str): The rendered chunk.
            record (dict[str, Any]): The extracted record.

        Returns:
            str: The SHA-256 hex digest of the model, its settings, the prompt, the chunk and the record.
        """
        fields = {k: record[k] for k in EXTRACTED_FIELDS}
        payload = json.dumps([self.model, self.think, self.options, self.prompt, text, fields], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def call(self, text: str, record: dict[str, Any]) -> CriticVerdict:
        """Asks the local model to judge one record against its chunk.

        The chunk comes before the record, so consecutive records from the same chunk share a prompt
        prefix that Ollama can reuse.

        Args:
            text (str): The rendered chunk.
            record (dict[str, Any]): The extracted record.

        Returns:
            CriticVerdict: The critic's reason and verdict.

        Raises:
            RuntimeError: If the output was cut off or does not match the schema.
        """
        fields = {k: record[k] for k in EXTRACTED_FIELDS}
        content = f"{text}\n\n## Record\n\n{json.dumps(fields, ensure_ascii=False, indent=2)}"
        response = self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": self.prompt},
                {"role": "user", "content": content},
            ],
            format=CriticVerdict.model_json_schema(),
            think=self.think,
            options=self.options,
        )

        if response.done_reason == "length":
            raise RuntimeError(f"output hit num_predict ({self.options['num_predict']}) or num_ctx ({self.options['num_ctx']})")
        if response.prompt_eval_count and response.prompt_eval_count >= self.options["num_ctx"]:
            raise RuntimeError(f"prompt filled the context window ({self.options['num_ctx']} tokens); raise num_ctx")
        try:
            return CriticVerdict.model_validate_json(response.message.content)
        except ValidationError as error:
            raise RuntimeError(f"output does not match the schema: {error.error_count()} errors") from error

    def critique(self, record: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
        """Runs the critic on one grounded record, reusing a cached verdict when the inputs are unchanged.

        Args:
            record (dict[str, Any]): The grounded record.
            chunk (dict[str, Any]): The chunk it came from.

        Returns:
            dict[str, Any]: "critic_verdict" and "critic_reason".
        """
        text = render(chunk)
        cached = self.cache_dir / f"{self.cache_key(text, record)}.json"

        if cached.is_file():
            with open(cached, "r", encoding="utf-8") as file:
                verdict = CriticVerdict.model_validate(json.load(file))
        else:
            verdict = self.call(text, record)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(cached, "w", encoding="utf-8") as file:
                json.dump(verdict.model_dump(), file, ensure_ascii=False, indent=2)

        return {"critic_verdict": verdict.verdict, "critic_reason": verdict.reason}

    def same_entity(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Decides whether two records describe the same entity.

        Records match when they share an entity type and bearer and their terms are near-identical,
        so that e.g. the provider's and the deployer's duty to keep logs stay separate.

        Args:
            a (dict[str, Any]): A record.
            b (dict[str, Any]): Another record.

        Returns:
            bool: True if the records should be merged.
        """
        key = lambda r: (r["entity_type"], normalise(r["bearer"] or "").lower())
        if key(a) != key(b):
            return False
        score = fuzz.token_sort_ratio(normalise(a["term"]).lower(), normalise(b["term"]).lower())
        return score >= self.dedup_threshold

    def deduplicate(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merges records of the same entity, keeping every source (README §6, S4 step 3).

        The merged record takes its fields from the best-supported member (critic verdict first,
        then confidence) and lists every member's reference, quote and verdict under "sources".

        Args:
            records (list[dict[str, Any]]): The grounded, critiqued records.

        Returns:
            list[dict[str, Any]]: One record per entity, in order of first appearance.
        """
        groups: list[list[dict[str, Any]]] = []
        for record in records:
            group = next((g for g in groups if self.same_entity(g[0], record)), None)
            if group is None:
                groups.append([record])
            else:
                group.append(record)

        merged = []
        for group in groups:
            best = min(group, key=lambda r: (VERDICT_RANK[r["critic_verdict"]], -r["confidence"]))
            sources = [
                {k: r[k] for k in ("article_ref", "chunk_id", "eli_uri", "source_quote", "critic_verdict", "critic_reason")}
                for r in group
            ]
            merged.append({
                **best,
                "article_refs": list(dict.fromkeys(r["article_ref"] for r in group)),
                "sources": sources,
                "merged_count": len(group),
            })
        return merged

    def save(self, name: str, records: list[dict[str, Any]]) -> Path:
        """Writes records as JSONL.

        Args:
            name (str): The output filename, e.g. "entities.jsonl".
            records (list[dict[str, Any]]): The records to write.

        Returns:
            Path: The path of the written file.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        out = self.path / name
        with open(out, "w", encoding="utf-8") as file:
            for record in records:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return out

    def run(self) -> dict[str, int]:
        """Grounds, critiques and deduplicates every extracted record.

        Records that fail grounding skip the critic (ADR-5) and go to "rejected.jsonl"; the rest are
        merged into "entities.jsonl". A record the critic cannot judge keeps a null verdict.

        Returns:
            dict[str, int]: Counts of input, grounded, rejected and merged records, and of each verdict.
        """
        grounded, rejected = [], []
        for record, chunk in tqdm(self.load_records(), desc="Verifying"):
            record = {**record, **ground(record, chunk, self.fuzzy_threshold)}
            if not record["grounded"]:
                rejected.append({**record, "critic_verdict": None, "critic_reason": None})
                continue
            try:
                record.update(self.critique(record, chunk))
            except (ollama.ResponseError, RuntimeError) as error:
                tqdm.write(f"{record['article_ref']} {record['term']}: critic failed ({error})")
                record.update({"critic_verdict": None, "critic_reason": f"critic failed: {error}"})
            grounded.append(record)

        entities = self.deduplicate(grounded)
        self.save("entities.jsonl", entities)
        self.save("rejected.jsonl", rejected)

        counts = {
            "input": len(grounded) + len(rejected),
            "grounded": len(grounded),
            "rejected": len(rejected),
            "entities": len(entities),
        }
        for verdict in ("supported", "partially", "unsupported", None):
            counts[f"critic_{verdict}"] = sum(r["critic_verdict"] == verdict for r in grounded)
        return counts


if __name__ == "__main__":
    verify = Verify()
    counts = verify.run()
    print(json.dumps(counts, indent=2))
    print(f"Saved {counts['entities']} entities and {counts['rejected']} rejected records to {verify.path}")
