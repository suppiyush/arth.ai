"""Party and item name matching: score >= 85 is a match, two matches within 5 points is ambiguous."""

from dataclasses import dataclass, field

from rapidfuzz import fuzz, process, utils

MATCH_SCORE = 85
NEAR_SCORE = 70         # close enough to suggest ("pipes" -> PVC Pipe 1 inch), not to assume
AMBIGUOUS_GAP = 5
WORD_SCORE = 80         # a spoken word matches a word of the name at this ratio ("cemnt" ~ "cement")
FILLER = {"bag", "bags", "bori", "kg", "piece", "pieces", "pcs", "box", "coil", "bucket", "cft", "of", "the",
          "wala", "wali", "wale", "ka", "ki", "ke"}


@dataclass
class Match:
    name: str | None = None                             # the single confident match
    candidates: list[str] = field(default_factory=list)  # set when ambiguous
    near: list[str] = field(default_factory=list)       # close names that left out a spoken word (every_word)

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


def covers(query: str, name: str) -> bool:
    """Every word said is in the name: "tmt 12mm" and "cemnt" pass, "red paint" fails White Paint 20L,
    "TMT Bar 10mm" fails TMT Bar 12mm."""
    have = utils.default_process(name).split()
    return all(any(w in h or fuzz.ratio(w, h) >= WORD_SCORE for h in have)
               for w in utils.default_process(query).split() if w not in FILLER)


def best_match(query: str, choices: list[str], every_word: bool = False) -> Match:
    """every_word (items): a close name only counts if it has every word said, so a colour or size the shop
    doesn't stock is asked about rather than matched to its neighbour."""
    if not query or not choices:
        return Match()
    for c in choices:                                   # exact (case-insensitive) always wins
        if c.lower() == query.strip().lower():
            return Match(c)
    found = process.extract(query, choices, scorer=fuzz.WRatio, processor=utils.default_process, limit=5)
    hits = [(name, score) for name, score, _ in found if score >= MATCH_SCORE]
    if every_word:
        hits = [(name, score) for name, score in hits if covers(query, name)]
        if not hits:
            return Match(near=[name for name, score, _ in found if score >= NEAR_SCORE][:3])
    if not hits:
        return Match()
    top = hits[0][1]
    close = [name for name, score in hits if top - score < AMBIGUOUS_GAP]
    return Match(close[0]) if len(close) == 1 else Match(candidates=close)
