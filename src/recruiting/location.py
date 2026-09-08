"""Structured work mode and region, so eligibility compares meaning rather than spelling."""

from dataclasses import dataclass

UNKNOWN = "unknown"

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
    }
)
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
        mode = UNKNOWN
        for name, phrases in WORK_MODE_PHRASES:
            for phrase in phrases:
                needle = phrase.split()
                index = _find(words, needle)
                if index is not None:
                    mode, words = name, words[:index] + words[index + len(needle) :]
                    break
            if mode != UNKNOWN:
                break
        remainder = " ".join(w for w in words if w not in MODE_QUALIFIERS)
        return cls(mode, REGION_ALIASES.get(remainder, remainder))

    @property
    def stated(self) -> bool:
        return self.work_mode != UNKNOWN or bool(self.region)

    def accepts(self, job: "Location") -> bool:
        """Whether this configured location covers a job's stated location."""
        if job.work_mode == UNKNOWN or job.work_mode != self.work_mode:
            return False
        # A configured location without a region does not constrain one. A configured region
        # excludes a different region, but still admits a posting that states none; the review
        # reasons record that the region was unstated so the operator can see it.
        return not self.region or job.region in ("", self.region)

    def describe(self) -> str:
        if not self.stated:
            return "not stated"
        mode = self.work_mode if self.work_mode != UNKNOWN else "work mode not stated"
        if not self.region:
            return f"{mode}, region not stated"
        return f"{mode} / {self.region}"


def _find(words: list[str], needle: list[str]) -> int | None:
    for start in range(len(words) - len(needle) + 1):
        if words[start : start + len(needle)] == needle:
            return start
    return None
