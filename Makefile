.PHONY: all ingest chunk extract verify curate build validate evaluate diagrams docs package test clean

# Every stage runs through uv, so the pinned environment is used without activating it by hand.
PY := uv run python
UV := uv run

# Rebuilds the ontology from the committed snapshot and the committed curation decisions.
# Curation is interactive, so it is not part of this target.
all: ingest chunk extract verify build validate evaluate diagrams docs

ingest:   ; $(PY) -m aiact_onto.ingest
chunk:    ; $(PY) -m aiact_onto.chunk
extract:  ; $(PY) -m aiact_onto.extract
verify:   ; $(PY) -m aiact_onto.verify
curate:   ; $(PY) -m aiact_onto.curate
build:    ; $(PY) -m aiact_onto.build
validate: ; $(PY) -m aiact_onto.validate
diagrams: ; $(PY) -m aiact_onto.utils.diagram
evaluate: ; $(PY) -m aiact_onto.evaluate
docs:     ; $(UV) pylode ontology/aiact-merged.owl -o docs/html/index.html
	 cd docs && pdflatex -interaction=nonstopmode documentation.tex && rm -f documentation.aux documentation.log documentation.out

# Collects the assignment outputs into output/ and output.zip.
package:  ; $(PY) -m aiact_onto.package --force --zip

test:     ; $(UV) --extra dev pytest -q

# Removes generated artefacts but keeps the raw snapshot, the curation decisions and the reports.
clean:
	rm -rf reports/.work ontology/aiact-generated.ttl ontology/aiact-merged.owl ontology/aiact-with-examples.ttl
