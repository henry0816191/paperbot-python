"""Tests for paperscout.models."""
from __future__ import annotations

import pytest

from paperscout.models import FileExt, Paper, PaperPrefix, PaperType


# ── Enum sanity ──────────────────────────────────────────────────────────────

def test_paper_prefix_values():
    assert PaperPrefix.D == "D"
    assert PaperPrefix.P == "P"
    assert PaperPrefix.N == "N"
    assert PaperPrefix.CWG == "CWG"
    assert PaperPrefix.EWG == "EWG"
    assert PaperPrefix.LWG == "LWG"
    assert PaperPrefix.LEWG == "LEWG"
    assert PaperPrefix.FS == "FS"
    assert PaperPrefix.SD == "SD"
    assert PaperPrefix.EDIT == "EDIT"


def test_paper_type_values():
    assert PaperType.PAPER == "paper"
    assert PaperType.ISSUE == "issue"
    assert PaperType.EDITORIAL == "editorial"
    assert PaperType.STANDING_DOCUMENT == "standing-document"
    assert PaperType.DRAFT == "draft"


def test_file_ext_values():
    assert FileExt.PDF == ".pdf"
    assert FileExt.HTML == ".html"


# ── Paper properties: P-prefix ───────────────────────────────────────────────

def test_paper_p_number_prefix_revision():
    p = Paper(id="P2300R10")
    assert p.number == 2300
    assert p.prefix == "P"
    assert p.revision == 10


def test_paper_p_zero_revision():
    p = Paper(id="P0001R0")
    assert p.number == 1
    assert p.prefix == "P"
    assert p.revision == 0


def test_paper_d_prefix():
    p = Paper(id="D2300R1")
    assert p.number == 2300
    assert p.prefix == "D"
    assert p.revision == 1


# ── Paper properties: N-prefix ───────────────────────────────────────────────

def test_paper_n_number():
    p = Paper(id="N4950")
    assert p.number == 4950
    assert p.prefix == "N"
    assert p.revision is None


# ── Paper properties: issue tracker prefixes ─────────────────────────────────

@pytest.mark.parametrize("paper_id,expected_prefix,expected_num", [
    ("CWG123", "CWG", 123),
    ("EWG456", "EWG", 456),
    ("LWG789", "LWG", 789),
    ("LEWG42", "LEWG", 42),
    ("FS10", "FS", 10),
])
def test_paper_issue_prefixes(paper_id, expected_prefix, expected_num):
    p = Paper(id=paper_id)
    assert p.prefix == expected_prefix
    assert p.number == expected_num
    assert p.revision is None


# ── Paper properties: unknown IDs ────────────────────────────────────────────

def test_paper_unknown_id():
    p = Paper(id="UNKNOWN")
    assert p.number is None
    assert p.prefix == ""
    assert p.revision is None


def test_paper_empty_id():
    p = Paper(id="")
    assert p.number is None
    assert p.prefix == ""
    assert p.revision is None


# ── Paper.from_index_entry ───────────────────────────────────────────────────

def test_from_index_entry_full():
    entry = {
        "title": "Test Paper",
        "author": "John Doe",
        "date": "2024-01-15",
        "type": "paper",
        "subgroup": "EWG",
        "link": "https://wg21.link/P2300R10",
        "long_link": "https://isocpp.org/files/papers/P2300R10.pdf",
        "github_url": "https://github.com/cplusplus/papers/issues/1",
        "issues": ["CWG1234", "LWG5678"],
    }
    paper = Paper.from_index_entry("P2300R10", entry)
    assert paper.id == "P2300R10"
    assert paper.title == "Test Paper"
    assert paper.author == "John Doe"
    assert paper.date == "2024-01-15"
    assert paper.paper_type == PaperType.PAPER
    assert paper.subgroup == "EWG"
    assert paper.url == "https://wg21.link/P2300R10"
    assert paper.long_link == "https://isocpp.org/files/papers/P2300R10.pdf"
    assert paper.github_url == "https://github.com/cplusplus/papers/issues/1"
    assert paper.issues == ["CWG1234", "LWG5678"]


def test_from_index_entry_submitter_fallback():
    entry = {"submitter": "Jane Doe", "author": ""}
    paper = Paper.from_index_entry("N4950", entry)
    assert paper.author == "Jane Doe"


def test_from_index_entry_author_wins_over_submitter():
    entry = {"author": "Real Author", "submitter": "Someone Else"}
    paper = Paper.from_index_entry("P0001R0", entry)
    assert paper.author == "Real Author"


def test_from_index_entry_defaults():
    paper = Paper.from_index_entry("P0001R0", {})
    assert paper.title == ""
    assert paper.author == ""
    assert paper.date == ""
    assert paper.paper_type == PaperType.PAPER
    assert paper.subgroup == ""
    assert paper.url == ""
    assert paper.long_link == ""
    assert paper.github_url == ""
    assert paper.issues == []


def test_from_index_entry_no_issues_key():
    entry = {"title": "No Issues Field"}
    paper = Paper.from_index_entry("P0002R0", entry)
    assert paper.issues == []


def test_from_index_entry_null_issues():
    entry = {"issues": None}
    paper = Paper.from_index_entry("P0003R0", entry)
    assert paper.issues == []


def test_from_index_entry_issue_type():
    entry = {"type": "issue"}
    paper = Paper.from_index_entry("CWG1", entry)
    assert paper.paper_type == PaperType.ISSUE


def test_from_index_entry_standing_document_type():
    entry = {"type": "standing-document"}
    paper = Paper.from_index_entry("SD6", entry)
    assert paper.paper_type == PaperType.STANDING_DOCUMENT


# ── Paper dataclass defaults ──────────────────────────────────────────────────

def test_paper_default_fields():
    p = Paper(id="P1234R0")
    assert p.title == ""
    assert p.author == ""
    assert p.date == ""
    assert p.paper_type == PaperType.PAPER
    assert p.subgroup == ""
    assert p.url == ""
    assert p.long_link == ""
    assert p.github_url == ""
    assert p.issues == []
