"""Keyword search over the machine manual (local stand-in for the Evolus knowledge library)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_WORD = re.compile(r"[a-z0-9][a-z0-9\-]*")


@dataclass
class Section:
    source: str
    heading: str
    text: str


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def load_sections(manual_dir: str | Path | None = None) -> list[Section]:
    """Split every markdown file in the manual directory into sections at headings."""
    root = Path(manual_dir or os.environ.get("MANUAL_DIR", "manual"))
    sections: list[Section] = []
    for path in sorted(root.glob("*.md")):
        heading, lines = path.stem, []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                if "".join(lines).strip():
                    sections.append(Section(path.name, heading, "\n".join(lines).strip()))
                heading, lines = line.lstrip("#").strip(), []
            else:
                lines.append(line)
        if "".join(lines).strip():
            sections.append(Section(path.name, heading, "\n".join(lines).strip()))
    return sections


def search(query: str, sections: list[Section], top_k: int = 3) -> list[dict[str, str]]:
    """Return the sections sharing the most words with the query; headings count double."""
    terms = _words(query)
    scored = []
    for section in sections:
        score = len(terms & _words(section.text)) + 2 * len(terms & _words(section.heading))
        if score:
            scored.append((score, section))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [{"source": s.source, "heading": s.heading, "text": s.text} for _, s in scored[:top_k]]
