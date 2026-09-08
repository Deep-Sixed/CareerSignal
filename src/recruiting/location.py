"""Structured work mode and region, so eligibility compares meaning rather than spelling."""

from dataclasses import dataclass

UNKNOWN = "unknown"
# More than one work mode stated and not negated. Never eligible: resolving it by
# the search order would silently pick one meaning over another.
AMBIGUOUS = "ambiguous"

# Phrases naming a work mode, longest first so "on site" wins before "on" could match anything.
# Deliberately small: an unrecognized phrase leaves the mode unstated and therefore ineligible,
# which is visible to the operator, rather than being guessed into a match.
WORK_MODE_PHRASES = (
    ("remote", ("remote", "work from home", "wfh")),
    ("hybrid", ("hybrid",)),
    ("onsite", ("onsite", "on site", "in office")),
)
# Filler that qualifies a work mode or joins it to a region without naming a place. Every
# entry is generic English rather than a location, so dropping one cannot merge two places.
MODE_QUALIFIERS = frozenset(
    {
        "fully",
        "full",
        "100%",
        "only",
        "anywhere",
        "work",
        "working",
        "job",
        "jobs",
        "position",
        "positions",
        "role",
        "roles",
        "opportunity",
        "opportunities",
        "based",
        "in",
        "from",
        "within",
        "or",
        "and",
        "option",
        "options",
    }
)
# A work mode immediately preceded by one of these is being excluded, not offered.
NEGATIONS = frozenset({"not", "no", "non", "never", "excluding", "without", "zero"})
# Region synonyms are an allowlist for the same reason the tracking parameters are: anything
# unlisted stays as the literal place, so two different places never merge.
REGION_ALIASES = {
    "us": "us",
    "u.s.": "us",
    "u.s.a.": "us",
    "usa": "us",
    "united states": "us",
    "canada": "canada",
    "europe": "europe",
    "eu": "europe",
}
SEPARATORS = "()[]{}-–—,/|"


def _trim(word: str) -> str:
    # Drop a trailing sentence period, but keep the interior periods of an initialism
    # such as "u.s." so it still resolves through the region allowlist.
    stripped = word.rstrip(".")
    return stripped if word.endswith(".") and "." not in stripped and stripped else word


def _tokens(value: str) -> list[str]:
    for character in SEPARATORS:
        value = value.replace(character, " ")
    return [_trim(word) for word in value.split()]


@dataclass(frozen=True)
class Location:
    """A work mode plus the region it applies to; either may be unstated."""

    work_mode: str
    region: str

    @classmethod
    def parse(cls, text: str) -> "Location":
        words = _tokens(" ".join(str(text).split()).casefold())
        offered, consumed = set(), set()
        for name, phrases in WORK_MODE_PHRASES:
            for phrase in phrases:
                needle = phrase.split()
                for start in _find_all(words, needle):
                    consumed.update(range(start, start + len(needle)))
                    negated = start and words[start - 1] in NEGATIONS
                    if negated:
                        consumed.add(start - 1)
                    else:
                        offered.add(name)
        # Every stated mode was negated, none was stated, or several were offered at once.
        mode = offered.pop() if len(offered) == 1 else AMBIGUOUS if offered else UNKNOWN
        remainder = " ".join(
            word
            for index, word in enumerate(words)
            if index not in consumed and word not in MODE_QUALIFIERS and word not in NEGATIONS
        )
        return cls(mode, REGION_ALIASES.get(remainder, remainder))

    @property
    def stated(self) -> bool:
        return self.work_mode != UNKNOWN or bool(self.region)

    @property
    def usable(self) -> bool:
        """Whether this names exactly one work mode, which eligibility requires."""
        return self.work_mode not in (UNKNOWN, AMBIGUOUS)

    def accepts(self, job: "Location") -> bool:
        """Whether this configured location covers a job's stated location."""
        if not job.usable or not self.usable or job.work_mode != self.work_mode:
            return False
        # A configured location without a region does not constrain one. A configured region
        # excludes a different region, but still admits a posting that states none; the review
        # reasons record that the region was unstated so the operator can see it.
        return not self.region or job.region in ("", self.region)

    def describe(self) -> str:
        if self.work_mode == AMBIGUOUS:
            suffix = f" / {self.region}" if self.region else ""
            return f"more than one work mode stated{suffix}"
        if not self.stated:
            return "not stated"
        mode = self.work_mode if self.work_mode != UNKNOWN else "work mode not stated"
        if not self.region:
            return f"{mode}, region not stated"
        return f"{mode} / {self.region}"


def _find_all(words: list[str], needle: list[str]):
    for start in range(len(words) - len(needle) + 1):
        if words[start : start + len(needle)] == needle:
            yield start
