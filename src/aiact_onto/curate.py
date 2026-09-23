import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, get_args

import typer
import yaml
from rapidfuzz import fuzz
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from aiact_onto.utils.render import render
from schemas.extraction import ExtractedEntity

DEFAULTS = {
    "in_file": "data/verified/entities.jsonl",
    "out_dir": "data/curated",
    "chunks_dir": "data/chunks",
}
ENTITY_TYPES = list(get_args(ExtractedEntity.model_fields["entity_type"].annotation))
EDITABLE = ("term", "entity_type", "label", "definition", "bearer", "applies_to")
NULLABLE = ("bearer", "applies_to")
KEPT = ("accept", "edit")
VERDICT_STYLE = {"supported": "green", "partially": "yellow", "unsupported": "red", None: "magenta"}
MENU = "[a]ccept [e]dit [r]eject [m]erge [s]kip [v]iew article [u]ndo [q]uit"


def record_id(record: dict[str, Any]) -> str:
    """Builds a stable id for a verified record from the fields a reviewer judges.

    The id changes when S4 produces a different record, so a changed record is reviewed again.

    Args:
        record (dict[str, Any]): The verified record.

    Returns:
        str: The first 12 hex characters of the SHA-256 of the record's key fields.
    """
    key = {k: record.get(k) for k in EDITABLE if k != "label"}
    key["article_refs"] = sorted(record.get("article_refs") or [record["article_ref"]])
    payload = json.dumps(key, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def priority(record: dict[str, Any]) -> int:
    """Ranks a record for review; lower comes first (README §6, S5).

    Order: not judged by the critic, unsupported, verdicts that disagree across merged sources,
    partially supported, supported.

    Args:
        record (dict[str, Any]): The verified record.

    Returns:
        int: The rank, from 0 to 4.
    """
    verdicts = {s["critic_verdict"] for s in record.get("sources") or []} | {record["critic_verdict"]}
    if None in verdicts:
        return 0
    if "unsupported" in verdicts:
        return 1
    if len(verdicts) > 1:
        return 2
    return 3 if record["critic_verdict"] == "partially" else 4


def replay(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Replays the decision log into the current decision per record.

    A later decision on a record replaces an earlier one, and "undo" returns the record to undecided.

    Args:
        decisions (list[dict[str, Any]]): The log entries, oldest first.

    Returns:
        dict[str, dict[str, Any]]: The current decision, keyed by record id.
    """
    state = {}
    for decision in decisions:
        if decision["action"] == "undo":
            state.pop(decision["id"], None)
        else:
            state[decision["id"]] = decision
    return state


def resolve(rid: str, state: dict[str, dict[str, Any]]) -> str | None:
    """Follows a chain of merges to the record it ends at.

    Args:
        rid (str): The id of the merge target.
        state (dict[str, dict[str, Any]]): The current decisions.

    Returns:
        str | None: The id at the end of the chain, or None if the chain loops.
    """
    seen = set()
    while rid in state and state[rid]["action"] == "merge":
        if rid in seen:
            return None
        seen.add(rid)
        rid = state[rid]["target"]
    return rid


def build(records: dict[str, dict[str, Any]], state: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Builds the curated entities from the verified records and the current decisions.

    Accepted and edited records become entities; merged records add their references and sources
    to the entity they were merged into.

    Args:
        records (dict[str, dict[str, Any]]): The verified records, keyed by record id.
        state (dict[str, dict[str, Any]]): The current decisions, keyed by record id.

    Returns:
        tuple[list[dict[str, Any]], list[str]]: The entities sorted by type and term, and the ids of
            merged records whose target is no longer kept.
    """
    entities = {}
    for rid, decision in state.items():
        if rid not in records or decision["action"] not in KEPT:
            continue
        record = records[rid]
        fields = {k: record[k] for k in EDITABLE}
        fields.update({k: new for k, (_, new) in decision.get("changes", {}).items()})
        entities[rid] = {
            "id": rid,
            **fields,
            "article_refs": list(record.get("article_refs") or [record["article_ref"]]),
            "sources": [
                {k: s[k] for k in ("article_ref", "chunk_id", "eli_uri", "source_quote")}
                for s in record.get("sources") or [record]
            ],
            "confidence": record["confidence"],
            "critic_verdict": record["critic_verdict"],
            "model": record.get("model"),
            "curation_status": "edited" if decision["action"] == "edit" else "accepted",
            "reviewer": decision.get("reviewer"),
            "decided_at": decision["timestamp"],
            "merged_ids": [],
        }

    orphans = []
    for rid, decision in state.items():
        if rid not in records or decision["action"] != "merge":
            continue
        target = entities.get(resolve(decision["target"], state))
        if target is None:
            orphans.append(rid)
            continue
        record = records[rid]
        for ref in record.get("article_refs") or [record["article_ref"]]:
            if ref not in target["article_refs"]:
                target["article_refs"].append(ref)
        target["sources"].extend(
            {k: s[k] for k in ("article_ref", "chunk_id", "eli_uri", "source_quote")}
            for s in record.get("sources") or [record]
        )
        target["merged_ids"].append(rid)

    ordered = sorted(entities.values(), key=lambda e: (e["entity_type"], e["term"].lower()))
    return ordered, orphans


class Curate:
    def __init__(self, config: str | Path = "./config/pipeline.yaml", console: Console | None = None):
        """Loads the curation settings.

        Args:
            config (str | Path): The path to the pipeline YAML file; its "curate" section overrides DEFAULTS.
            console (Console | None): The rich console to write to, or None for the terminal.
        """
        self.config = self.load_config(config)
        self.in_file = Path(self.config["in_file"])
        self.chunks_dir = Path(self.config["chunks_dir"])
        self.path = Path(self.config["out_dir"])
        self.decisions_file = self.path / "decisions.jsonl"
        self.entities_file = self.path / "entities.json"
        self.console = console or Console()
        self.chunk_files: dict[str, Path] | None = None

    def load_config(self, path: str | Path) -> dict[str, Any]:
        """Loads the "curate" section of the pipeline config.

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
        return {**DEFAULTS, **(config.get("curate") or {})}

    def load_records(self) -> dict[str, dict[str, Any]]:
        """Loads the verified records written by S4.

        Returns:
            dict[str, dict[str, Any]]: The records keyed by record id, in S4 order.
        """
        with open(self.in_file, "r", encoding="utf-8") as file:
            records = [json.loads(line) for line in file if line.strip()]
        return {record_id(r): r for r in records}

    def load_decisions(self) -> list[dict[str, Any]]:
        """Loads the decision log.

        Returns:
            list[dict[str, Any]]: The log entries, oldest first; empty if nothing was decided yet.
        """
        if not self.decisions_file.is_file():
            return []
        with open(self.decisions_file, "r", encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]

    def log(self, decision: dict[str, Any]) -> None:
        """Appends a decision to the log and rebuilds the curated entities.

        Args:
            decision (dict[str, Any]): The log entry.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.decisions_file, "a", encoding="utf-8") as file:
            file.write(json.dumps(decision, ensure_ascii=False) + "\n")
        self.export()

    def export(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Rebuilds "entities.json" from the verified records and the decision log.

        Returns:
            tuple[list[dict[str, Any]], list[str]]: The entities written and the orphaned merge ids.
        """
        entities, orphans = build(self.load_records(), replay(self.load_decisions()))
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.entities_file, "w", encoding="utf-8") as file:
            json.dump(entities, file, ensure_ascii=False, indent=2)
        return entities, orphans

    def queue(self, redo: bool = False, verdicts: list[str] | None = None) -> list[dict[str, Any]]:
        """Lists the records to review, most doubtful first.

        Args:
            redo (bool): Whether to include records that already have a decision.
            verdicts (list[str] | None): Only include records with these critic verdicts
                ("none" for records the critic could not judge), or None for all.

        Returns:
            list[dict[str, Any]]: The records in review order.
        """
        state = replay(self.load_decisions())
        records = [
            r for rid, r in self.load_records().items()
            if (redo or rid not in state) and (not verdicts or str(r["critic_verdict"]).lower() in verdicts)
        ]
        return sorted(records, key=priority)

    def chunk_for(self, chunk_id: str) -> dict[str, Any] | None:
        """Finds a chunk by its id (e.g. "art_5" or "anx_III").

        Args:
            chunk_id (str): The chunk id stored with each source.

        Returns:
            dict[str, Any] | None: The chunk, or None if no chunk file has that id.
        """
        if self.chunk_files is None:
            self.chunk_files = {}
            for file in self.chunks_dir.glob("*.json"):
                with open(file, "r", encoding="utf-8") as f:
                    self.chunk_files[json.load(f)["id"]] = file
        file = self.chunk_files.get(chunk_id)
        if file is None:
            return None
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)

    def show(self, record: dict[str, Any], index: int, total: int) -> None:
        """Prints a record next to its source quotes and critic verdicts.

        Args:
            record (dict[str, Any]): The verified record.
            index (int): Its position in the queue, from 1.
            total (int): The length of the queue.
        """
        fields = Table.grid(padding=(0, 2))
        fields.add_column(style="bold")
        fields.add_column()
        fields.add_row("Type", Text(record["entity_type"]))
        fields.add_row("Label", Text(record["label"]))
        fields.add_row("Definition", Text(record["definition"]))
        fields.add_row("Bearer", Text(str(record["bearer"])))
        fields.add_row("Applies to", Text(str(record["applies_to"])))
        fields.add_row("Confidence", Text(f"{record['confidence']:.2f}"))
        fields.add_row("References", Text(", ".join(record.get("article_refs") or [record["article_ref"]])))

        sources = []
        for source in record.get("sources") or [record]:
            verdict = source["critic_verdict"]
            text = Text()
            text.append(f"\n{source['article_ref']}  ", style="bold")
            text.append(verdict or "not judged", style=VERDICT_STYLE.get(verdict, "magenta"))
            text.append(f"\n“{source['source_quote']}”", style="italic")
            if source.get("critic_reason"):
                text.append(f"\nCritic: {source['critic_reason']}", style="dim")
            sources.append(text)

        verdict = record["critic_verdict"]
        title = f"[{index}/{total}] {escape(record['term'])}"
        subtitle = f"[{VERDICT_STYLE.get(verdict, 'magenta')}]{verdict or 'not judged'}[/] · id {record_id(record)}"
        self.console.print(Panel(Group(fields, *sources), title=title, subtitle=subtitle, title_align="left"))

    def view(self, record: dict[str, Any]) -> None:
        """Prints the full text of every article or annex the record cites.

        Args:
            record (dict[str, Any]): The verified record.
        """
        chunk_ids = dict.fromkeys(s["chunk_id"] for s in record.get("sources") or [record])
        for chunk_id in chunk_ids:
            chunk = self.chunk_for(chunk_id)
            if chunk is None:
                self.console.print(f"No chunk file found for {escape(chunk_id)}.", style="red")
                continue
            self.console.print(Panel(Text(render(chunk)), title=escape(chunk_id), title_align="left"))

    def edit(self, record: dict[str, Any]) -> dict[str, list[Any]]:
        """Asks for a new value of each editable field; Enter keeps the current one.

        Args:
            record (dict[str, Any]): The verified record.

        Returns:
            dict[str, list[Any]]: The changed fields, as {field: [old, new]}.
        """
        self.console.print("Enter keeps the current value; '-' clears bearer or applies_to.", style="dim")
        changes = {}
        for field in EDITABLE:
            current = record[field]
            if field == "entity_type":
                new = Prompt.ask(field, choices=ENTITY_TYPES, default=current, console=self.console)
            elif field == "definition":
                raw = Prompt.ask("definition (Enter keeps the one shown above)", default="", show_default=False, console=self.console)
                new = raw.strip() or current
            elif field in NULLABLE:
                raw = Prompt.ask(field, default=current or "", console=self.console).strip()
                new = None if raw in ("", "-") else raw
            else:
                new = Prompt.ask(field, default=current, console=self.console).strip() or current
            if new != current:
                changes[field] = [current, new]
        return changes

    def pick_target(self, record: dict[str, Any], rid: str) -> str | None:
        """Asks which kept entity to merge a record into, suggesting the most similar ones.

        Args:
            record (dict[str, Any]): The record being merged.
            rid (str): Its id.

        Returns:
            str | None: The id of the target entity, or None if the reviewer cancelled.
        """
        entities, _ = build(self.load_records(), replay(self.load_decisions()))
        candidates = [e for e in entities if e["id"] != rid]
        if not candidates:
            self.console.print("No accepted entities to merge into yet.", style="yellow")
            return None

        def score(entity: dict[str, Any]) -> tuple[bool, float]:
            similarity = fuzz.token_sort_ratio(entity["term"].lower(), record["term"].lower())
            return entity["entity_type"] == record["entity_type"], similarity

        candidates = sorted(candidates, key=score, reverse=True)[:8]
        table = Table("#", "Term", "Type", "Bearer", "References")
        for n, entity in enumerate(candidates, 1):
            table.add_row(str(n), Text(entity["term"]), entity["entity_type"], Text(str(entity["bearer"])),
                          Text(", ".join(entity["article_refs"])))
        self.console.print(table)
        choice = Prompt.ask("Merge into # (0 cancels)", choices=[str(n) for n in range(len(candidates) + 1)],
                            show_choices=False, console=self.console)
        return None if choice == "0" else candidates[int(choice) - 1]["id"]

    def review(self, reviewer: str, redo: bool = False, verdicts: list[str] | None = None) -> Counter:
        """Runs the interactive review; every decision is saved as soon as it is made.

        Args:
            reviewer (str): The name recorded with each decision.
            redo (bool): Whether to review records that already have a decision.
            verdicts (list[str] | None): Only review records with these critic verdicts, or None for all.

        Returns:
            Counter: The number of decisions of each kind made in this session.
        """
        queue = self.queue(redo, verdicts)
        ids = [record_id(r) for r in queue]
        history: list[str] = []
        made: Counter = Counter()
        if not queue:
            self.console.print("Nothing to review.", style="green")
            return made

        i = 0
        while i < len(queue):
            record, rid = queue[i], ids[i]
            self.show(record, i + 1, len(queue))
            choice = Prompt.ask(escape(MENU), choices=list("aermsvuq"), show_choices=False, console=self.console)

            if choice == "q":
                break
            if choice == "s":
                i += 1
                continue
            if choice == "v":
                self.view(record)
                continue
            if choice == "u":
                if not history:
                    self.console.print("Nothing to undo in this session.", style="yellow")
                    continue
                last = history.pop()
                self.log({"id": last, "action": "undo", "reviewer": reviewer, "timestamp": now()})
                made["undo"] += 1
                i = ids.index(last)
                continue

            decision = {
                "id": rid,
                "term": record["term"],
                "entity_type": record["entity_type"],
                "critic_verdict": record["critic_verdict"],
                "reviewer": reviewer,
            }
            if choice == "a":
                decision["action"] = "accept"
            elif choice == "e":
                changes = self.edit(record)
                decision["action"] = "edit" if changes else "accept"
                if changes:
                    decision["changes"] = changes
            elif choice == "r":
                decision["action"] = "reject"
                reason = Prompt.ask("Reason (optional)", default="", show_default=False, console=self.console).strip()
                if reason:
                    decision["reason"] = reason
            elif choice == "m":
                target = self.pick_target(record, rid)
                if target is None:
                    continue
                decision["action"] = "merge"
                decision["target"] = target

            decision["timestamp"] = now()
            self.log(decision)
            made[decision["action"]] += 1
            history.append(rid)
            i += 1

        return made

    def status(self) -> None:
        """Prints review progress and how the reviewer's decisions compare with the critic's verdicts."""
        records = self.load_records()
        state = replay(self.load_decisions())
        entities, orphans = build(records, state)
        current = {rid: d for rid, d in state.items() if rid in records}
        stale = len(state) - len(current)

        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="bold")
        summary.add_column(justify="right")
        summary.add_row("Verified records", str(len(records)))
        summary.add_row("Decided", str(len(current)))
        summary.add_row("Remaining", str(len(records) - len(current)))
        summary.add_row("Curated entities", str(len(entities)))
        if stale:
            summary.add_row("Decisions on records S4 no longer produces", str(stale))
        if orphans:
            summary.add_row("Merges whose target is no longer kept", str(len(orphans)))
        self.console.print(summary)

        actions = ("accept", "edit", "reject", "merge")
        table = Table("Critic verdict", *actions, "undecided", title="Reviewer decisions by critic verdict")
        for verdict in ("supported", "partially", "unsupported", None):
            rids = [rid for rid, r in records.items() if r["critic_verdict"] == verdict]
            counts = Counter(current[rid]["action"] if rid in current else "undecided" for rid in rids)
            table.add_row(verdict or "not judged", *(str(counts[a]) for a in (*actions, "undecided")))
        self.console.print(table)


def now() -> str:
    """Returns the current UTC time as an ISO 8601 string, to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_reviewer() -> str:
    """Returns the OS user name, used as the reviewer when none is given."""
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def run_review(curate: Curate, reviewer: str, redo: bool = False, verdicts: list[str] | None = None) -> None:
    """Runs a review session, then prints what was decided and the overall progress.

    Args:
        curate (Curate): The curation tool.
        reviewer (str): The name recorded with each decision.
        redo (bool): Whether to review records that already have a decision.
        verdicts (list[str] | None): Only review records with these critic verdicts, or None for all.
    """
    made = curate.review(reviewer, redo, verdicts)
    if made:
        curate.console.print("This session: " + ", ".join(f"{n} {a}" for a, n in made.items()))
    curate.status()


app = typer.Typer(add_completion=False, help="S5: review verified entities and curate the ontology's source data.")
CONFIG = typer.Option(Path("config/pipeline.yaml"), "--config", help="Pipeline config file.")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, config: Path = CONFIG) -> None:
    """Starts a review when no command is given."""
    ctx.obj = Curate(config)
    if ctx.invoked_subcommand is None:
        run_review(ctx.obj, default_reviewer())


@app.command()
def review(
    ctx: typer.Context,
    reviewer: str = typer.Option(default_reviewer(), help="Name recorded with each decision."),
    redo: bool = typer.Option(False, "--redo", help="Also review records that already have a decision."),
    verdict: Optional[list[str]] = typer.Option(None, help="Only review this critic verdict (repeatable): "
                                                           "supported, partially, unsupported or none."),
) -> None:
    """Review records one at a time: accept, edit, reject or merge."""
    run_review(ctx.obj, reviewer, redo, [v.lower() for v in verdict] if verdict else None)


@app.command()
def status(ctx: typer.Context) -> None:
    """Show review progress and decisions by critic verdict."""
    ctx.obj.status()


@app.command()
def export(ctx: typer.Context) -> None:
    """Rebuild entities.json from the decision log."""
    curate: Curate = ctx.obj
    entities, orphans = curate.export()
    curate.console.print(f"Saved {len(entities)} entities to {curate.entities_file}")
    if orphans:
        curate.console.print(f"{len(orphans)} merged records point at an entity that is no longer kept.", style="yellow")


if __name__ == "__main__":
    app()
