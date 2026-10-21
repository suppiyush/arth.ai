"""Party and item name matching: score >= 85 is a match, two matches within 5 points is ambiguous."""

from dataclasses import dataclass, field

from rapidfuzz import fuzz, process, utils

MATCH_SCORE = 85
AMBIGUOUS_GAP = 5


@dataclass
class Match:
    name: str | None = None                             # the single confident match
    candidates: list[str] = field(default_factory=list)  # set when ambiguous

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


def best_match(query: str, choices: list[str]) -> Match:
    if not query or not choices:
        return Match()
    for c in choices:                                   # exact (case-insensitive) always wins
        if c.lower() == query.strip().lower():
            return Match(c)
    hits = process.extract(query, choices, scorer=fuzz.WRatio, processor=utils.default_process, limit=5)
    hits = [(name, score) for name, score, _ in hits if score >= MATCH_SCORE]
    if not hits:
        return Match()
    top = hits[0][1]
    close = [name for name, score in hits if top - score < AMBIGUOUS_GAP]
    return Match(close[0]) if len(close) == 1 else Match(candidates=close)
