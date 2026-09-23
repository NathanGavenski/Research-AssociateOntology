from aiact_onto.verify import Verify, ground, normalise

ARTICLE = {
    "id": "art_5",
    "type": "article",
    "article_number": "5",
    "text": "Article 5 Prohibited AI practices 1. The following AI practices shall be prohibited: "
            "(a) the use of ‘real-time’ remote biometric identification systems in publicly accessible spaces;",
}
ANNEX = {
    "id": "anx_III",
    "type": "annex",
    "annex_number": "III",
    "text": "ANNEX III High-risk AI systems referred to in Article 6(2) 3. Education and vocational training:",
}


def record(quote: str, ref: str = "Art. 5(1)(a)") -> dict:
    return {"source_quote": quote, "article_ref": ref}


def test_normalise_collapses_whitespace_and_quotes():
    assert normalise("  the  ‘real-time’\n“x”  ") == "the 'real-time' \"x\""


def test_exact_quote_is_grounded():
    result = ground(record("the use of ‘real-time’ remote biometric identification systems"), ARTICLE)
    assert result["grounded"] and result["grounding_method"] == "exact"


def test_straight_quotes_and_extra_whitespace_still_match_exactly():
    result = ground(record("the use of 'real-time'   remote\nbiometric identification systems"), ARTICLE)
    assert result["grounded"] and result["grounding_method"] == "exact"


def test_small_typo_is_grounded_fuzzily():
    result = ground(record("the use of 'real-time' remote biometric identifcation systems in publicly accessible spaces"), ARTICLE)
    assert result["grounded"] and result["grounding_method"] == "fuzzy"
    assert 95 <= result["grounding_score"] < 100


def test_paraphrased_quote_is_rejected():
    result = ground(record("AI systems that identify people remotely in real time in public places"), ARTICLE)
    assert not result["grounded"] and result["grounding_method"] is None
    assert "quote not found" in result["grounding_reason"]


def test_empty_quote_is_rejected():
    assert not ground(record("   "), ARTICLE)["grounded"]


def test_reference_to_another_article_is_rejected():
    result = ground(record("the use of ‘real-time’ remote biometric", ref="Art. 50(1)"), ARTICLE)
    assert not result["grounded"]
    assert "does not match chunk art_5" in result["grounding_reason"]


def test_reference_prefix_of_another_number_is_rejected():
    assert not ground(record("the use of ‘real-time’", ref="Art. 55(1)"), ARTICLE)["grounded"]


def test_bare_article_reference_is_accepted():
    assert ground(record("the use of ‘real-time’", ref="Art. 5"), ARTICLE)["grounded"]


def test_annex_reference_is_checked():
    assert ground(record("Education and vocational training", ref="Annex III(3)"), ANNEX)["grounded"]
    assert not ground(record("Education and vocational training", ref="Annex II(3)"), ANNEX)["grounded"]


def entity(term: str, entity_type: str = "Obligation", bearer: str | None = "Provider", verdict: str = "supported",
           confidence: float = 0.9, ref: str = "Art. 16(a)") -> dict:
    return {
        "term": term, "entity_type": entity_type, "bearer": bearer, "confidence": confidence,
        "article_ref": ref, "chunk_id": "art_16", "eli_uri": "eli", "source_quote": "q",
        "critic_verdict": verdict, "critic_reason": "r",
    }


def verifier() -> Verify:
    v = Verify.__new__(Verify)
    v.dedup_threshold = 90
    return v


def test_deduplicate_merges_near_identical_terms_and_keeps_all_refs():
    merged = verifier().deduplicate([
        entity("Keep Logs", verdict="partially", ref="Art. 16(e)"),
        entity("Keep logs", verdict="supported", ref="Art. 19(1)"),
    ])
    assert len(merged) == 1
    assert merged[0]["article_refs"] == ["Art. 16(e)", "Art. 19(1)"]
    assert merged[0]["article_ref"] == "Art. 19(1)"  # the supported record wins
    assert merged[0]["merged_count"] == 2


def test_deduplicate_keeps_different_bearers_apart():
    merged = verifier().deduplicate([entity("Keep Logs"), entity("Keep Logs", bearer="Deployer")])
    assert len(merged) == 2


def test_deduplicate_keeps_different_types_apart():
    merged = verifier().deduplicate([
        entity("Human Oversight", entity_type="Requirement", bearer=None),
        entity("Human Oversight", entity_type="Obligation", bearer="Deployer"),
    ])
    assert len(merged) == 2
