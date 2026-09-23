import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import owlrl
import yaml
from pyshacl import validate as shacl_validate
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

DEFAULTS = {
    "ontology": "ontology/aiact-merged.owl",
    "examples": "ontology/examples.ttl",
    "shapes": "shapes/aiact-shapes.ttl",
    "combined": "ontology/aiact-with-examples.ttl",
    "out_dir": "reports",
    "engine": "auto",              # auto | robot | python
    "robot": "robot",
    "docker_image": "obolibrary/robot",
    "reasoner": "hermit",
}
EXAMPLES = Namespace("https://w3id.org/aiact-poc/examples#")
RISK_CLASSES = {"ProhibitedAISystem", "HighRiskAISystem", "AnnexIHighRiskAISystem", "AnnexIIIHighRiskAISystem",
                "LimitedRiskAISystem", "MinimalRiskAISystem"}
# The risk classes each example individual must be inferred into (README §5.6).
EXPECTED = {
    "CVScreeningTool": {"AnnexIIIHighRiskAISystem", "HighRiskAISystem"},
    "SocialScoringPlatform": {"ProhibitedAISystem"},
    "ChatAssistant": set(),
}


class Validate:
    def __init__(self, config: str | Path = "./config/pipeline.yaml"):
        """Loads the validation settings and works out which reasoner is available.

        Args:
            config (str | Path): The path to the pipeline YAML file; its "validate" section overrides DEFAULTS.
        """
        self.config = self.load_config(config)
        self.ontology = Path(self.config["ontology"])
        self.examples = Path(self.config["examples"])
        self.shapes = Path(self.config["shapes"])
        self.combined_file = Path(self.config["combined"])
        self.path = Path(self.config["out_dir"])
        self.work = self.path / ".work"
        self.robot = self.robot_command() if self.config["engine"] in ("auto", "robot") else None
        self.engine = "robot" if self.robot else "python"

    def load_config(self, path: str | Path) -> dict[str, Any]:
        """Loads the "validate" section of the pipeline config.

        Args:
            path (str | Path): The path to the pipeline YAML file.

        Returns:
            dict[str, Any]: DEFAULTS updated with any values set in the file.
        """
        try:
            with open(path, "r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
        except FileNotFoundError:
            config = {}
        return {**DEFAULTS, **(config.get("validate") or {})}

    def robot_command(self) -> list[str] | None:
        """Finds ROBOT, locally or in a container, so no Java has to be installed.

        Returns:
            list[str] | None: The command prefix to run ROBOT with, or None if neither is available.
        """
        local = shutil.which(self.config["robot"])
        if local:
            return [local]
        docker = shutil.which("docker")
        if docker and subprocess.run([docker, "info"], capture_output=True).returncode == 0:
            return [docker, "run", "--rm", "-v", f"{Path.cwd()}:/work", "-w", "/work",
                    self.config["docker_image"], "robot"]
        return None

    def arg(self, path: Path) -> str:
        """Writes a path the way ROBOT needs it, with forward slashes for the container.

        Args:
            path (Path): A path relative to the repository root.

        Returns:
            str: The path with forward slashes.
        """
        return path.as_posix()

    def run_robot(self, *args: str) -> subprocess.CompletedProcess:
        """Runs one ROBOT command.

        Args:
            *args (str): The ROBOT arguments, with paths relative to the repository root.

        Returns:
            subprocess.CompletedProcess: The finished process, with output captured.
        """
        return subprocess.run([*self.robot, *args], capture_output=True, text=True)

    def combined(self) -> Path:
        """Writes the ontology and the test individuals into one file.

        The file is kept in ontology/ rather than in the scratch directory, because it is what you
        open in Protégé to see the example systems classified.

        Returns:
            Path: The combined Turtle file.
        """
        graph = Graph().parse(self.ontology, format="xml")
        if self.examples.is_file():
            graph.parse(self.examples, format="turtle")
        self.combined_file.parent.mkdir(parents=True, exist_ok=True)
        graph.serialize(destination=self.combined_file, format="turtle")
        self.work.mkdir(parents=True, exist_ok=True)
        return self.combined_file

    def check_parses(self) -> dict[str, Any]:
        """Checks that the released ontology parses as RDF/XML.

        Returns:
            dict[str, Any]: The check result, with the triple count.
        """
        try:
            graph = Graph().parse(self.ontology, format="xml")
        except Exception as error:
            return {"check": "parses", "status": "fail", "detail": str(error)}
        return {"check": "parses", "status": "pass", "detail": f"{len(graph)} triples"}

    def check_profile(self) -> dict[str, Any]:
        """Checks the ontology is in the OWL 2 DL profile (ROBOT only).

        Returns:
            dict[str, Any]: The check result.
        """
        if not self.robot:
            return {"check": "profile", "status": "skipped", "detail": "needs ROBOT (local or via Docker)"}
        result = self.run_robot("validate-profile", "--profile", "DL", "--input", self.arg(self.ontology))
        output = (result.stdout + result.stderr).strip()
        status = "pass" if result.returncode == 0 else "fail"
        return {"check": "profile", "status": status, "detail": output.splitlines()[0] if output else ""}

    def reason_robot(self, source: Path) -> tuple[Graph | None, str]:
        """Runs HermiT through ROBOT and returns the inferred graph.

        Args:
            source (Path): The combined ontology and examples file.

        Returns:
            tuple[Graph | None, str]: The reasoned graph (None if the reasoner refused) and ROBOT's output.
        """
        out = self.work / "reasoned.owl"
        result = self.run_robot("reason", "--reasoner", self.config["reasoner"],
                                "--axiom-generators", "SubClass EquivalentClass ClassAssertion",
                                "--input", self.arg(source), "--output", self.arg(out))
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0 or not out.is_file():
            return None, output
        return Graph().parse(out), output

    def reason_python(self, source: Path) -> tuple[Graph, str]:
        """Applies the OWL 2 RL rules with owlrl, as a fallback when ROBOT is unavailable.

        The rules cover the existential restrictions the risk classes are defined with, but they are
        not a full DL reasoner: they cannot prove a class unsatisfiable.

        Args:
            source (Path): The combined ontology and examples file.

        Returns:
            tuple[Graph, str]: The expanded graph and a note about what was run.
        """
        graph = Graph().parse(source, format="turtle")
        owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(graph)
        return graph, "owlrl OWL 2 RL closure (no unsatisfiability check)"

    def check_consistency(self, graph: Graph | None, output: str) -> dict[str, Any]:
        """Reports whether the reasoner accepted the ontology.

        Args:
            graph (Graph | None): The reasoned graph, or None when the reasoner refused.
            output (str): The reasoner's own output.

        Returns:
            dict[str, Any]: The check result.
        """
        if graph is None:
            return {"check": "consistency", "status": "fail", "detail": output[-400:]}
        nothing = [s for s in graph.subjects(RDF.type, OWL.Nothing) if s != OWL.Nothing]
        unsatisfiable = [s for s in graph.subjects(OWL.equivalentClass, OWL.Nothing) if s != OWL.Nothing]
        if nothing or unsatisfiable:
            names = [str(s).split("#")[-1] for s in (*nothing, *unsatisfiable)]
            return {"check": "consistency", "status": "fail", "detail": f"unsatisfiable: {', '.join(names)}"}
        detail = "consistent, no unsatisfiable classes" if self.engine == "robot" else \
                 "no contradiction found by the OWL 2 RL rules"
        return {"check": "consistency", "status": "pass", "detail": detail}

    def check_classification(self, graph: Graph | None) -> dict[str, Any]:
        """Checks the test individuals land in the risk class they should (README §5.6).

        Args:
            graph (Graph | None): The reasoned graph.

        Returns:
            dict[str, Any]: The check result, naming any individual that was classified wrongly.
        """
        if graph is None:
            return {"check": "classification", "status": "skipped", "detail": "no reasoned graph"}
        problems, seen = [], {}
        for name, expected in EXPECTED.items():
            types = set()
            for direct in graph.objects(EXAMPLES[name], RDF.type):  # HermiT asserts the most specific type only
                types.update(graph.transitive_objects(direct, RDFS.subClassOf))
            inferred = {str(t).split("#")[-1] for t in types if isinstance(t, URIRef)} & RISK_CLASSES
            seen[name] = sorted(inferred)
            if not expected.issubset(inferred):
                problems.append(f"{name}: expected {sorted(expected)}, got {sorted(inferred)}")
            elif not expected and inferred:
                problems.append(f"{name}: expected no risk class, got {sorted(inferred)}")
        status = "fail" if problems else "pass"
        return {"check": "classification", "status": status, "detail": "; ".join(problems) or json.dumps(seen)}

    def check_shacl(self) -> dict[str, Any]:
        """Runs the annotation-completeness shapes with pySHACL, which needs no Java.

        Returns:
            dict[str, Any]: The check result, with the number of violations.
        """
        if not self.shapes.is_file():
            return {"check": "shacl", "status": "skipped", "detail": f"{self.shapes} not found"}
        data = Graph().parse(self.ontology, format="xml")
        conforms, _, text = shacl_validate(data, shacl_graph=Graph().parse(self.shapes, format="turtle"),
                                           inference="none")
        (self.path / "shacl_report.txt").write_text(text, encoding="utf-8")
        violations = text.count("Constraint Violation")
        return {"check": "shacl", "status": "pass" if conforms else "fail",
                "detail": "conforms" if conforms else f"{violations} violations, see reports/shacl_report.txt"}

    def check_report(self) -> dict[str, Any]:
        """Runs ROBOT's own quality report and keeps it in reports/ (ROBOT only).

        Returns:
            dict[str, Any]: The check result, which fails only on ERROR-level issues.
        """
        if not self.robot:
            return {"check": "robot_report", "status": "skipped", "detail": "needs ROBOT (local or via Docker)"}
        out = self.path / "robot_report.tsv"
        result = self.run_robot("report", "--input", self.arg(self.ontology), "--fail-on", "ERROR",
                                "--output", self.arg(out))
        levels = {"ERROR": 0, "WARN": 0, "INFO": 0}
        if out.is_file():
            for line in out.read_text(encoding="utf-8").splitlines()[1:]:
                level = line.split("\t")[0]
                if level in levels:
                    levels[level] += 1
        status = "pass" if result.returncode == 0 else "fail"
        return {"check": "robot_report", "status": status,
                "detail": f"{levels['ERROR']} errors, {levels['WARN']} warnings, {levels['INFO']} info"}

    def run(self) -> list[dict[str, Any]]:
        """Runs every check and writes reports/validation.json and reports/reasoner.txt.

        Returns:
            list[dict[str, Any]]: One result per check.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        source = self.combined()
        graph, output = self.reason_robot(source) if self.robot else self.reason_python(source)

        results = [
            self.check_parses(),
            self.check_profile(),
            self.check_consistency(graph, output),
            self.check_classification(graph),
            self.check_shacl(),
            self.check_report(),
        ]
        (self.path / "reasoner.txt").write_text(
            f"engine: {self.engine}\nreasoner: {self.config['reasoner'] if self.engine == 'robot' else 'owlrl'}\n\n{output}\n",
            encoding="utf-8")
        with open(self.path / "validation.json", "w", encoding="utf-8") as file:
            json.dump({"engine": self.engine, "checks": results}, file, indent=2)
        return results


if __name__ == "__main__":
    validator = Validate()
    results = validator.run()
    print(f"engine: {validator.engine}")
    for result in results:
        mark = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}[result["status"]]
        print(f"  [{mark}] {result['check']}: {result['detail'][:110]}")
    raise SystemExit(1 if any(r["status"] == "fail" for r in results) else 0)
