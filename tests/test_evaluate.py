import json
from pathlib import Path

import pytest

from aiact_onto.evaluate import Evaluate, kappa, percentage


@pytest.fixture(scope="module")
def metrics(tmp_path_factory) -> dict:
    """Runs S8 over the real repository once, writing its report into a temporary folder."""
    tmp = tmp_path_factory.mktemp("metrics")
    config = tmp / "pipeline.yaml"
    config.write_text(f"evaluate:\n  out_file: {(tmp / 'metrics.json').as_posix()}\n", encoding="utf-8")
    return Evaluate(config).run()


def test_percentage_handles_an_empty_denominator():
    assert percentage(1, 4) == 25.0
    assert percentage(0, 0) is None


def test_kappa_is_one_for_perfect_and_zero_for_chance_agreement():
    assert kappa([("keep", "keep"), ("drop", "drop")]) == 1.0
    # Two raters who agree exactly as often as chance would predict.
    chance = [("keep", "keep"), ("keep", "drop"), ("drop", "keep"), ("drop", "drop")]
    assert kappa(chance) == 0.0


def test_kappa_is_none_when_there_is_nothing_to_compare():
    assert kappa([]) is None
    assert kappa([("keep", "keep")]) is None          # one label only: chance agreement is total


def test_size_counts_named_entities_only(metrics):
    size = metrics["size"]
    assert size["classes"] == size.get("class_count", size["classes"])   # rdflib agrees with ROBOT
    assert size["object_properties"] == 11
    assert size["triples"] > 1000


def test_annotation_coverage_is_complete(metrics):
    annotation = metrics["annotation"]
    assert annotation["with_label"] == 100.0
    assert annotation["with_definition"] == 100.0
    assert annotation["generated_with_source_quote"] == 100.0
    assert annotation["obligations_with_bearer"] == 100.0


def test_every_competency_question_is_answered(metrics):
    questions = metrics["competency_questions"]
    assert questions["answered"] == questions["questions"] == 6
    assert all(rows > 0 for rows in questions["rows"].values())


def test_pipeline_funnel_only_ever_narrows(metrics):
    pipeline = metrics["pipeline"]
    assert pipeline["extracted_records"] >= pipeline["verified_records"] >= pipeline["curated_entities"]
    assert sum(pipeline["decisions"].values()) == pipeline["verified_records"]
    assert 0 <= pipeline["critic_reviewer_kappa"] <= 1


def test_logical_metrics_carry_the_reasoner_verdict(metrics):
    logical = metrics["logical"]
    assert logical["available"] and logical["checks"]["consistency"] == "pass"


def test_gold_sample_absence_is_reported_not_hidden(metrics):
    assert metrics["gold_sample"]["annotated_articles"] == 0
    assert "precision and recall" in metrics["gold_sample"]["note"]


def test_report_is_written_where_configured(tmp_path):
    config = tmp_path / "pipeline.yaml"
    out = tmp_path / "nested" / "metrics.json"
    config.write_text(f"evaluate:\n  out_file: {out.as_posix()}\n", encoding="utf-8")
    Evaluate(config).run()
    assert json.loads(out.read_text(encoding="utf-8"))["size"]["classes"] > 0
