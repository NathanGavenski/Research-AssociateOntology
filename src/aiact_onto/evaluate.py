"""S8: measures the ontology and the pipeline that built it, into reports/metrics.json (README §7.1).

Every number the documentation quotes comes from here, so a rerun of the pipeline cannot leave the
write-up quoting stale figures.
"""
import json
from collections import Counter
from pathlib import Path
from typing import Any

import owlrl
import yaml
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from aiact_onto.curate import replay
from aiact_onto.validate import Validate

DEFAULTS = {
    "ontology": "ontology/aiact-merged.owl",
    "examples": "ontology/examples.ttl",
    "core": "ontology/aiact-core.ttl",
    "alignments": "ontology/aiact-alignments.ttl",
    "queries": "queries",
    "extracted": "data/extracted",
    "verified": "data/verified",
    "curated": "data/curated",
    "gold": "data/gold",
    "validation": "reports/validation.json",
    "out_file": "reports/metrics.json",
}
AIACT = Namespace("https://w3id.org/aiact-poc#")
MAPPING_PROPERTIES = (SKOS.closeMatch, SKOS.exactMatch, SKOS.relatedMatch)
NEEDS_REASONING = {"cq6_classified_systems.rq"}
# What the critic's verdict implies the reviewer should do, for the agreement measure.
CRITIC_EXPECTS = {"supported": "keep", "partially": "change", "unsupported": "drop"}
DECISION_MEANS = {"accept": "keep", "edit": "change", "merge": "change", "reject": "drop"}


def percentage(part: int, whole: int) -> float | None:
    """Returns part as a percentage of whole, to one decimal, or None when whole is zero."""
    return round(100 * part / whole, 1) if whole else None


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Computes Cohen's kappa between two raters over the same items.

    Args:
        pairs (list[tuple[str, str]]): One (first rater, second rater) label pair per item.

    Returns:
        float | None: Kappa to three decimals, or None when there is nothing to compare or the
            raters agree so uniformly that chance agreement is total.
    """
    if not pairs:
        return None
    labels = sorted({label for pair in pairs for label in pair})
    total = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / total
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum((first[label] / total) * (second[label] / total) for label in labels)
    return None if expected == 1 else round((observed - expected) / (1 - expected), 3)


class Evaluate:
    def __init__(self, config: str | Path = "./config/pipeline.yaml"):
        """Loads the evaluation settings.

        Args:
            config (str | Path): The path to the pipeline YAML file; its "evaluate" section overrides DEFAULTS.
        """
        self.config = self.load_config(config)
        self.path = Path(self.config["out_file"])
        self.graph = Graph().parse(self.config["ontology"], format="xml")

    def load_config(self, path: str | Path) -> dict[str, Any]:
        """Loads the "evaluate" section of the pipeline config.

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
        return {**DEFAULTS, **(config.get("evaluate") or {})}

    def jsonl(self, folder: str, pattern: str = "*.jsonl") -> list[dict[str, Any]]:
        """Reads every record from the JSONL files in a folder.

        Args:
            folder (str): The folder to read.
            pattern (str): Which files to read.

        Returns:
            list[dict[str, Any]]: The records, in filename order.
        """
        records = []
        for file in sorted(Path(folder).glob(pattern)):
            with open(file, "r", encoding="utf-8") as handle:
                records.extend(json.loads(line) for line in handle if line.strip())
        return records

    def robot_measure(self) -> dict[str, int]:
        """Asks ROBOT for the axiom counts, which rdflib cannot give.

        Returns:
            dict[str, int]: ROBOT's metrics, or an empty dict when ROBOT is unavailable.
        """
        validator = Validate(self.config.get("config", "./config/pipeline.yaml"))
        if not validator.robot:
            return {}
        out = validator.work / "measure.tsv"
        out.parent.mkdir(parents=True, exist_ok=True)
        result = validator.run_robot("measure", "--input", validator.arg(Path(self.config["ontology"])),
                                     "--output", validator.arg(out))
        if result.returncode != 0 or not out.is_file():
            return {}
        wanted = ("axiom_count", "logical_axiom_count", "class_count", "individual_count")
        measured = {}
        for line in out.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] in wanted:
                measured[parts[0]] = int(parts[1])
        return measured

    def size(self) -> dict[str, Any]:
        """Counts what the ontology contains.

        Blank nodes are left out: an anonymous class expression such as a union in a property range
        is typed owl:Class but is not an entity anyone would count.

        Returns:
            dict[str, Any]: Counts of classes, properties, individuals and triples, plus ROBOT's
                axiom counts when ROBOT is available.
        """
        count = lambda kind: len({s for s in self.graph.subjects(RDF.type, kind) if isinstance(s, URIRef)})
        return {
            **self.robot_measure(),
            "classes": count(OWL.Class),
            "object_properties": count(OWL.ObjectProperty),
            "datatype_properties": count(OWL.DatatypeProperty),
            "annotation_properties": count(OWL.AnnotationProperty),
            "named_individuals": count(OWL.NamedIndividual),
            "triples": len(self.graph),
        }

    def structure(self) -> dict[str, Any]:
        """Measures the shape of the class hierarchy.

        Returns:
            dict[str, Any]: Maximum and mean depth below the roots, and mean children per parent.
        """
        parents: dict[URIRef, list[URIRef]] = {}
        for child, parent in self.graph.subject_objects(RDFS.subClassOf):
            if isinstance(child, URIRef) and isinstance(parent, URIRef) and str(child).startswith(str(AIACT)):
                parents.setdefault(child, []).append(parent)

        def depth(node: URIRef, seen: frozenset = frozenset()) -> int:
            if node in seen or node not in parents:
                return 0
            return 1 + max(depth(p, seen | {node}) for p in parents[node])

        depths = [depth(node) for node in parents]
        children = Counter(parent for values in parents.values() for parent in values)
        return {
            "max_depth": max(depths, default=0),
            "mean_depth": round(sum(depths) / len(depths), 2) if depths else None,
            "mean_children_per_parent": round(sum(children.values()) / len(children), 2) if children else None,
        }

    def annotation(self) -> dict[str, Any]:
        """Measures how completely the entities are annotated.

        Returns:
            dict[str, Any]: The share of entities carrying each annotation, and traceability to ELI.
        """
        entities = set(self.graph.subjects(AIACT.curationStatus, None))
        generated = {e for e in entities
                     if "hand-authored" not in {str(s) for s in self.graph.objects(e, AIACT.curationStatus)}}
        share = lambda group, prop: percentage(sum(1 for e in group if (e, prop, None) in self.graph), len(group))
        return {
            "entities": len(entities),
            "with_label": share(entities, RDFS.label),
            "with_definition": share(entities, SKOS.definition),
            "with_article_ref": share(entities, AIACT.articleRef),
            "generated_entities": len(generated),
            "generated_with_source_quote": share(generated, AIACT.sourceQuote),
            "generated_with_eli_link": share(generated, AIACT.hasLegalSource),
            "obligations_with_bearer": share(set(self.graph.subjects(RDF.type, AIACT.Obligation)), AIACT.obligationOf),
        }

    def reuse(self) -> dict[str, Any]:
        """Measures how much of the hand-authored core is aligned to an external ontology.

        Returns:
            dict[str, Any]: The number of core classes, how many are mapped, and the share.
        """
        core = Graph().parse(self.config["core"], format="turtle")
        core_classes = {s for s in core.subjects(RDF.type, OWL.Class)
                        if isinstance(s, URIRef) and str(s).startswith(str(AIACT))}
        alignments = Graph().parse(self.config["alignments"], format="turtle")
        mapped = {s for prop in (*MAPPING_PROPERTIES, RDFS.subClassOf) for s in alignments.subjects(prop, None)}
        return {
            "core_classes": len(core_classes),
            "aligned_core_classes": len(core_classes & mapped),
            "aligned_percent": percentage(len(core_classes & mapped), len(core_classes)),
        }

    def competency_questions(self) -> dict[str, Any]:
        """Runs every competency question and records how many rows each returns.

        Returns:
            dict[str, Any]: The row count per query and the share that answer at all.
        """
        reasoned = Graph().parse(self.config["ontology"], format="xml")
        reasoned.parse(self.config["examples"], format="turtle")
        owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(reasoned)

        rows = {}
        for query in sorted(Path(self.config["queries"]).glob("*.rq")):
            graph = reasoned if query.name in NEEDS_REASONING else self.graph
            rows[query.stem] = len(list(graph.query(query.read_text(encoding="utf-8"))))
        answered = sum(1 for count in rows.values() if count)
        return {"questions": len(rows), "answered": answered,
                "answered_percent": percentage(answered, len(rows)), "rows": rows}

    def pipeline(self) -> dict[str, Any]:
        """Measures the funnel from extraction to curation, and the critic's agreement with the reviewer.

        Returns:
            dict[str, Any]: Stage counts, grounding rates, decision rates and Cohen's kappa.
        """
        extracted = self.jsonl(self.config["extracted"])
        verified = self.jsonl(self.config["verified"], "entities.jsonl")
        rejected = self.jsonl(self.config["verified"], "rejected.jsonl")
        curated_file = Path(self.config["curated"]) / "entities.json"
        decisions_file = Path(self.config["curated"]) / "decisions.jsonl"

        curated = json.loads(curated_file.read_text(encoding="utf-8")) if curated_file.is_file() else []
        decisions = []
        if decisions_file.is_file():
            with open(decisions_file, "r", encoding="utf-8") as handle:
                decisions = [json.loads(line) for line in handle if line.strip()]
        state = replay(decisions)

        methods = Counter(record.get("grounding_method") for record in verified)
        verdicts = Counter(record.get("critic_verdict") for record in verified)
        actions = Counter(decision["action"] for decision in state.values())
        pairs = []
        for decision in state.values():
            verdict = decision.get("critic_verdict")
            if verdict in CRITIC_EXPECTS and decision["action"] in DECISION_MEANS:
                pairs.append((CRITIC_EXPECTS[verdict], DECISION_MEANS[decision["action"]]))

        decided = sum(actions.values())
        return {
            "extracted_records": len(extracted),
            "grounding_rejected": len(rejected),
            "grounding_pass_percent": percentage(len(extracted) - len(rejected), len(extracted)),
            "exact_quote_percent": percentage(methods["exact"], len(verified)),
            "verified_records": len(verified),
            "critic_verdicts": {str(k): v for k, v in verdicts.items()},
            "curated_entities": len(curated),
            "decisions": dict(actions),
            "decision_percent": {action: percentage(count, decided) for action, count in actions.items()},
            "unchanged_percent": percentage(actions["accept"], decided),
            "critic_reviewer_agreement_percent": percentage(sum(1 for a, b in pairs if a == b), len(pairs)),
            "critic_reviewer_kappa": kappa(pairs),
            "entities_by_type": dict(Counter(entity["entity_type"] for entity in curated)),
        }

    def logical(self) -> dict[str, Any]:
        """Reads what S7 concluded, so the metrics carry the reasoner's verdict.

        Returns:
            dict[str, Any]: The status of each validation check, or a note that S7 has not run.
        """
        path = Path(self.config["validation"])
        if not path.is_file():
            return {"available": False, "note": "run the validation step (S7) first"}
        report = json.loads(path.read_text(encoding="utf-8"))
        return {"available": True, "engine": report.get("engine"),
                "checks": {check["check"]: check["status"] for check in report.get("checks", [])}}

    def gold(self) -> dict[str, Any]:
        """Reports on the gold sample, which is what precision and recall would be measured against.

        Returns:
            dict[str, Any]: Whether a gold sample exists, and what it would give.
        """
        files = [p for p in Path(self.config["gold"]).glob("*") if p.suffix in (".json", ".jsonl")]
        return {
            "annotated_articles": len(files),
            "note": "No gold sample yet. Annotating 3-5 articles by hand before running the pipeline "
                    "would give precision and recall per entity type, the most defensible accuracy number.",
        }

    def run(self) -> dict[str, Any]:
        """Computes every metric and writes reports/metrics.json.

        Returns:
            dict[str, Any]: The metrics, grouped as in README §7.1.
        """
        metrics = {
            "size": self.size(),
            "structure": self.structure(),
            "annotation": self.annotation(),
            "reuse": self.reuse(),
            "competency_questions": self.competency_questions(),
            "pipeline": self.pipeline(),
            "logical": self.logical(),
            "gold_sample": self.gold(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump(metrics, file, indent=2)
        return metrics


if __name__ == "__main__":
    evaluate = Evaluate()
    metrics = evaluate.run()
    size, annotation, pipeline = metrics["size"], metrics["annotation"], metrics["pipeline"]
    print(f"{size['classes']} classes, {size['named_individuals']} individuals, {size['triples']} triples")
    print(f"annotation: {annotation['with_label']}% labelled, {annotation['with_definition']}% defined, "
          f"{annotation['obligations_with_bearer']}% of obligations have a bearer")
    print(f"pipeline: {pipeline['extracted_records']} extracted -> {pipeline['verified_records']} verified "
          f"-> {pipeline['curated_entities']} curated; critic/reviewer kappa {pipeline['critic_reviewer_kappa']}")
    print(f"competency questions answered: {metrics['competency_questions']['answered']}"
          f"/{metrics['competency_questions']['questions']}")
    print(f"Saved {evaluate.path}")
