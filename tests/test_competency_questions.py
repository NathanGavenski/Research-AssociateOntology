"""Runs every competency question in queries/ against the built ontology (README §8).

A competency question is only answered if the query runs and comes back with a sensible answer, so
each test checks the shape of the result, not just that it is non-empty.
"""
from pathlib import Path

import owlrl
import pytest
from rdflib import Graph, Namespace

AIACT = Namespace("https://w3id.org/aiact-poc#")
QUERIES = Path("queries")
ONTOLOGY = Path("ontology/aiact-merged.owl")
EXAMPLES = Path("ontology/examples.ttl")
NEEDS_REASONING = {"cq6_classified_systems.rq"}


@pytest.fixture(scope="module")
def asserted() -> Graph:
    return Graph().parse(ONTOLOGY, format="xml")


@pytest.fixture(scope="module")
def reasoned() -> Graph:
    graph = Graph().parse(ONTOLOGY, format="xml")
    graph.parse(EXAMPLES, format="turtle")
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(graph)
    return graph


def ask(graph: Graph, name: str) -> list:
    return list(graph.query((QUERIES / name).read_text(encoding="utf-8")))


def local(value) -> str:
    return str(value).split("#")[-1]


@pytest.mark.parametrize("name", sorted(p.name for p in QUERIES.glob("*.rq")))
def test_every_competency_question_returns_an_answer(name, asserted, reasoned):
    graph = reasoned if name in NEEDS_REASONING else asserted
    assert ask(graph, name), f"{name} returned no rows"


def test_cq1_returns_provider_obligations_with_their_articles(asserted):
    rows = ask(asserted, "cq1_provider_obligations.rq")
    articles = {str(r.article) for r in rows}
    assert "Art. 16(g)" in articles                                  # draw up the EU declaration of conformity
    assert all(str(r.article).startswith(("Art.", "Annex")) for r in rows)
    assert all(str(r.definition) for r in rows)


def test_cq2_returns_the_article_5_practices_with_verbatim_quotes(asserted):
    rows = ask(asserted, "cq2_prohibited_practices.rq")
    assert {local(r.practice) for r in rows} >= {"SocialScoring", "EmotionInference"}
    assert all(str(r.article).startswith("Art. 5") for r in rows)
    assert all(len(str(r.quote)) > 20 for r in rows)


def test_cq3_returns_annex_iii_areas(asserted):
    rows = ask(asserted, "cq3_high_risk_areas.rq")
    assert all(str(r.article).startswith("Annex III") for r in rows)
    assert "Biometrics" in {local(r.area) for r in rows}


def test_cq4_returns_requirements_that_apply_to_high_risk_systems(asserted):
    rows = ask(asserted, "cq4_high_risk_requirements.rq")
    labels = {str(r.label).lower() for r in rows}
    assert any("accuracy" in label for label in labels)
    assert any("risk management" in label for label in labels)


def test_cq5_separates_provider_and_authorised_representative_duties(asserted):
    rows = ask(asserted, "cq5_gpai_obligations.rq")
    by_role = {}
    for row in rows:
        by_role.setdefault(local(row.role), set()).add(str(row.label))
    assert {"Provider", "AuthorisedRepresentative"} <= set(by_role)
    assert not by_role["Provider"] & by_role["AuthorisedRepresentative"]


def test_cq6_needs_the_reasoner_and_explains_each_classification(asserted, reasoned):
    assert not ask(asserted, "cq6_classified_systems.rq")            # nothing is classified without reasoning
    rows = ask(reasoned, "cq6_classified_systems.rq")
    found = {local(r.system): (local(r.riskClass), local(r.reason)) for r in rows}
    assert found["CVScreeningTool"] == ("AnnexIIIHighRiskAISystem", "EmploymentAndWorkersManagement")
    assert found["SocialScoringPlatform"] == ("ProhibitedAISystem", "SocialScoring")
    assert "ChatAssistant" not in found
