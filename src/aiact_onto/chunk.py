import hashlib
import json
import re
import warnings
from pathlib import Path
from typing import Any, Iterator

import yaml
from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning
from tqdm import tqdm

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ARTICLE_ID = re.compile(r"^art_\d+$")
ANNEX_ID = re.compile(r"^anx_[IVXLC]+$")
PARAGRAPH_ID = re.compile(r"^\d{3}\.\d{3}$")
PARAGRAPH_NUMBER = re.compile(r"^\d+\.\s*")
POINT_LABEL = re.compile(r"\(([^)]+)\)")
ELI = "http://data.europa.eu/eli/reg/2024/1689"


class Chunk:
    def __init__(self, text: str, metadata: dict, scope: str | Path = "./config/scope.yaml"):
        """Parses the ingested HTML and loads the chunking scope.

        Args:
            text (str): The ingested HTML content of the regulation.
            metadata (dict): Provenance metadata (source, retrieval date, hash) attached to every chunk.
            scope (str | Path): The path to the YAML file listing the articles and annexes in scope.
        """
        self.soup = BeautifulSoup(text, "lxml")
        self.chunks = []
        self.metadata = metadata
        self.path = Path("./data/chunks/")
        self.scope = self.load_scope(scope)

    def load_scope(self, path: str | Path) -> dict[str, set[str]]:
        """Loads the articles and annexes in scope.

        Args:
            path (str | Path): The path to the scope YAML file.

        Returns:
            dict[str, set[str]]: The article and annex numbers in scope, keyed by "articles" and "annexes".
                An empty set means everything of that kind is in scope.
        """
        try:
            with open(path, "r", encoding="utf-8") as file:
                scope = yaml.safe_load(file) or {}
        except FileNotFoundError:
            scope = {}
        return {kind: {str(n) for n in scope.get(kind) or []} for kind in ("articles", "annexes")}

    def in_scope(self, kind: str, number: str) -> bool:
        """Determines whether an article or annex should be chunked.

        Args:
            kind (str): Either "articles" or "annexes".
            number (str): The article number (e.g. "5") or annex numeral (e.g. "III").

        Returns:
            bool: True if the scope for that kind is empty or lists the number, False otherwise.
        """
        return not self.scope[kind] or number in self.scope[kind]

    def clean(self, s: str) -> str:
        """Normalises whitespace, replacing non-breaking spaces and collapsing runs of whitespace.

        Args:
            s (str): The raw text.

        Returns:
            str: The cleaned text.
        """
        return " ".join(s.replace(" ", " ").split())

    def text(self, el: Tag) -> str:
        """Extracts the cleaned text of an element as it reads in the source.

        Args:
            el (Tag): The element to extract text from.

        Returns:
            str: The cleaned text of the element and all its descendants.
        """
        return self.clean(el.get_text())

    def blocks(self, container: Tag) -> Iterator[tuple[str, Tag]]:
        """Yields the direct text, heading and point-table blocks of a container in document order.

        Wrapper divs without structural meaning (e.g. quoted amendments) are descended into;
        title divs are skipped.

        Args:
            container (Tag): The article, paragraph, annex or table cell element to walk.

        Returns:
            Iterator[tuple[str, Tag]]: Pairs of block kind ("p", "heading" or "table") and element.
        """
        for child in container.find_all(recursive=False):
            classes = child.get("class", [])
            if child.name == "p" and "oj-normal" in classes:
                yield "p", child
            elif child.name == "p" and "oj-ti-grseq-1" in classes:
                yield "heading", child
            elif child.name == "span":
                yield "p", child
            elif child.name == "table":
                yield "table", child
            elif child.name == "div" and "eli-title" not in classes:
                yield from self.blocks(child)

    def parse_point(self, table: Tag, parent_ref: str) -> dict[str, Any] | None:
        """Parses a point table and its nested sub-points.

        Args:
            table (Tag): The table holding the point label and content cells.
            parent_ref (str): The reference of the enclosing paragraph or point (e.g. "5(1)").

        Returns:
            dict[str, Any] | None: The point with its "ref", "label", "text" and nested "points",
                or None if the table is not a point.
        """
        tbody = table.find("tbody", recursive=False) or table
        row = tbody.find("tr", recursive=False)
        cells = row.find_all("td", recursive=False) if row else []
        if len(cells) < 2:
            return None

        raw_label = self.text(cells[-2])
        match = POINT_LABEL.search(raw_label)
        label = match.group(1) if match else raw_label.rstrip(".")
        ref = f"{parent_ref}({label})"

        texts, subpoints = [], []
        for kind, el in self.blocks(cells[-1]):
            if kind == "p":
                texts.append(self.text(el))
            elif kind == "table" and (point := self.parse_point(el, ref)):
                subpoints.append(point)

        return {"ref": ref, "label": label, "text": " ".join(texts), "points": subpoints}

    def parse_paragraph(self, container: Tag, ref: str, number: int | None) -> dict[str, Any]:
        """Parses a paragraph's lead text, subparagraphs and points.

        Args:
            container (Tag): The paragraph element, or the article/annex element when it is unnumbered.
            ref (str): The reference of the paragraph (e.g. "5(1)", "3" or "Annex III").
            number (int | None): The paragraph number, or None for unnumbered paragraphs.

        Returns:
            dict[str, Any]: The paragraph with its "ref", "number", "text", "subparagraphs" and "points".
        """
        texts, points, section = [], [], None
        for kind, el in self.blocks(container):
            if kind == "p":
                texts.append(self.text(el))
            elif kind == "heading":
                section = self.text(el)
            elif point := self.parse_point(el, ref):
                if section:
                    point["section"] = section
                points.append(point)

        if texts and number is not None:
            texts[0] = PARAGRAPH_NUMBER.sub("", texts[0], count=1)

        return {
            "ref": ref,
            "number": number,
            "text": texts[0] if texts else "",
            "subparagraphs": texts[1:],
            "points": points,
        }

    def parse_article(self, div: Tag) -> dict[str, Any]:
        """Parses an article into a chunk.

        Args:
            div (Tag): The article element (id "art_N").

        Returns:
            dict[str, Any]: The chunk with its "id", "type", "article_number", "title", "eli_uri",
                "paragraphs" and full "text".
        """
        num = self.text(div.select_one("p.oj-ti-art")).removeprefix("Article ").strip()
        title = self.text(div.select_one("p.oj-sti-art"))

        paragraph_divs = div.find_all("div", id=PARAGRAPH_ID, recursive=False)
        if paragraph_divs:
            paragraphs = []
            for p_div in paragraph_divs:
                p_num = int(p_div["id"].split(".")[1])
                paragraphs.append(self.parse_paragraph(p_div, f"{num}({p_num})", p_num))
        else:
            paragraphs = [self.parse_paragraph(div, num, None)]

        return {
            "id": div["id"],
            "type": "article",
            "article_number": num,
            "title": title,
            "eli_uri": f"{ELI}/art_{num}/oj",
            "paragraphs": paragraphs,
            "text": self.text(div),
        }

    def parse_annex(self, div: Tag) -> dict[str, Any]:
        """Parses an annex into a chunk.

        Args:
            div (Tag): The annex element (id "anx_N").

        Returns:
            dict[str, Any]: The chunk with its "id", "type", "annex_number", "title", "eli_uri",
                "paragraphs" and full "text".
        """
        headings = div.find_all("p", class_="oj-doc-ti", recursive=False)
        num = self.text(headings[0]).removeprefix("ANNEX ").strip()
        title = self.text(headings[1]) if len(headings) > 1 else ""

        return {
            "id": div["id"],
            "type": "annex",
            "annex_number": num,
            "title": title,
            "eli_uri": f"{ELI}/anx_{num}/oj",
            "paragraphs": [self.parse_paragraph(div, f"Annex {num}", None)],
            "text": self.text(div),
        }

    def parse(self) -> list[dict[str, Any]]:
        """Parses every article and annex in scope.

        Returns:
            list[dict[str, Any]]: The article chunks followed by the annex chunks.
        """
        self.chunks = []
        articles = self.soup.find_all("div", id=ARTICLE_ID)
        annexes = self.soup.find_all("div", id=ANNEX_ID)

        for div in tqdm(articles, desc="Parsing articles"):
            if self.in_scope("articles", div["id"].removeprefix("art_")):
                self.chunks.append(self.parse_article(div))
        for div in tqdm(annexes, desc="Parsing annexes"):
            if self.in_scope("annexes", div["id"].removeprefix("anx_")):
                self.chunks.append(self.parse_annex(div))
        return self.chunks

    def filename(self, chunk: dict[str, Any]) -> str:
        """Builds the output filename of a chunk.

        Args:
            chunk (dict[str, Any]): The article or annex chunk.

        Returns:
            str: "art_NNN.json" for articles (zero-padded) or "annex_N.json" for annexes.
        """
        if chunk["type"] == "article":
            return f"art_{int(chunk['article_number']):03d}.json"
        return f"annex_{chunk['annex_number']}.json"

    def save(self) -> list[Path]:
        """Saves one JSON file per chunk, each carrying the run metadata.

        Returns:
            list[Path]: The paths of the written files.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        written = []
        for chunk in self.chunks:
            out = self.path / self.filename(chunk)
            with open(out, "w", encoding="utf-8") as file:
                json.dump({**chunk, "metadata": self.metadata}, file, ensure_ascii=False, indent=2)
            written.append(out)
        return written


if __name__ == "__main__":
    from aiact_onto.ingest import Ingest

    ingest = Ingest()
    text = ingest.ingest()
    metadata = {
        "source_url": ingest.html,
        "celex": "32024R1689",
        "retrieved_at": ingest.last_ingest().isoformat(),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }

    chunk = Chunk(text, metadata=metadata)
    chunk.parse()
    written = chunk.save()
    print(f"Saved {len(written)} chunks to {chunk.path}")
