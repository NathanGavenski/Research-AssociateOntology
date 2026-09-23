import json

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, PROV, RDF, RDFS, SKOS

from aiact_onto.build import Build, camel_case

AIACT = Namespace("https://w3id.org/aiact-poc#")
ELI = "http://data.europa.eu/eli/reg/2024/1689/art_16/oj"


def curated(term: str, entity_type: str, bearer: str | None = None, applies_to: str | None = None,
            ref: str = "Art. 16(a)", status: str = "accepted") -> dict:
    return {
        "id": term.lower().replace(" ", "")[:12],
        "term": term, "entity_type": entity_type, "label": term.lower(), "definition": f"Definition of {term}.",
        "bearer": bearer, "applies_to": applies_to, "article_refs": [ref],
        "sources": [{"article_ref": ref, "chunk_id": "art_16", "eli_uri": ELI, "source_quote": f"quote for {term}"}],
        "confidence": 0.9, "critic_verdict": "supported", "model": "qwen3.5:4b",
        "curation_status": status, "reviewer": "t", "decided_at": "2026-09-22T10:00:00+00:00", "merged_ids": [],
    }


@pytest.fixture
def builder(tmp_path):
    def make(entities: list[dict]) -> Build:
        (tmp_path / "entities.json").write_text(json.dumps(entities), encoding="utf-8")
        config = tmp_path / "pipeline.yaml"
        config.write_text(
            "build:\n"
            f"  in_file: {(tmp_path / 'entities.json').as_posix()}\n"
            "  namespaces: config/namespaces.yaml\n"
            "  core: ontology/aiact-core.ttl\n"
            "  alignments: ontology/aiact-alignments.ttl\n"
            f"  out_ttl: {(tmp_path / 'generated.ttl').as_posix()}\n"
            f"  out_owl: {(tmp_path / 'merged.owl').as_posix()}\n"
            "  robot: robot-not-installed\n",
            encoding="utf-8",
        )
        return Build(config)
    return make


def test_camel_case_handles_punctuation_and_empty_terms():
    assert camel_case("real-time remote biometric identification") == "RealTimeRemoteBiometricIdentification"
    assert camel_case("‘provider’") == "Provider"
    assert camel_case("...") == "Unnamed"


def test_actors_become_classes_and_obligations_become_individuals(builder):
    build = builder([curated("Provider", "Actor"), curated("Draw Up Declaration", "Obligation", bearer="Provider")])
    graph = build.generate(build.load_entities())
    assert (AIACT.Provider, RDF.type, OWL.Class) in graph
    assert (AIACT.Provider, RDFS.subClassOf, AIACT.OperatorRole) in graph
    assert (AIACT.DrawUpDeclaration, RDF.type, AIACT.Obligation) in graph
    assert (AIACT.DrawUpDeclaration, RDF.type, OWL.NamedIndividual) in graph


def test_every_entity_carries_its_annotations_and_provenance(builder):
    build = builder([curated("Keep Logs", "Obligation", bearer="Provider", status="edited")])
    graph = build.generate(build.load_entities())
    iri = AIACT.KeepLogs
    assert (iri, RDFS.label, Literal("keep logs", lang="en")) in graph
    assert (iri, SKOS.definition, Literal("Definition of Keep Logs.", lang="en")) in graph
    assert (iri, AIACT.articleRef, Literal("Art. 16(a)")) in graph
    assert (iri, AIACT.sourceQuote, Literal("quote for Keep Logs")) in graph
    assert (iri, AIACT.curationStatus, Literal("edited")) in graph
    assert (iri, AIACT.hasLegalSource, URIRef(ELI)) in graph
    assert (URIRef(ELI), RDF.type, AIACT.LegalProvision) in graph
    assert next(graph.objects(iri, PROV.wasGeneratedBy), None) is not None


def test_bearer_resolves_to_a_curated_actor_before_a_core_class(builder):
    build = builder([curated("Provider", "Actor"), curated("Keep Logs", "Obligation", bearer="provider")])
    graph = build.generate(build.load_entities())
    assert (AIACT.KeepLogs, AIACT.obligationOf, AIACT.Provider) in graph
    assert not build.warnings


def test_bearer_falls_back_to_the_core_role_and_is_punned(builder):
    build = builder([curated("Keep Logs", "Obligation", bearer="Deployers")])
    graph = build.generate(build.load_entities())
    assert (AIACT.KeepLogs, AIACT.obligationOf, AIACT.DeployerRole) in graph
    assert (AIACT.DeployerRole, RDF.type, OWL.NamedIndividual) in graph


def test_applies_to_maps_to_a_core_system_class(builder):
    build = builder([curated("Risk Management", "Requirement", applies_to="high-risk AI systems")])
    graph = build.generate(build.load_entities())
    assert (AIACT.RiskManagement, AIACT.appliesToSystem, AIACT.HighRiskAISystem) in graph


def test_unknown_bearer_is_reported_and_nothing_is_minted_for_it(builder):
    build = builder([curated("Keep Logs", "Obligation", bearer="Someone Else")])
    graph = build.generate(build.load_entities())
    assert not list(graph.objects(AIACT.KeepLogs, AIACT.obligationOf))
    assert any("Someone Else" in w for w in build.warnings)


def test_colliding_terms_get_separate_iris(builder):
    build = builder([curated("Human Oversight", "Requirement"), curated("human-oversight", "Requirement")])
    iris = build.mint(build.load_entities())
    assert len(set(iris.values())) == 2
    assert any("collides" in w for w in build.warnings)


def test_run_id_changes_only_when_the_curated_data_changes(builder):
    build = builder([curated("A", "Requirement")])
    entities = build.load_entities()
    assert build.run_id(entities) == build.run_id(list(entities))
    assert build.run_id(entities) != build.run_id([curated("B", "Requirement")])


def test_run_writes_both_files_and_merges_the_hand_authored_axioms(builder):
    build = builder([curated("Provider", "Actor"), curated("Keep Logs", "Obligation", bearer="Provider")])
    counts = build.run()
    assert counts["entities"] == 2 and counts["classes"] == 1 and counts["individuals"] >= 1

    generated = Graph().parse(build.out_ttl, format="turtle")
    merged = Graph().parse(build.out_owl, format="xml")
    assert (AIACT.Provider, RDFS.subClassOf, AIACT.OperatorRole) in generated
    assert (AIACT.OperatorRole, RDF.type, OWL.Class) not in generated       # the core file owns it
    assert (AIACT.OperatorRole, RDF.type, OWL.Class) in merged              # and the merge brings it in
    assert (AIACT.AISystem, SKOS.closeMatch, URIRef("https://w3id.org/airo#AISystem")) in merged
    assert not list(merged.triples((None, OWL.imports, None)))
    assert not build.out_owl.with_suffix(".merged.ttl").exists()
