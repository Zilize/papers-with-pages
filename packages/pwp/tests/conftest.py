"""Synthetic fixtures keep the test suite independent of external services."""

from pathlib import Path

import pytest


@pytest.fixture
def atom_xml() -> bytes:
    return (Path(__file__).parent / "fixtures" / "atom.xml").read_bytes()


@pytest.fixture
def oai_xml() -> bytes:
    return (Path(__file__).parent / "fixtures" / "oai.xml").read_bytes()


@pytest.fixture
def project_html() -> str:
    return (Path(__file__).parent / "fixtures" / "project.html").read_text(encoding="utf-8")
