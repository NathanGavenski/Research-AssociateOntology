import json

import pytest
from typer.testing import CliRunner

from aiact_onto.curate import app, build, priority, record_id, replay


def verified(term: str, verdict: str | None = "supported", entity_type: str = "Obligation",
             bearer: str | None = "Provider", ref: str = "Art. 16(a)", sources: list | None = None) -> dict:
    source = {"article_ref": ref, "chunk_id": "art_16", "eli_uri": "http://data.europa.eu/eli/reg/2024/1689/art_16/oj",
              "source_quote": f"quote for {term}", "critic_verdict": verdict, "critic_reason": "reason"}
    return {
        "term": term, "entity_type": entity_type, "label": term.lower(), "definition": f"Definition of {term}.",
        "article_ref": ref, "source_quote": source["source_quote"], "bearer": bearer, "applies_to": None,
        "confidence": 0.9, "chunk_id": "art_16", "eli_uri": source["eli_uri"], "model": "qwen3.5:4b",
        "critic_verdict": verdict, "critic_reason": "reason", "article_refs": [ref],
        "sources": sources or [source], "merged_count": len(sources or [source]),
    }


def decision(record: dict, action: str, **extra) -> dict:
    return {"id": record_id(record), "action": action, "timestamp": "2026-09-22T10:00:00+00:00", "reviewer": "t", **extra}


def test_record_id_is_stable_and_ignores_provenance():
    a = verified("Keep Logs")
    b = {**a, "confidence": 0.1, "model": "other", "label": "different label"}
    assert record_id(a) == record_id(b)
    assert record_id(a) != record_id({**a, "bearer": "Deployer"})


def test_priority_puts_doubtful_records_first():
    mixed = verified("Mixed", "supported", sources=[
        verified("x", "supported")["sources"][0], verified("y", "partially")["sources"][0]])
    ranked = sorted([verified("S", "supported"), verified("P", "partially"), mixed,
                     verified("U", "unsupported"), verified("N", None)], key=priority)
    assert [r["term"] for r in ranked] == ["N", "U", "Mixed", "P", "S"]


def test_replay_keeps_last_decision_and_undo_clears_it():
    a, b = verified("A"), verified("B")
    state = replay([decision(a, "reject"), decision(a, "accept"), decision(b, "accept"), decision(b, "undo")])
    assert state[record_id(a)]["action"] == "accept"
    assert record_id(b) not in state


def test_build_applies_edits_and_drops_rejections():
    a, b = verified("A"), verified("B")
    records = {record_id(r): r for r in (a, b)}
    state = replay([
        decision(a, "edit", changes={"bearer": ["Provider", "Deployer"], "entity_type": ["Obligation", "Requirement"]}),
        decision(b, "reject"),
    ])
    entities, orphans = build(records, state)
    assert len(entities) == 1 and not orphans
    assert entities[0]["bearer"] == "Deployer" and entities[0]["entity_type"] == "Requirement"
    assert entities[0]["curation_status"] == "edited"


def test_build_folds_merges_into_target_through_chains():
    a = verified("Keep Logs", ref="Art. 16(e)")
    b = verified("Keep logs", ref="Art. 19(1)")
    c = verified("Log keeping", ref="Art. 12(1)")
    records = {record_id(r): r for r in (a, b, c)}
    state = replay([
        decision(a, "accept"),
        decision(b, "merge", target=record_id(a)),
        decision(c, "merge", target=record_id(b)),
    ])
    entities, orphans = build(records, state)
    assert len(entities) == 1 and not orphans
    assert entities[0]["article_refs"] == ["Art. 16(e)", "Art. 19(1)", "Art. 12(1)"]
    assert len(entities[0]["sources"]) == 3
    assert set(entities[0]["merged_ids"]) == {record_id(b), record_id(c)}


def test_build_reports_merges_into_rejected_targets():
    a, b = verified("A"), verified("B")
    records = {record_id(r): r for r in (a, b)}
    state = replay([decision(a, "reject"), decision(b, "merge", target=record_id(a))])
    entities, orphans = build(records, state)
    assert entities == [] and orphans == [record_id(b)]


def test_build_ignores_decisions_on_records_s4_no_longer_produces():
    a = verified("A")
    entities, _ = build({}, replay([decision(a, "accept")]))
    assert entities == []


@pytest.fixture
def workspace(tmp_path):
    records = [verified("Supported One", "supported"), verified("Unsupported One", "unsupported", ref="Art. 16(b)"),
               verified("Partial One", "partially", bearer="Deployer", ref="Art. 16(c)")]
    (tmp_path / "entities.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    config = tmp_path / "pipeline.yaml"
    config.write_text(
        f"curate:\n  in_file: {(tmp_path / 'entities.jsonl').as_posix()}\n"
        f"  out_dir: {(tmp_path / 'curated').as_posix()}\n  chunks_dir: {(tmp_path / 'chunks').as_posix()}\n",
        encoding="utf-8",
    )
    return tmp_path, config, records


def run(config, keys: list[str], *args: str):
    return CliRunner().invoke(app, ["--config", str(config), *args], input="\n".join(keys) + "\n")


def test_review_session_writes_log_and_entities(workspace):
    tmp_path, config, records = workspace
    # Queue order: Unsupported One, Partial One, Supported One.
    keys = [
        "r", "not an entity",                                   # reject Unsupported One with a reason
        "e", "", "", "", "Deployer keeps the logs.", "-", "",   # edit Partial One: new definition, clear bearer
        "a",                                                    # accept Supported One
    ]
    result = run(config, keys, "review", "--reviewer", "tester")
    assert result.exit_code == 0, result.output

    log = [json.loads(l) for l in (tmp_path / "curated" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [d["action"] for d in log] == ["reject", "edit", "accept"]
    assert log[0]["reason"] == "not an entity" and log[0]["reviewer"] == "tester"
    assert log[1]["changes"] == {"definition": ["Definition of Partial One.", "Deployer keeps the logs."],
                                 "bearer": ["Deployer", None]}

    entities = json.loads((tmp_path / "curated" / "entities.json").read_text(encoding="utf-8"))
    by_term = {e["term"]: e for e in entities}
    assert set(by_term) == {"Partial One", "Supported One"}
    assert by_term["Partial One"]["bearer"] is None
    assert by_term["Partial One"]["curation_status"] == "edited"
    assert by_term["Supported One"]["curation_status"] == "accepted"


def test_review_resumes_skips_undoes_and_merges(workspace):
    tmp_path, config, records = workspace
    run(config, ["s", "q"], "review")                           # skip one, quit: nothing decided
    assert not (tmp_path / "curated" / "decisions.jsonl").exists()

    # Accept Unsupported One, undo it, accept it again, then merge Partial One into it, then quit.
    result = run(config, ["a", "u", "a", "m", "1", "q"], "review")
    assert result.exit_code == 0, result.output
    log = [json.loads(l) for l in (tmp_path / "curated" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [d["action"] for d in log] == ["accept", "undo", "accept", "merge"]

    entities = json.loads((tmp_path / "curated" / "entities.json").read_text(encoding="utf-8"))
    assert len(entities) == 1
    assert entities[0]["article_refs"] == ["Art. 16(b)", "Art. 16(c)"]

    # The next session only shows the record that is still undecided.
    result = run(config, ["q"], "review")
    assert "[1/1] Supported One" in result.output
