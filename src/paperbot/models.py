from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class PaperPrefix(str, Enum):
    D = "D"
    P = "P"
    N = "N"
    CWG = "CWG"
    EWG = "EWG"
    LWG = "LWG"
    LEWG = "LEWG"
    FS = "FS"
    SD = "SD"
    EDIT = "EDIT"


class PaperType(str, Enum):
    PAPER = "paper"
    ISSUE = "issue"
    EDITORIAL = "editorial"
    STANDING_DOCUMENT = "standing-document"
    DRAFT = "draft"


class FileExt(str, Enum):
    PDF = ".pdf"
    HTML = ".html"


_P_RE = re.compile(r"^([PD])(\d+)R(\d+)$", re.IGNORECASE)
_N_RE = re.compile(r"^N(\d+)$", re.IGNORECASE)
_ISSUE_RE = re.compile(r"^(CWG|EWG|LWG|LEWG|FS)(\d+)$", re.IGNORECASE)


@dataclass(slots=True)
class Paper:
    id: str
    title: str = ""
    author: str = ""
    date: str = ""
    paper_type: PaperType = PaperType.PAPER
    subgroup: str = ""
    url: str = ""
    long_link: str = ""
    github_url: str = ""
    issues: list[str] = field(default_factory=list)

    @property
    def number(self) -> int | None:
        m = _P_RE.match(self.id)
        if m:
            return int(m.group(2))
        m = _N_RE.match(self.id)
        if m:
            return int(m.group(1))
        m = _ISSUE_RE.match(self.id)
        if m:
            return int(m.group(2))
        return None

    @property
    def prefix(self) -> str:
        m = _P_RE.match(self.id)
        if m:
            return m.group(1).upper()
        m = _N_RE.match(self.id)
        if m:
            return "N"
        m = _ISSUE_RE.match(self.id)
        if m:
            return m.group(1).upper()
        return ""

    @property
    def revision(self) -> int | None:
        m = _P_RE.match(self.id)
        return int(m.group(3)) if m else None

    @staticmethod
    def from_index_entry(key: str, entry: dict) -> Paper:
        return Paper(
            id=key,
            title=entry.get("title", ""),
            author=entry.get("author", "") or entry.get("submitter", ""),
            date=entry.get("date", ""),
            paper_type=PaperType(entry["type"]) if "type" in entry else PaperType.PAPER,
            subgroup=entry.get("subgroup", ""),
            url=entry.get("link", ""),
            long_link=entry.get("long_link", ""),
            github_url=entry.get("github_url", ""),
            issues=entry.get("issues", []) or [],
        )
