# S4 to S5: what the human review changed

The review pass recorded in `data/curated/decisions.jsonl` was made by an AI assistant standing in for the human
reviewer, so what follows is evidence about what a reviewing step catches that the automated critic does not,
and not about human reviewers specifically.

Pipeline run: Qwen 3.5 4B through Ollama for both extraction (S3) and the critic (S4), temperature 0, extraction
with thinking on, critic with thinking off.

## 1. Numbers

| Stage | Result |
|---|---|
| S3 extracted | 181 candidate records across 25 articles and annexes |
| S4 grounding | 179 passed, 2 rejected (quote not found in the source) |
| S4 deduplication | 10 groups merged 29 records, leaving 160 |
| S4 critic | 44 supported, 113 partially, 3 unsupported |
| S5 review | 81 accepted, 53 edited, 20 merged, 6 rejected, leaving **134 entities** |

Decisions against the critic's verdict:

| Critic verdict | accepted | edited | merged | rejected |
|---|---|---|---|---|
| supported (44) | 36 | 5 | 2 | 1 |
| partially (113) | 44 | 47 | 18 | 4 |
| unsupported (3) | 1 | 1 | 0 | 1 |

The review changed something in 79 of 160 records (49%). The critic's verdict predicted the outcome poorly: 18% of
the records it called "supported" were still changed, and 39% of the records it called "partially" were accepted
unchanged.

What the edits touched:

| Field edited | Records |
|---|---|
| `applies_to` filled in (was null) | 40 |
| `bearer` filled in (was null) | 29 |
| `definition` rewritten | 10 |
| `term` and `label` renamed | 8 |
| `entity_type` corrected | 1 |

## 2. The cases that justify a reviewing step

### Case A. The critic pushed a systematic misclassification that would have broken the ontology

The single largest effect in the data. The Act's Chapter III, Section 2 (Art. 8 to 15) is titled *Requirements for
high-risk AI systems* and phrases each one as "High-risk AI systems shall be designed and developed in such a way
that…". Article 16(a) then separately obliges providers to "ensure that their high-risk AI systems are compliant
with the requirements set out in Section 2".

The critic read the word "shall" in Art. 9 to 15 and concluded, in **35 records**, that the entity type was wrong
and these were obligations of providers. Examples: *Accuracy*, *Cybersecurity*, *Resilience*, *Transparency*,
*Effective Human Oversight Capability*, *Data Quality Criteria*, *Automatic Recording of Events*.

Acting on that advice would have erased the distinction between a property of a system and a duty of an actor,
which is the distinction the ontology's `Requirement` / `Obligation` split and its `obligationOf` and
`appliesToSystem` properties are built on. It would also have made Art. 16(a) circular: an obligation to comply
with a set of obligations. The review kept the extractor's classification in all 35 records and instead filled in
the missing `applies_to`. One Art. 17 record went the other way — *put in place a quality management system* is
genuinely phrased as a duty on providers — which is the single `entity_type` correction in the run.

### Case B. The critic invented a fact

For Art. 51(2), the extractor recorded that a general-purpose AI model is presumed to have high impact
capabilities above 10^25 floating point operations. The critic's verdict: *"The definition incorrectly states the
threshold as 10^25 instead of the text's 10^28."*

The Act says 10^25. The critic hallucinated a number, contradicted a correct extraction, and stated it with the
same confidence as its other verdicts. A reviewer following the critic would have introduced a factual error into
the ontology that no downstream check — grounding, reasoner, SHACL — would have caught, because the value is
inside a paraphrased definition, not a quoted span.

Two smaller fabrications of the same kind: the critic said the *Re-offending Risk Assessment Systems* definition
had added "or to assess personality traits and characteristics" to Annex III(6)(d), and that *Appointment of
Authorised Representative* had added "established in third countries" to Art. 54(1). Both phrases are in the Act.
All three records were accepted unchanged against the critic's advice.

### Case C. The critic scored the fields it was not asked about

Grounding and the critic both look at whether the record is *faithful*. Neither checks whether the record is
*complete enough to build an ontology from*. The result:

- **29 obligations had no bearer.** Every Art. 26 duty ("Deployers shall monitor…", "Deployers shall assign human
  oversight…") arrived with `bearer: null`. Without a bearer, `obligationOf` cannot be asserted and the competency
  question "which obligations does a deployer hold?" returns nothing. The critic did flag most of these in its
  free-text reason, but as a "partially" verdict, at the same severity as a missing comma in a definition.
- **`applies_to` was null in 40 records**, including 29 of the 34 requirements. Requirements that apply to no
  system class cannot be attached to `HighRiskAISystem`.

After the review, all 62 obligations have a bearer and all 13 requirements have an `applies_to` value.

### Case D. Records that should not be in an ontology at all

Six records were rejected, and the critic had marked one of them "supported" and four "partially":

| Record | Critic | Why it was rejected |
|---|---|---|
| Special Categories Processing Conditions (Art. 10(5)) | **supported** | S4's deduplication had merged Art. 10(5) and its six separate conditions (a)–(f) into one record; the provision is also a conditional permission, not an obligation |
| High Impact Capabilities (Art. 51(1)(a)) | partially | A classification criterion, not a category of AI system; no entity type fits it |
| High Impact Capabilities (Presumed) (Art. 51(2)) | unsupported | A presumption about a compute threshold is not an entity |
| Consideration of Reassessment Request (Art. 52(5)) | partially | The Commission *may* decide: a power, not a duty |
| Delegated Act Amendment (Art. 6(7)) | partially | A law-making power, out of scope for the proof of concept |
| Emotion Recognition and Biometric Categorisation System (Art. 50(3)) | partially | Conflates two categories that already exist separately from Annex III(1) |

The first is the sharpest case: the critic read one record against one article, saw a faithful definition and a
verbatim quote, and passed it. It had no way to see that six distinct legal conditions had been collapsed into a
single entity by an earlier stage, because nothing in its prompt shows it the other records.

### Case E. Duplicates that survived automatic deduplication, and over-merges that did not

S4 merges on term similarity within the same type and bearer. Because S3 had left the bearer null on all Art. 26
duties, the deployer's *Keep logs* (Art. 26(6)) and the provider's *Keep Logs* (Art. 16(e)) were kept apart for
the right reason but with identical terms, which then collided when S6 minted IRIs. The same happened for
*Comply with registration obligations*. The review renamed the deployer versions and set their bearers.

In the other direction, the review merged 20 records that S4 had kept separate: the thirteen aspects of the
quality management system in Art. 17(1)(a)–(m) into one obligation, the six content items of Art. 13(3)(a)–(f)
into *Instructions for use*, and the duplicate fundamental rights impact assessment that S3 had extracted twice
from Art. 27, once as a requirement and once as an obligation. Automatic deduplication cannot make these calls,
because the terms are not similar; only a reading of the article structure shows that they are parts of one
entity.

### Case F. Terms that were wrong as names

Eight records were renamed. Annex III area records had been named after one of their sub-items — *Employment
Management Systems* for the area "Employment, workers' management and access to self-employment", and *Essential
Services Eligibility Systems* for an area that also covers credit scoring, insurance pricing and emergency call
triage. Two Art. 64 obligations were named *Commission* and *Member States*, that is, after their bearer rather
than the duty. These become the ontology's IRIs and labels, so they were renamed to the duty they express.

## 3. Effect on the built ontology

| | From S4 records | From S5 curation |
|---|---|---|
| Entities | 160 | 134 |
| Classes | 47 | 44 |
| Individuals | 140 | 122 |
| Generated triples | 1618 | 1484 |
| Unresolved references | 5 | 2 |

The two remaining unresolved references are "Commission" and "Member States" as bearers of the Art. 64 duties.
They are not modelling errors in the data but a gap in the hand-authored core: `aiact-core.ttl` has no class for
the Commission or for a Member State. This is itself a review finding — the extraction surfaced two duty-holders
the TBox did not anticipate.

## 4. Limitations

- **The reviewer was an AI, not a human.** It shares the extractor's and the critic's blind spots on anything
  requiring legal expertise, and its agreement with the extractor may be inflated by both being language models.
- **The review is a single pass with no second opinion**, so there is no inter-annotator agreement to report. Two
  independent reviewers over the same records would give a defensible reliability figure.
- **Curation decisions were not timed**, so the report says nothing about review effort per record, which is the
  number most relevant to whether the approach scales.
- **The 6 rejections and 20 merges are modelling choices, not corrections of fact.** A different reviewer could
  keep the Art. 17 aspects as separate requirements and would still be defensible.
