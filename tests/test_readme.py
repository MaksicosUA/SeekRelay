from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/MaksicosUA/SeekRelay/blob/main/"
LINKS = re.findall(r"\]\(([^)]+)\)", (ROOT / "README.md").read_text(encoding="utf-8"))


def test_links_work_outside_the_repository() -> None:
    # PyPI shows the README frozen per release and cannot resolve relative links.
    assert [link for link in LINKS if not link.startswith("https://")] == []


def test_links_into_the_repository_point_at_files() -> None:
    paths = [
        link.removeprefix(REPOSITORY).partition("#")[0]
        for link in LINKS
        if link.startswith(REPOSITORY)
    ]
    assert paths
    assert [path for path in paths if not (ROOT / path).is_file()] == []
