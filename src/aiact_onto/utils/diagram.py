"""Draws the class hierarchy and the reasoner's conclusions as PNGs (README §7, reports/screenshots).

These are generated from the reasoned ontology with Graphviz, so they can be rebuilt from the
committed data. They are evidence of what the reasoner concluded, not a substitute for the Protégé
screenshots the deliverables ask for.
"""
import shutil
import subprocess
from pathlib import Path

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

AIACT = Namespace("https://w3id.org/aiact-poc#")
EXAMPLES = Namespace("https://w3id.org/aiact-poc/examples#")
RISK_CLASSES = ("ProhibitedAISystem", "HighRiskAISystem", "AnnexIHighRiskAISystem",
                "AnnexIIIHighRiskAISystem", "LimitedRiskAISystem", "MinimalRiskAISystem")
STYLE = {
    "hand-authored": 'fillcolor="#dbe9f6"',
    "curated": 'fillcolor="#e8f4e4"',
    "individual": 'fillcolor="#fdf3d8", shape=ellipse',
    "inferred": 'fillcolor="#f8d7da"',
}


def name(iri: URIRef) -> str:
    """Returns the local name of an IRI."""
    return str(iri).split("#")[-1]


def label(graph: Graph, iri: URIRef, limit: int = 46) -> str:
    """Returns an entity's English label, shortened so the diagram stays readable.

    Args:
        graph (Graph): The ontology.
        iri (URIRef): The entity.
        limit (int): The longest label to keep whole.

    Returns:
        str: The label, or the local name when the entity has none.
    """
    for text in graph.objects(iri, RDFS.label):
        text = str(text)
        return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return name(iri)


def hand_authored(graph: Graph, iri: URIRef) -> bool:
    """Says whether the core file authored this entity, rather than the pipeline."""
    return any(str(s) == "hand-authored" for s in graph.objects(iri, AIACT.curationStatus))


def hierarchy_dot(graph: Graph, core_only: bool = False) -> str:
    """Builds the asserted class hierarchy of the aiact namespace.

    Args:
        graph (Graph): The ontology.
        core_only (bool): True to draw only the hand-authored classes, noting how many classes the
            pipeline added under each of them, which is the readable view of the skeleton.

    Returns:
        str: The Graphviz source.
    """
    lines = ['digraph hierarchy {', '  rankdir=LR; node [style=filled, shape=box, fontname="Helvetica", fontsize=10];',
             '  edge [color="#666666", arrowhead=empty];']
    edges = [(c, p) for c, p in graph.subject_objects(RDFS.subClassOf)
             if isinstance(c, URIRef) and isinstance(p, URIRef)
             and str(c).startswith(str(AIACT)) and str(p).startswith(str(AIACT))]
    curated_children: dict[URIRef, int] = {}
    for child, parent in edges:
        if not hand_authored(graph, child):
            curated_children[parent] = curated_children.get(parent, 0) + 1

    drawn = set()
    for child, parent in edges:
        if core_only and not hand_authored(graph, child):
            continue
        for node in (child, parent):
            if node not in drawn:
                style = STYLE["hand-authored"] if hand_authored(graph, node) else STYLE["curated"]
                text = label(graph, node)
                if core_only and curated_children.get(node):
                    text += f"\\n(+{curated_children[node]} from the pipeline)"
                lines.append(f'  "{name(node)}" [label="{text}", {style}];')
                drawn.add(node)
        lines.append(f'  "{name(parent)}" -> "{name(child)}";')
    lines.append('  subgraph cluster_key { label="key"; fontname="Helvetica"; fontsize=10; '
                 f'"hand-authored TBox" [{STYLE["hand-authored"]}]; "from the pipeline" [{STYLE["curated"]}]; }}')
    lines.append("}")
    return "\n".join(lines)


def classification_dot(graph: Graph) -> str:
    """Builds the picture of what the reasoner concluded about the test individuals.

    Args:
        graph (Graph): The reasoned ontology, including the examples.

    Returns:
        str: The Graphviz source.
    """
    lines = ['digraph classification {', '  rankdir=LR; node [style=filled, shape=box, fontname="Helvetica", fontsize=10];',
             '  edge [fontname="Helvetica", fontsize=9];']
    for individual in sorted(graph.subjects(RDF.type, OWL.NamedIndividual)):
        if not str(individual).startswith(str(EXAMPLES)):
            continue
        lines.append(f'  "{name(individual)}" [label="{label(graph, individual)}", {STYLE["individual"]}];')
        for prop, style in ((AIACT.usesPractice, "uses practice"), (AIACT.usedInArea, "used in area"),
                            (AIACT.hasRole, "has role"), (AIACT.roleInRelationTo, "role in relation to")):
            for target in graph.objects(individual, prop):
                lines.append(f'  "{name(target)}" [label="{label(graph, target)}", {STYLE["individual"]}];')
                lines.append(f'  "{name(individual)}" -> "{name(target)}" [label="{style}", color="#666666"];')
        for cls in graph.objects(individual, RDF.type):
            if isinstance(cls, URIRef) and name(cls) in RISK_CLASSES:
                lines.append(f'  "{name(cls)}" [label="{label(graph, cls)}", {STYLE["inferred"]}];')
                lines.append(f'  "{name(individual)}" -> "{name(cls)}" '
                             f'[label="inferred type", style=dashed, color="#c0392b"];')
    lines.append('  subgraph cluster_key { label="key"; fontname="Helvetica"; fontsize=10; '
                 f'"asserted" [{STYLE["individual"]}]; "inferred by HermiT" [{STYLE["inferred"]}]; }}')
    lines.append("}")
    return "\n".join(lines)


def render(dot: str, out: Path) -> Path:
    """Writes the Graphviz source and renders it to PNG.

    Args:
        dot (str): The Graphviz source.
        out (Path): The PNG to write; the source is written beside it with a .dot suffix.

    Returns:
        Path: The PNG that was written.

    Raises:
        RuntimeError: If Graphviz is not installed.
    """
    executable = shutil.which("dot")
    if executable is None:
        raise RuntimeError("Graphviz 'dot' is not on PATH; install Graphviz to render the diagrams")
    out.parent.mkdir(parents=True, exist_ok=True)
    source = out.with_suffix(".dot")
    source.write_text(dot, encoding="utf-8")
    subprocess.run([executable, "-Tpng", str(source), "-o", str(out)], check=True)
    return out


if __name__ == "__main__":
    reasoned = Path("reports/.work/reasoned.owl")
    if not reasoned.is_file():
        raise SystemExit("reports/.work/reasoned.owl not found; run 'make validate' first")
    graph = Graph().parse(reasoned)
    out = Path("reports/screenshots")
    print(render(hierarchy_dot(graph, core_only=True), out / "class_hierarchy.png"))
    print(render(hierarchy_dot(graph), out / "class_hierarchy_full.png"))
    print(render(classification_dot(graph), out / "inferred_classification.png"))
