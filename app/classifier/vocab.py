"""Domain vocabulary seeds for the rule-based classifier.

Deliberately generic so the v1 heuristics work for anyone — common work nouns
(documents, deliverables, tickets, meetings…) paired with an action signal.
For domain-specific judgement, switch to the LLM classifier and define your own
persona/topics on the Settings page.
"""
from __future__ import annotations

import re
from functools import lru_cache

# Words that, in a message/comment, signal someone is asking for an action.
ACTION_SIGNALS = {
    "please",
    "can you",
    "could you",
    "need",
    "needs",
    "asap",
    "eod",
    "eow",
    "by end of",
    "deadline",
    "due",
    "turnaround",
    "deliver",
    "delivery",
    "send",
    "upload",
    "publish",
    "push",
    "review",
    "approve",
    "sign off",
    "sign-off",
    "signoff",
    "fix",
    "address",
    "revise",
    "revision",
    "update",
    "kickoff",
    "turnover",
    "ingest",
}

# Common "real work" nouns — the things people actually ask each other to act on.
# Kept generic on purpose; pair one of these with an ACTION_SIGNAL to flag a task.
DOMAIN_TERMS = {
    # documents & deliverables
    "document",
    "doc",
    "file",
    "report",
    "deck",
    "slide",
    "slides",
    "presentation",
    "spreadsheet",
    "draft",
    "proposal",
    "brief",
    "plan",
    "budget",
    "invoice",
    "quote",
    "estimate",
    "contract",
    "agreement",
    "summary",
    "agenda",
    "deliverable",
    # design & content
    "design",
    "mockup",
    "wireframe",
    "prototype",
    "layout",
    "logo",
    "banner",
    "graphic",
    "image",
    "photo",
    "video",
    "copy",
    "content",
    "asset",
    "draft",
    # software / product
    "bug",
    "issue",
    "ticket",
    "feature",
    "task",
    "release",
    "deploy",
    "deployment",
    "build",
    "pull request",
    "merge",
    "spec",
    "requirement",
    "requirements",
    # web / comms
    "page",
    "site",
    "website",
    "app",
    "form",
    "email",
    "message",
    # process & scheduling
    "meeting",
    "call",
    "demo",
    "milestone",
    "deadline",
    "feedback",
    "approval",
    "revision",
    "version",
}


@lru_cache(maxsize=None)
def _term_pattern(term: str) -> re.Pattern[str]:
    r"""A case-insensitive, word-boundary matcher for one term.

    Substring matching (`term in text`) produced false positives on short tokens
    — "doc" would fire on "**doc**umentation", "app" on "**app**lied", "spec" on
    "e**spec**ially". We match whole words/phrases instead. `\b` on each side,
    with internal whitespace allowed to also match hyphens/underscores so
    "sign off", "sign-off" and "sign_off" all hit the same term.
    """
    parts = [re.escape(p) for p in term.split()]
    body = r"[\s_-]+".join(parts)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


def contains_any(text: str, terms: set[str]) -> bool:
    return any(_term_pattern(term).search(text) for term in terms)


def matched_terms(text: str, terms: set[str]) -> list[str]:
    return [term for term in terms if _term_pattern(term).search(text)]


# First names that are also everyday English words. For these, a bare
# word-boundary match ("mark the file", "will you send it", "art department") is
# too noisy, so we require a vocative cue (a greeting, an @-handle, or trailing
# punctuation) before treating the text as addressing the owner by name.
_AMBIGUOUS_FIRST_NAMES = {
    "mark", "will", "bill", "art", "rose", "drew", "grace", "hope", "june",
    "may", "jack", "rob", "guy", "sue", "dawn", "joy", "ray", "gene", "pat",
    "hazel", "daisy", "victor", "frank", "rich", "chip", "penny", "bob", "don",
    "van", "jean", "holly", "brook", "brooke", "wade", "reed", "miles", "chase",
}


@lru_cache(maxsize=None)
def _vocative_pattern(name: str) -> re.Pattern[str]:
    """Match `name` only when it reads as address, not as a common word.

    Fires on ``@name``, a greeting followed by the name, or the name trailed by
    ``, : ? !`` or a possessive — the shapes a real mention actually takes."""
    n = re.escape(name)
    return re.compile(
        rf"(?:@|\b(?:hey|hi|hello|yo|thanks|thx|cc)\s+){n}\b"
        rf"|\b{n}\s*(?:[,:?!]|['’]s)",
        re.IGNORECASE,
    )


def mentions_name(text: str, full_name: str | None) -> bool:
    """True if `text` addresses the person by name.

    The full name always counts. A bare first name counts too — except for first
    names that are also common words, where a vocative cue is required. This
    keeps "hey Sam, can you…" while dropping "mark the file as done"."""
    name = (full_name or "").strip()
    if not name or not text:
        return False
    parts = name.split()
    if len(parts) > 1 and _term_pattern(name).search(text):
        return True
    first = parts[0]
    if first.lower() in _AMBIGUOUS_FIRST_NAMES:
        return bool(_vocative_pattern(first).search(text))
    return bool(_term_pattern(first).search(text))
