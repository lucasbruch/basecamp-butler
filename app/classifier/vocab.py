"""Domain vocabulary seeds for the rule-based classifier.

Deliberately biased toward VFX / full-CG commercial production / DOOH rather
than generic office project-management terms, so the v1 heuristics fire on the
things that actually matter in this pipeline.
"""
from __future__ import annotations

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

# Core pipeline / craft terminology (VFX, full-CG, DOOH).
DOMAIN_TERMS = {
    # general vfx / cg
    "render",
    "re-render",
    "rerender",
    "render pass",
    "comp",
    "compositing",
    "roto",
    "paint",
    "cleanup",
    "matchmove",
    "tracking",
    "track",
    "layout",
    "lookdev",
    "lighting",
    "shading",
    "texture",
    "modeling",
    "rig",
    "animation",
    "anim",
    "fx",
    "sim",
    "simulation",
    "cache",
    "plate",
    "conform",
    "slate",
    "shot",
    "sequence",
    "asset",
    "wip",
    "version",
    "turntable",
    # color / delivery
    "color grade",
    "grade",
    "grading",
    "color",
    "colour",
    "delivery",
    "deliverable",
    "master",
    "export",
    "codec",
    "prores",
    "dpx",
    "exr",
    "resolution",
    "frame range",
    "aspect ratio",
    # client / review
    "client review",
    "client",
    "review round",
    "round",
    "notes",
    "feedback",
    "approval",
    "approved",
    "revisions",
    # DOOH-specific
    "dooh",
    "loop",
    "spec",
    "specs",
    "screen",
    "billboard",
    "pixel map",
    "content loop",
    "playout",
    "led",
}


def contains_any(text: str, terms: set[str]) -> bool:
    low = text.lower()
    return any(term in low for term in terms)


def matched_terms(text: str, terms: set[str]) -> list[str]:
    low = text.lower()
    return [term for term in terms if term in low]
