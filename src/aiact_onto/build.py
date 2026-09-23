import hashlib
import json
import re
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any

import yaml
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.compare import to_isomorphic
from rdflib.namespace import DCTERMS, OWL, PROV, RDF, RDFS, SKOS

DEFAULTS = {
    "in_file": "data/curated/entities.json",
    "namespaces": "config/namespaces.yaml",
    "core": "ontology/aiact-core.ttl",
    "alignments": "ontology/aiact-alignments.ttl",
    "out_ttl": "ontology/aiact-generated.ttl",
    "out_owl": "ontology/aiact-merged.owl",
    "robot": "robot",
}
NAMESPACE_DEFAULTS = {
    "base": "https://w3id.org/aiact-poc#",
    "ontology": {"generated": "https://w3id.org/aiact-poc/generated"},
    "source": "http://data.europa.eu/eli/reg/2024/1689/oj",
    "prefixes": {},
}
# entity_type -> (how it is built, the core class it hangs from)
TYPE_MAP = {
    "AISystemCategory": ("class", "AISystem"),
    "Actor": ("class", "OperatorRole"),
    "Authority": ("class", "Authority"),
    "ProhibitedPractice": ("individual", "ProhibitedPractice"),
    "HighRiskArea": ("individual", "HighRiskArea"),
    "Requirement": ("individual", "Requirement"),
    "Obligation": ("individual", "Obligation"),
}
# Terms a bearer or applies_to value may use for a class the core file already defines.
CORE_BEARERS = {
    "provider": "ProviderRole",
    "providers": "ProviderRole",
    "deployer": "DeployerRole",
    "deployers": "DeployerRole",
    "importer": "ImporterRole",
    "distributor": "DistributorRole",
    "authorised representative": "AuthorisedRepresentativeRole",
    "authorized representative": "AuthorisedRepresentativeRole",
    "commission": "Commission",
    "european commission": "Commission",
    "member state": "MemberState",
    "member states": "MemberState",
    "ai office": "AIOffice",
    "national competent authority": "NationalCompetentAuthority",
    "national competent authorities": "NationalCompetentAuthority",
    "market surveillance authority": "MarketSurveillanceAuthority",
    "notifying authority": "NotifyingAuthority",
    "notified body": "NotifiedBody",
}
CORE_SYSTEMS = {
    "ai system": "AISystem",
    "ai systems": "AISystem",
    "high-risk ai system": "HighRiskAISystem",
    "high-risk ai systems": "HighRiskAISystem",
    "high risk ai system": "HighRiskAISystem",
    "prohibited ai system": "ProhibitedAISystem",
    "limited-risk ai system": "LimitedRiskAISystem",
    "minimal-risk ai system": "MinimalRiskAISystem",
    "general-purpose ai model": "GeneralPurposeAIModel",
    "general purpose ai model": "GeneralPurposeAIModel",
    "gpai model": "GeneralPurposeAIModel",
    "general-purpose ai model with systemic risk": "GPAIModelWithSystemicRisk",
}
NON_ALPHANUMERIC = re.compile(r"[^0-9A-Za-z]+")
ARTICLE_PART = re.compile(r"^(Art\. \d+|Annex [IVXLC]+)")


def camel_case(term: str) -> str:
    """Turns a term into a CamelCase local name for an IRI.

    Args:
        term (str): The entity's term, e.g. "real-time remote biometric identification".

    Returns:
        str: The local name, e.g. "RealTimeRemoteBiometricIdentification".
    """
    words = [w for w in NON_ALPHANUMERIC.split(term) if w]
    name = "".join(w[:1].upper() + w[1:] for w in words)
    return name or "Unnamed"


class Build:
    def __init__(self, config: str | Path = "./config/pipeline.yaml"):
        """Loads the build settings and the namespaces.

        Args:
            config (str | Path): The path to the pipeline YAML file; its "build" section overrides DEFAULTS.
        """
        self.config = self.load_config(config, "build", DEFAULTS)
        self.namespaces = self.load_config(self.config["namespaces"], None, NAMESPACE_DEFAULTS)
        self.in_file = Path(self.config["in_file"])
        self.core = Path(self.config["core"])
        self.alignments = Path(self.config["alignments"])
        self.out_ttl = Path(self.config["out_ttl"])
        self.out_owl = Path(self.config["out_owl"])
        self.aiact = Namespace(self.namespaces["base"])
        self.ontology_iri = URIRef(self.namespaces["ontology"]["generated"])
        self.warnings: list[str] = []
        self.core_terms = self.load_core_terms()

    def load_core_terms(self) -> set[URIRef]:
        """Lists the IRIs the hand-authored core file already defines.

        Returns:
            set[URIRef]: Every subject the core file declares as a class or a property.
        """
        if not self.core.is_file():
            return set()
        graph = Graph().parse(self.core, format="turtle")
        kinds = (OWL.Class, OWL.ObjectProperty, OWL.AnnotationProperty, OWL.DatatypeProperty)
        terms = {s for kind in kinds for s in graph.subjects(RDF.type, kind) if isinstance(s, URIRef)}
        self.core_labels = {str(label).strip().lower(): s for s in terms for label in graph.objects(s, RDFS.label)}
        return terms

    def load_config(self, path: str | Path, section: str | None, defaults: dict[str, Any]) -> dict[str, Any]:
        """Loads a YAML file, or one section of it, over a set of defaults.

        Args:
            path (str | Path): The path to the YAML file.
            section (str | None): The section to read, or None for the whole file.
            defaults (dict[str, Any]): The values to fall back on.

        Returns:
            dict[str, Any]: The defaults updated with the file's values.
        """
        try:
            with open(path, "r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
        except FileNotFoundError:
            config = {}
        return {**defaults, **((config.get(section) or {}) if section else config)}

    def load_entities(self) -> list[dict[str, Any]]:
        """Loads the curated entities written by S5.

        Returns:
            list[dict[str, Any]]: The entities, in the order S5 wrote them.

        Raises:
            FileNotFoundError: If S5 has not been run yet.
        """
        if not self.in_file.is_file():
            raise FileNotFoundError(f"{self.in_file} not found; run the curation step (S5) first")
        with open(self.in_file, "r", encoding="utf-8") as file:
            return json.load(file)

    def mint(self, entities: list[dict[str, Any]]) -> dict[str, URIRef]:
        """Mints one IRI per entity from its term, keeping collisions apart.

        A term that yields a local name already taken by another entity gets a numeric suffix, so
        an IRI never stands for two entities.

        Args:
            entities (list[dict[str, Any]]): The curated entities.

        Returns:
            dict[str, URIRef]: The IRI of each entity, keyed by entity id.
        """
        iris, taken = {}, {}
        for entity in entities:
            name = camel_case(entity["term"])
            existing = self.core_labels.get(entity["term"].strip().lower())
            if existing is not None and self.aiact[name] not in self.core_terms:
                self.warnings.append(f"{entity['term']!r} has the same label as the core class "
                                     f"{str(existing).split('#')[-1]}; its references and quotes were added to it")
                iris[entity["id"]] = existing
                continue
            if self.aiact[name] in self.core_terms:
                self.warnings.append(f"{entity['term']!r} names {name}, which the core file already defines; "
                                     f"its references and quotes were added to that class")
                iris[entity["id"]] = self.aiact[name]
                continue
            if name in taken:
                self.warnings.append(f"{entity['term']!r} ({entity['id']}) collides with {taken[name]!r} on {name}")
                suffix = 2
                while f"{name}_{suffix}" in taken:
                    suffix += 1
                name = f"{name}_{suffix}"
            taken[name] = entity["term"]
            iris[entity["id"]] = self.aiact[name]
        return iris

    def resolve(self, value: str | None, entities: list[dict[str, Any]], iris: dict[str, URIRef],
                types: tuple[str, ...], core: dict[str, str]) -> URIRef | None:
        """Finds the IRI a bearer or applies_to value refers to.

        A curated entity of a fitting type wins; otherwise the value may name a class the core file
        already defines. Nothing is minted for a value that matches neither.

        Args:
            value (str | None): The bearer or applies_to value.
            entities (list[dict[str, Any]]): The curated entities.
            iris (dict[str, URIRef]): Their IRIs.
            types (tuple[str, ...]): The entity types that may be referred to.
            core (dict[str, str]): Terms mapped to local names in the core file.

        Returns:
            URIRef | None: The IRI, or None when the value refers to nothing known.
        """
        if not value:
            return None
        wanted = value.strip().lower()
        for entity in entities:
            if entity["entity_type"] in types and entity["term"].strip().lower() == wanted:
                return iris[entity["id"]]
        if wanted in core:
            return self.aiact[core[wanted]]
        self.warnings.append(f"{value!r} does not match a curated entity or a core class")
        return None

    def run_id(self, entities: list[dict[str, Any]]) -> str:
        """Derives a run id from the curated data, so an unchanged build produces an unchanged file.

        Args:
            entities (list[dict[str, Any]]): The curated entities.

        Returns:
            str: The first 12 hex characters of the SHA-256 of the curated entities.
        """
        payload = json.dumps(entities, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def provenance(self, graph: Graph, entities: list[dict[str, Any]]) -> URIRef:
        """Adds the pipeline run that generated this build.

        Args:
            graph (Graph): The graph being built.
            entities (list[dict[str, Any]]): The curated entities.

        Returns:
            URIRef: The IRI of the run.
        """
        run = self.aiact[f"run_{self.run_id(entities)}"]
        models = sorted({e["model"] for e in entities if e.get("model")})
        graph.add((run, RDF.type, PROV.Activity))
        graph.add((run, RDFS.label, Literal(f"AIAct-Onto pipeline run {self.run_id(entities)}", lang="en")))
        graph.add((run, DCTERMS.source, URIRef(self.namespaces["source"])))
        for model in models:
            graph.add((run, DCTERMS.description, Literal(f"extraction and verification model: {model}", lang="en")))
        return run

    def legal_source(self, graph: Graph, subject: URIRef, source: dict[str, Any]) -> None:
        """Links an entity to the provision it comes from and declares that provision.

        Args:
            graph (Graph): The graph being built.
            subject (URIRef): The entity's IRI.
            source (dict[str, Any]): One of the entity's sources.
        """
        if not source.get("eli_uri"):
            return
        provision = URIRef(source["eli_uri"])
        graph.add((subject, self.aiact.hasLegalSource, provision))
        graph.add((provision, RDF.type, self.aiact.LegalProvision))
        graph.add((provision, RDF.type, OWL.NamedIndividual))
        match = ARTICLE_PART.match(source.get("article_ref", ""))
        if match:
            graph.add((provision, RDFS.label, Literal(match.group(1), lang="en")))

    def entity_triples(self, graph: Graph, entity: dict[str, Any], iri: URIRef, run: URIRef,
                       entities: list[dict[str, Any]], iris: dict[str, URIRef]) -> None:
        """Adds one curated entity to the graph (README §6, S6).

        Args:
            graph (Graph): The graph being built.
            entity (dict[str, Any]): The curated entity.
            iri (URIRef): Its IRI.
            run (URIRef): The IRI of the pipeline run.
            entities (list[dict[str, Any]]): All curated entities, for resolving bearers.
            iris (dict[str, URIRef]): Their IRIs.
        """
        if iri in self.core_terms:  # the core file owns the declaration; only add the grounding
            for ref in entity["article_refs"]:
                graph.add((iri, self.aiact.articleRef, Literal(ref)))
            for source in entity["sources"]:
                graph.add((iri, self.aiact.sourceQuote, Literal(source["source_quote"])))
                self.legal_source(graph, iri, source)
            graph.add((iri, PROV.wasGeneratedBy, run))
            return

        kind, parent = TYPE_MAP[entity["entity_type"]]
        if kind == "class":
            graph.add((iri, RDF.type, OWL.Class))
            graph.add((iri, RDFS.subClassOf, self.aiact[parent]))
            core = CORE_BEARERS.get(entity["term"].strip().lower()) if entity["entity_type"] == "Actor" else None
            if core:  # e.g. the curated "provider" is the core ProviderRole under another name
                graph.add((iri, OWL.equivalentClass, self.aiact[core]))
        else:
            graph.add((iri, RDF.type, self.aiact[parent]))
            graph.add((iri, RDF.type, OWL.NamedIndividual))

        graph.add((iri, RDFS.label, Literal(entity["label"] or entity["term"], lang="en")))
        graph.add((iri, SKOS.definition, Literal(entity["definition"], lang="en")))
        graph.add((iri, self.aiact.curationStatus, Literal(entity["curation_status"])))
        graph.add((iri, PROV.wasGeneratedBy, run))
        for ref in entity["article_refs"]:
            graph.add((iri, self.aiact.articleRef, Literal(ref)))
        for source in entity["sources"]:
            graph.add((iri, self.aiact.sourceQuote, Literal(source["source_quote"])))
            self.legal_source(graph, iri, source)

        if entity["entity_type"] == "Obligation":
            bearer = self.resolve(entity["bearer"], entities, iris, ("Actor", "Authority"), CORE_BEARERS)
            if bearer:
                graph.add((iri, self.aiact.obligationOf, bearer))
                graph.add((bearer, RDF.type, OWL.NamedIndividual))  # punning: the role class as an individual
        if entity["entity_type"] in ("Obligation", "Requirement"):
            system = self.resolve(entity["applies_to"], entities, iris, ("AISystemCategory",), CORE_SYSTEMS)
            if system:
                graph.add((iri, self.aiact.appliesToSystem, system))
                graph.add((system, RDF.type, OWL.NamedIndividual))  # punning: the system class as an individual

    def generate(self, entities: list[dict[str, Any]]) -> Graph:
        """Builds the generated graph from the curated entities.

        Args:
            entities (list[dict[str, Any]]): The curated entities.

        Returns:
            Graph: The graph, with its ontology header.
        """
        graph = Graph()
        for prefix, iri in self.namespaces["prefixes"].items():
            graph.bind(prefix, Namespace(iri))
        graph.bind("owl", OWL)
        graph.bind("skos", SKOS)
        graph.bind("prov", PROV)
        graph.bind("dcterms", DCTERMS)

        graph.add((self.ontology_iri, RDF.type, OWL.Ontology))
        graph.add((self.ontology_iri, DCTERMS.title, Literal("AIAct-Onto generated entities", lang="en")))
        graph.add((self.ontology_iri, RDFS.comment,
                   Literal("Built by S6 from data/curated/entities.json. Do not edit by hand.", lang="en")))
        graph.add((self.ontology_iri, OWL.imports, URIRef(self.namespaces["ontology"].get("core", ""))))
        if self.namespaces.get("license"):
            graph.add((self.ontology_iri, DCTERMS.license, URIRef(self.namespaces["license"])))

        iris = self.mint(entities)
        run = self.provenance(graph, entities)
        for entity in entities:
            if entity["entity_type"] not in TYPE_MAP:
                self.warnings.append(f"{entity['term']!r} has unknown type {entity['entity_type']!r}")
                continue
            self.entity_triples(graph, entity, iris[entity["id"]], run, entities, iris)
        return graph

    def merge(self, generated: Graph) -> Graph:
        """Merges the hand-authored files with the generated one for a single release file.

        Args:
            generated (Graph): The generated graph.

        Returns:
            Graph: The merged graph, without the imports statement the merge makes redundant.
        """
        merged = Graph()
        for prefix, namespace in generated.namespaces():
            merged.bind(prefix, namespace)
        for path in (self.core, self.alignments):
            if path.is_file():
                merged.parse(path, format="turtle")
            else:
                self.warnings.append(f"{path} not found; it is not part of the merged ontology")
        merged += generated
        merged.remove((None, OWL.imports, None))
        return merged

    def robot_convert(self, source: Path) -> bool:
        """Converts the merged file with ROBOT when it is installed.

        Args:
            source (Path): The merged Turtle file to convert.

        Returns:
            bool: True if ROBOT wrote the RDF/XML file, False if ROBOT is not available.
        """
        robot = shutil.which(self.config["robot"])
        if robot is None:
            return False
        subprocess.run([robot, "merge", "--input", str(source), "convert", "--format", "owl",
                        "--output", str(self.out_owl)], check=True)
        return True

    def save(self, generated: Graph, merged: Graph) -> tuple[Path, Path]:
        """Writes the generated Turtle file and the merged release file.

        ROBOT produces the RDF/XML when it is installed; otherwise rdflib does, which is enough for
        Protégé but skips ROBOT's own checks.

        Args:
            generated (Graph): The generated graph.
            merged (Graph): The merged graph.

        Returns:
            tuple[Path, Path]: The paths written.
        """
        self.out_ttl.parent.mkdir(parents=True, exist_ok=True)
        generated.serialize(destination=self.out_ttl, format="turtle")

        merged_ttl = self.out_owl.with_suffix(".merged.ttl")
        merged.serialize(destination=merged_ttl, format="turtle")
        if self.robot_convert(merged_ttl):
            merged_ttl.unlink()
        else:
            self.warnings.append("ROBOT not found; the merged ontology was written by rdflib instead")
            with warnings.catch_warnings():  # rdflib warns about list nodes it can in fact write
                warnings.simplefilter("ignore", UserWarning)
                merged.serialize(destination=self.out_owl, format="pretty-xml")
            merged_ttl.unlink()

        written = Graph().parse(self.out_owl, format="xml")
        if to_isomorphic(written) != to_isomorphic(merged):
            self.warnings.append(f"{self.out_owl} does not hold every triple of the merged graph "
                                 f"({len(written)} of {len(merged)}); check the axioms that use lists")
        return self.out_ttl, self.out_owl

    def run(self) -> dict[str, int]:
        """Builds the ontology from the curated entities.

        Returns:
            dict[str, int]: Counts of entities, generated classes and individuals, and triples.
        """
        entities = self.load_entities()
        generated = self.generate(entities)
        merged = self.merge(generated)
        self.save(generated, merged)
        return {
            "entities": len(entities),
            "classes": len(set(generated.subjects(RDF.type, OWL.Class))),
            "individuals": len(set(generated.subjects(RDF.type, OWL.NamedIndividual))),
            "generated_triples": len(generated),
            "merged_triples": len(merged),
            "warnings": len(self.warnings),
        }


if __name__ == "__main__":
    build = Build()
    counts = build.run()
    for warning in dict.fromkeys(build.warnings):
        print(f"warning: {warning}")
    print(json.dumps(counts, indent=2))
    print(f"Saved {build.out_ttl} and {build.out_owl}")
