from typing import Any


def render_point(point: dict[str, Any], depth: int) -> list[str]:
    """Renders a point and its sub-points as reference-tagged lines.

    Args:
        point (dict[str, Any]): The point with its "ref", "text" and nested "points".
        depth (int): The nesting level, used for indentation.

    Returns:
        list[str]: One line per point, e.g. "  [5(1)(c)] the placing on the market ...".
    """
    lines = [f"{'  ' * depth}[{point['ref']}] {point['text']}"]
    for sub in point["points"]:
        lines.extend(render_point(sub, depth + 1))
    return lines


def render(chunk: dict[str, Any]) -> str:
    """Renders a chunk as text where every passage is prefixed with its reference.

    Args:
        chunk (dict[str, Any]): The article or annex chunk.

    Returns:
        str: The heading followed by one line per paragraph, subparagraph and point.
    """
    if chunk["type"] == "article":
        heading = f"Article {chunk['article_number']}: {chunk['title']}"
    else:
        heading = f"Annex {chunk['annex_number']}: {chunk['title']}"

    lines, section = [heading, ""], None
    for paragraph in chunk["paragraphs"]:
        if paragraph["text"]:
            lines.append(f"[{paragraph['ref']}] {paragraph['text']}")
        for point in paragraph["points"]:
            if point.get("section") and point["section"] != section:
                section = point["section"]
                lines.append(section)
            lines.extend(render_point(point, 1))
        for subparagraph in paragraph["subparagraphs"]:
            lines.append(f"[{paragraph['ref']}] {subparagraph}")
    return "\n".join(lines)
