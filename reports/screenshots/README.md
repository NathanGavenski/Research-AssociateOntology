# Evidence images

`make diagrams` rebuilds these from `reports/.work/reasoned.owl`, the ontology after HermiT has run,
so they can be regenerated from the committed data (`src/aiact_onto/utils/diagram.py`).

| File | What it shows |
|---|---|
| `class_hierarchy.png` | The hand-authored TBox, with a count of the classes the pipeline added under each core class |
| `class_hierarchy_full.png` | The same hierarchy including every generated class |
| `inferred_classification.png` | What HermiT concluded about the test individuals: dashed red edges are inferred, never asserted |

`inferred_classification.png` is the one that demonstrates the reasoning. The CV screening tool is
asserted only as an AI system used in the employment area, and HermiT concludes it is an Annex III
high-risk AI system; the social scoring platform is likewise concluded to be a prohibited AI system;
the chat assistant is neither, and stays unclassified.

## Still to capture

README §7 asks for screenshots taken in Protégé, which these generated diagrams do not replace:

1. The class hierarchy in the Protégé tree view.
2. The reasoner result, showing HermiT reporting no unsatisfiable classes.
3. The inferred hierarchy, with the example systems under their inferred classes.

Open `ontology/aiact-with-examples.ttl` for those, not `aiact-merged.owl`: the merged file holds no
test individuals, so there would be nothing to classify.
