## Running the repository

This repository uses [uv](https://docs.astral.sh/uv/) to handle its dependencies.
I highly recommend installing since it handles dependencies faster and it is what I used to test and develop.

To install all dependencies you just need:
```bash
git clone https://github.com/NathanGavenski/Research-AssociateOntology.git
cd Research-AssociateOntology
uv sync
```

The extraction (S3) and verification (S4) stages run a local model through [Ollama](https://ollama.com/download).
Install Ollama, then pull the model set in `config/pipeline.yaml`:
```bash
ollama pull qwen3.5:4b
```

To rebuild the ontology from the committed data and curation decisions:
```bash
make all
```

To run the pipeline from scratch, including your own review of the extracted entities:
```bash
make ingest chunk extract verify   # S1–S4: download, chunk, extract and verify candidates
make curate                        # S5: review each candidate (interactive)
make build validate evaluate docs  # S6–S9: build, check and document the ontology
```
Model responses are cached in `data/extracted/.cache` and `data/verified/.cache`, so rerunning a stage with unchanged inputs and settings makes no model calls.
Windows users need `make` installed (e.g. `choco install make`).

## Pipeline

The system is a linear, file-based pipeline. Every stage reads versioned artefacts from the previous stage and writes new ones, so any stage can be rerun in isolation and every intermediate output is inspectable in the repository.

```
                 ┌──────────────────────────────────────────────────────────┐
                 │                 config/ (scope, prompts, model)          │
                 └──────────────────────────────────────────────────────────┘
                                          │
┌───────────┐   ┌───────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────────────────────┐
│ S1 Ingest │ → │ S2 Chunk  │ → │ S3 Extract   │ → │ S4 Verify    │ → │ S5 Curate              │
│ EUR-Lex   │   │ by article│   │ local LLM →  │   │ grounding +  │   │ human review           │
│ HTML      │   │ + metadata│   │ JSON (schema-│   │ LLM critic + │   │ (accept / edit / drop) │
│           │   │           │   │  constrained)│   │ dedup        │   │                        │
└───────────┘   └───────────┘   └──────────────┘   └──────────────┘   └────────────────────────┘
                                                                              │
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐     │
│ S9 Docs      │ ← │ S8 Evaluate  │ ← │ S7 Validate  │ ← │ S6 Build OWL │ ←───┘
│ pyLODE HTML  │   │ metrics, CQ  │   │ reasoner,    │   │ core TBox +  │
│ + report     │   │ coverage,    │   │ SHACL, ROBOT │   │ generated    │
│              │   │ OOPS!        │   │              │   │ ABox         │
└──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
```

### Design principles

1. **Separation of judgment and volume.** Humans decide the schema (TBox); the pipeline generates the repetitive content (annotations, individuals, article links).
2. **The LLM never writes OWL.** It only fills a fixed JSON schema. OWL is produced by deterministic code, so every axiom is reproducible and reviewable.
3. **Grounding by construction.** Every extracted item must carry a verbatim quote from its source article. A non-LLM string check rejects anything whose quote does not appear in the source.
4. **Provenance everywhere.** Each generated entity records its source article (ELI URI), the pipeline run that produced it and whether a human edited it.
5. **Hand-authored core, generated periphery.** `aiact-core.ttl` is written by hand; `aiact-generated.ttl` is rebuilt from curated data; `aiact.ttl` imports both.
6. **Local models.** Both LLM stages run on a local model through Ollama, so the pipeline needs no API key and reruns cost nothing.

### Stages

Each stage is a Python module (`python -m aiact_onto.<stage>`) with a `make` target. Stage settings live in `config/pipeline.yaml`.

#### S1 Ingest

- **Input:** EUR-Lex HTML of Regulation (EU) 2024/1689 (CELEX `32024R1689`, English).
- **Output:** `data/raw/output.html` plus `data/raw/last_ingest.txt` (retrieval time).
- **Notes:** the page is downloaded again only when the snapshot is more than a day old. The SHA-256 of the snapshot is recorded in every chunk, so later stages can tell which version of the text they were built from.

#### S2 Chunk

- **Input:** raw HTML.
- **Output:** one file per article or annex in scope, e.g. `data/chunks/art_005.json`, `data/chunks/annex_III.json`. Each chunk holds `id`, `type`, `article_number` or `annex_number`, `title`, `eli_uri`, `paragraphs[]`, the full `text` and the source `metadata` (URL, CELEX, retrieval date, SHA-256).
- **Rules:** split on article boundaries; keep paragraph and point numbering, because article references must reach point level (e.g. `5(1)(c)`).
- **Filter:** only chunks listed in `config/scope.yaml`.

#### S3 Extract (AI-assisted)

- **Input:** chunks.
- **Output:** `data/extracted/<chunk>.jsonl`, one record per candidate entity.
- **Schema (`schemas/extraction.py`):**

```python
class ExtractedEntity(BaseModel):
    term: str                      # canonical name, e.g. "Provider"
    entity_type: Literal["AISystemCategory", "ProhibitedPractice", "HighRiskArea",
                         "Actor", "Authority", "Requirement", "Obligation"]
    label: str
    definition: str                # one sentence, paraphrased
    article_ref: str               # e.g. "Art. 5(1)(c)"
    source_quote: str              # verbatim span from the chunk
    bearer: str | None = None      # for obligations: which role holds it
    applies_to: str | None = None  # for obligations/requirements: which system class
    confidence: float
```

- **Model:** Qwen 3.5 9B through Ollama, which fits an 8 GB GPU; `qwen3.5:4b` is a lighter alternative. Any Ollama model can be set in `config/pipeline.yaml`.
- **Input format:** every passage of the chunk is prefixed with its reference (e.g. `[5(1)(c)] ...`), so the model can give point-level references.
- **Prompt strategy (`prompts/extract.md`):** role and task; the closed list of entity types with one example each; instruction to copy `source_quote` verbatim; instruction to return an empty list when unsure, never a guess; few-shot examples from Art. 3 and Art. 5.
- **Settings:** temperature 0, output constrained to the JSON schema, one call per chunk. Responses are cached by a hash of the model, its settings, the prompt and the chunk, so only changed inputs are sent to the model again.
- **Provenance:** each record also carries `chunk_id`, `chunk_file`, `eli_uri`, `model` and `cache_key`.

#### S4 Verify

One deterministic check, then two steps on the records that pass it:

1. **Grounding check (deterministic).** After collapsing whitespace and replacing typographic quotes with straight ones, `source_quote` must appear in the chunk text, exactly or with a fuzzy ratio ≥ 95. `article_ref` must match the chunk's article or annex. Failures skip the critic and go to `data/verified/rejected.jsonl` with the reason.
2. **Critic check (LLM).** A second prompt (`prompts/critic.md`) receives the article text and the record and answers: is the definition faithful? is the entity type right? are the bearer and system class right? Output is `{reason, verdict: supported | partially | unsupported}`.
3. **Deduplication.** Records with the same entity type and bearer and near-identical terms (rapidfuzz) are merged across articles. The merged record keeps every article reference, quote and verdict under `sources`.

Output: `data/verified/entities.jsonl` with `grounded`, `critic_verdict` and `critic_reason` fields.

#### S5 Curate (human in the loop)

- A small `typer` + `rich` CLI shows each record next to its source quote and critic verdict and asks: accept / edit / reject / merge.
- Records the critic marked `unsupported`, or could not judge, are shown first.
- Every decision is logged (`data/curated/decisions.jsonl`) so you can report acceptance, edit and rejection rates.
- `data/curated/entities.json` is the single source of truth for S6.

#### S6 Build OWL

- `rdflib` loads `aiact-core.ttl`, then for each curated record:
  - maps `entity_type` to a core class (e.g. `ProhibitedPractice` → individual of `aiact:ProhibitedPractice`; `Actor` → subclass of `aiact:OperatorRole`);
  - mints an IRI from the term (CamelCase, stable, collision-checked);
  - adds `rdfs:label`, `skos:definition`, `aiact:articleRef`, `aiact:sourceQuote`, `aiact:hasLegalSource` (ELI URI), `aiact:curationStatus`, `prov:wasGeneratedBy`;
  - for obligations, adds `obligationOf` and `appliesToSystem` links.
- Writes `aiact-generated.ttl`, then uses ROBOT `merge` to produce `aiact-merged.owl` (RDF/XML) for Protégé.
- Alignments to AIRO/VAIR/DPV live in `aiact-alignments.ttl`, authored by hand (use `rdfs:subClassOf` or `skos:exactMatch`/`closeMatch`; prefer SKOS when unsure, since it does not affect reasoning).

#### S7 Validate

| Check | Tool | Pass condition |
|---|---|---|
| Parses as OWL | owlready2, Protégé | No parse errors |
| Consistency | HermiT via owlready2 and ROBOT `reason` | Ontology consistent, 0 unsatisfiable classes |
| Classification | HermiT | Example systems inferred into the expected risk class |
| Profile | ROBOT `validate-profile --profile DL` | OWL 2 DL |
| Annotation completeness | pySHACL with `shapes/aiact-shapes.ttl` | Every class and individual has label, definition, articleRef |
| Quality report | ROBOT `report` | No ERROR-level issues |
| Pitfalls | OOPS! (web service) | No critical pitfalls |

Screenshots from Protégé (class hierarchy, reasoner result, inferred hierarchy) go in `reports/screenshots/`.

#### S8 Evaluate

- Computes the preliminary metrics (size, structure, annotation coverage, reuse, traceability, CQ coverage) into `reports/metrics.json`.
- Measures pipeline accuracy against the manually annotated sample in `data/gold/`, and pipeline robustness from the grounding pass rate and the agreement between critic verdicts and curation decisions.

#### S9 Document

- `pylode` generates browsable HTML from `aiact-merged.owl`.
- The 2-page note is written by hand and draws numbers from `reports/metrics.json`.
## Repository structure

```
Research-AssociateOntology/
├── Makefile                          # one target per stage; "make all" rebuilds everything
├── pyproject.toml, uv.lock           # pinned dependencies
├── config/
│   ├── scope.yaml                    # which articles and annexes are in scope
│   ├── pipeline.yaml                 # model, settings and paths for every stage
│   └── namespaces.yaml               # base IRI and prefixes
├── prompts/                          # every prompt sent to a model
│   ├── extract.md                    # S3: entity extraction
│   ├── critic.md                     # S4: verification critic
│   ├── cq_generate.md                # drafting the competency questions
│   └── cq_to_sparql.md               # turning them into SPARQL
├── schemas/
│   └── extraction.py                 # the pydantic schema the model output is constrained to
├── src/aiact_onto/                   # one module per stage
│   ├── ingest.py                     # S1
│   ├── chunk.py                      # S2
│   ├── extract.py                    # S3
│   ├── verify.py                     # S4
│   ├── curate.py                     # S5, an interactive review CLI
│   ├── build.py                      # S6
│   ├── validate.py                   # S7
│   ├── evaluate.py                   # S8
│   ├── package.py                    # collects the deliverables into output/
│   └── utils/                        # chunk rendering, diagram generation
├── data/                             # every intermediate artefact, in pipeline order
│   ├── raw/                          # the EUR-Lex snapshot and its retrieval time
│   ├── chunks/                       # one JSON per article or annex (25)
│   ├── extracted/                    # raw model output, one JSONL per chunk (181 records)
│   ├── verified/                     # entities.jsonl (160) and rejected.jsonl
│   ├── curated/                      # decisions.jsonl (append-only log) and entities.json (134)
│   └── gold/                         # manually annotated sample for evaluation (empty)
├── ontology/
│   ├── aiact-core.ttl                # hand-authored TBox: classes, properties, key axioms
│   ├── aiact-alignments.ttl          # hand-authored mappings to AIRO, VAIR, DPV and ELI
│   ├── aiact-generated.ttl           # built by S6 from the curated records; do not edit
│   ├── aiact.ttl                     # top-level ontology, imports the three above
│   ├── aiact-merged.owl              # the release file, for Protégé
│   ├── aiact-with-examples.ttl       # the release file plus the test individuals
│   ├── examples.ttl                  # fictional systems the reasoner classifies
│   └── imports/                      # local copies of the reused ontologies
├── shapes/
│   └── aiact-shapes.ttl              # SHACL: every entity needs a label, definition and reference
├── queries/                          # the competency questions as executable SPARQL (6)
├── tests/                            # 72 tests over grounding, curation, build, validation and the CQs
├── reports/
│   ├── validation.json               # the six S7 checks and their results
│   ├── metrics.json                  # S8 metrics
│   ├── curation_report.md            # what the review step changed, and why it mattered
│   ├── shacl_report.txt              # pySHACL output
│   ├── robot_report.tsv              # ROBOT quality report
│   ├── reasoner.txt                  # which reasoner ran, and its output
│   └── screenshots/                  # Protégé screenshot and generated hierarchy diagrams
└── docs/
    ├── documentation.pdf             # the two-page note
    ├── documentation.tex             # its source
    └── html/                         # browsable ontology documentation from pyLODE
```

Files ignored by git: the model response caches (`data/*/.cache`), the S7 scratch folder
(`reports/.work/`), the submission bundle (`output/`) and LaTeX build files.


## Contact Information
Nathan Schneider Gavenski \
https://nathangavenski.github.io/ \
nathangavenski@gmail.com