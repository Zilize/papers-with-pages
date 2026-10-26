"""Shared link-label patterns, kept import-free to avoid module cycles."""

import re

NOISE = re.compile(
    r"\b(reference[sd]?|related work|other projects|more research|acknowledg\w*|"
    r"template|website source|built with|based on|inspired by|dependencies|"
    r"installation|requirements|baseline[sd]?|comparison|credit[sd]?|personal|homepage of)\b",
    re.I,
)
ACTION = re.compile(
    r"^(?:(?:our|official|project|view|download|source)\s+)*"
    r"(?:code|github|gitlab|repository|source code|project(?: page| homepage| website)?|"
    r"homepage|website|data(?:set)?s?|models?|checkpoints?|weights|demo|"
    r"hugging\s*face|paper|arxiv|video)(?:\s*\([^)]*\))?$",
    re.I,
)
_DECORATION = re.compile(r"[↗🔗📄💻]")


def action_label(anchor: str) -> bool:
    """Whether an anchor reads like an explicit resource button."""
    return bool(ACTION.fullmatch(_DECORATION.sub("", anchor).strip()))
