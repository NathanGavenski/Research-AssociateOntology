from pathlib import Path

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from aiact_onto.validate import EXAMPLES, Validate

AIACT = Namespace("https://w3id.org/aiact-poc#")


@pytest.fixture
def validator(tmp_path):
    def make(engine: str = "python") -> Validate:
        config = tmp_path / "pipeline.yaml"
        config.write_text(
            "validate:\n"
            "  ontology: ontology/aiact-merged.owl\n"
            "  examples: ontology/examples.ttl\n"
            "  shapes: shapes/aiact-shapes.ttl\n"
            f"  out_dir: {tmp_path.as_posix()}\n"
            f"  engine: {engine}\n",
            encoding="utf-8",
        )
        return Validate(config)
    return make


def classified(**types: str) -> Graph:
    """Builds a reasoned graph where each named individual has the given most specific type."""
    graph = Graph()
    graph.add((AIACT.AnnexIIIHighRiskAISystem, RDFS.subClassOf, AIACT.HighRiskAISystem))
    graph.add((AIACT.HighRiskAISystem, RDFS.subClassOf, AIACT.AISystem))
    for name, cls in types.items():
        graph.add((EXAMPLES[name], RDF.type, AIACT[cls]))
    return graph


def test_engine_python_never_reaches_for_robot(validator):
    assert validator("python").robot is None
    assert validator("python").engine == "python"


def test_paths_are_posix_for_the_container(validator):
    assert validator().arg(Path("ontology") / "aiact-merged.owl") == "ontology/aiact-merged.owl"


def test_robot_only_checks_are_skipped_without_robot(validator):
    v = validator("python")
    assert v.check_profile()["status"] == "skipped"
    assert v.check_report()["status"] == "skipped"


def test_classification_follows_the_subclass_chain(validator):
    graph = classified(CVScreeningTool="AnnexIIIHighRiskAISystem", SocialScoringPlatform="ProhibitedAISystem",
                       ChatAssistant="AISystem")
    result = validator().check_classification(graph)
    assert result["status"] == "pass", result["detail"]
    assert "HighRiskAISystem" in result["detail"]      # inherited, not asserted directly


def test_classification_fails_when_an_example_is_not_inferred(validator):
    graph = classified(CVScreeningTool="AISystem", SocialScoringPlatform="ProhibitedAISystem",
                       ChatAssistant="AISystem")
    result = validator().check_classification(graph)
    assert result["status"] == "fail" and "CVScreeningTool" in result["detail"]


def test_classification_fails_when_a_harmless_example_becomes_high_risk(validator):
    graph = classified(CVScreeningTool="AnnexIIIHighRiskAISystem", SocialScoringPlatform="ProhibitedAISystem",
                       ChatAssistant="HighRiskAISystem")
    result = validator().check_classification(graph)
    assert result["status"] == "fail" and "ChatAssistant" in result["detail"]


def test_consistency_reports_an_individual_proven_contradictory(validator):
    graph = Graph()
    graph.add((EXAMPLES.ContradictorySystem, RDF.type, OWL.Nothing))
    result = validator().check_consistency(graph, "")
    assert result["status"] == "fail" and "ContradictorySystem" in result["detail"]


def test_consistency_ignores_owl_nothing_describing_itself(validator):
    graph = Graph()
    graph.add((OWL.Nothing, OWL.equivalentClass, OWL.Nothing))   # owlrl adds this to every closure
    assert validator().check_consistency(graph, "")["status"] == "pass"


def test_consistency_fails_when_the_reasoner_refused(validator):
    result = validator().check_consistency(None, "The ontology is inconsistent")
    assert result["status"] == "fail" and "inconsistent" in result["detail"]


def test_shacl_passes_on_the_built_ontology(validator):
    result = validator("python").check_shacl()
    assert result["status"] == "pass", result["detail"]


def test_full_python_run_writes_its_reports(validator, tmp_path):
    v = validator("python")
    results = v.run()
    assert [r["status"] for r in results if r["check"] in ("parses", "consistency", "classification", "shacl")] \
        == ["pass", "pass", "pass", "pass"]
    assert (tmp_path / "validation.json").is_file() and (tmp_path / "reasoner.txt").is_file()
