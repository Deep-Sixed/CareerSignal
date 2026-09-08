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
        "for",
        "is",
        "are",
        "be",
        "currently",
    }
)
# A work mode denied by one of these is being excluded, not offered.
NEGATIONS = frozenset({"not", "no", "non", "never", "excluding", "without", "zero"})
# A negation only denies the mode when it is about the mode's availability. "remote, not
# Canada" denies a region and must leave the mode offered, so a following negation counts
# only when one of these words is near it.
AVAILABILITY = frozenset(
    {"available", "option", "options", "offered", "possible", "supported", "eligible", "allowed"}
)
# Denials that need no separate negation token.
DENIALS = frozenset({"unavailable", "unsupported"})
# Skipped when looking either side of a mode for a denial; never treated as a place.
CONNECTIVES = frozenset({"was", "were", "been", "the", "a", "an", "this", "that", "but"})
SKIPPABLE = MODE_QUALIFIERS | CONNECTIVES
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
    # Regions the posting explicitly rules out. Kept apart from `region` so an exclusion can
    # never read as a positive claim: "remote, not Canada" is not "remote in Canada".
    excluded: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> "Location":
        words = _tokens(" ".join(str(text).split()).casefold())
        occurrences = [
            (name, start, start + len(phrase.split()))
            for name, phrases in WORK_MODE_PHRASES
            for phrase in phrases
            for start in _find_all(words, phrase.split())
        ]
        spoken = {index for _, start, end in occurrences for index in range(start, end)}
        offered, consumed = set(), set(spoken)
        for name, start, end in occurrences:
            # A denial never reaches across another stated mode: in "onsite (no remote
            # option)" the denial belongs to remote, and onsite is still offered.
            others = spoken - set(range(start, end))
            denial = _denied_before(words, start, others) or _denied_after(words, end, others)
            if denial:
                consumed.update(denial)
            else:
                offered.add(name)
        # Every stated mode was negated, none was stated, or several were offered at once.
        mode = offered.pop() if len(offered) == 1 else AMBIGUOUS if offered else UNKNOWN
        # Words following a surviving negation describe what is ruled out, not where the job is.
        positive, exclusions, current = [], [], None
        for index, word in enumerate(words):
            if index in consumed or word in MODE_QUALIFIERS or word in CONNECTIVES:
                continue
            if word in NEGATIONS:
                current = []
                exclusions.append(current)
                continue
            (positive if current is None else current).append(word)
        return cls(
            mode,
            _region(" ".join(positive)),
            tuple(sorted({_region(" ".join(group)) for group in exclusions if group})),
        )

    @property
    def stated(self) -> bool:
        return self.work_mode != UNKNOWN or bool(self.region) or bool(self.excluded)

    @property
    def usable(self) -> bool:
        """Whether this names exactly one work mode, which eligibility requires."""
        return self.work_mode not in (UNKNOWN, AMBIGUOUS)

    def accepts(self, job: "Location") -> bool:
        """Whether this configured location covers a job's stated location."""
        if not job.usable or not self.usable or job.work_mode != self.work_mode:
            return False
        # A posting that rules out the configured region cannot satisfy it, whatever else
        # it states.
        if self.region and self.region in job.excluded:
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
        stated_region = f"{mode} / {self.region}" if self.region else f"{mode}, region not stated"
        if self.excluded:
            return f"{stated_region}, excluding {', '.join(self.excluded)}"
        return stated_region


def _region(value: str) -> str:
    return REGION_ALIASES.get(value, value)


def _denied_before(words: list[str], start: int, others: set[int]) -> set[int] | None:
    """A negation ahead of the mode, reachable across filler: "no option for remote"."""
    index = start - 1
    while index >= 0 and index not in others and words[index] in SKIPPABLE:
        index -= 1
    if index < 0 or index in others:
        return None
    return {index} if words[index] in NEGATIONS else None


def _denied_after(words: list[str], end: int, others: set[int]) -> set[int] | None:
    """A denial following the mode: "remote not available", "remote unavailable"."""
    index = end
    while index < len(words) and index not in others and words[index] in SKIPPABLE:
        index += 1
    if index >= len(words) or index in others:
        return None
    if words[index] in DENIALS:
        return {index}
    if words[index] in NEGATIONS:
        # Only an availability word makes this a denial of the mode; otherwise the negation
        # is about something else, such as an excluded region.
        for look in range(index + 1, min(index + 4, len(words))):
            if look in others:
                break
            if words[look] in AVAILABILITY:
                return {index, look}
    return None


def _find_all(words: list[str], needle: list[str]):
    for start in range(len(words) - len(needle) + 1):
        if words[start : start + len(needle)] == needle:
            yield start
