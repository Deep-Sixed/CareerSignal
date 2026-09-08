"""Immutable, normalized inputs and explainable review packets."""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from recruiting.location import AMBIGUOUS, Location


def fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def clean(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return " ".join(value.split())


# These allowlists decide what counts as the same job, so every entry must be a value no job
# board uses to identify one. Anything unlisted is treated as meaningful and preserved.
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMETERS = frozenset({"gclid", "fbclid", "msclkid"})
# Fragments naming a position on the page rather than a job. Hash-routed boards put the job
# identity in the fragment, so every other fragment is kept.
DECORATIVE_FRAGMENTS = frozenset({"top", "content", "main", "header", "footer", "apply"})
URL_REQUIRED = "Title, company and HTTPS job URL are required"


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMETERS or lowered.startswith(TRACKING_PREFIXES)


def canonical_url(value: str) -> str:
    """Collapse equivalent spellings of one job URL without merging distinct jobs."""
    url = urlsplit(clean(value))
    if url.scheme != "https" or not url.hostname:
        raise ValueError(URL_REQUIRED)
    if url.username or url.password:
        raise ValueError("Job URLs cannot contain credentials")
    try:
        port = url.port
    except ValueError as exc:
        raise ValueError(URL_REQUIRED) from exc
    # hostname is already lowercased; only a leading www. label is dropped, never a deeper
    # subdomain, which would merge genuinely different hosts.
    host = url.hostname.removeprefix("www.")
    if not host:
        raise ValueError(URL_REQUIRED)
    if ":" in host:
        host = f"[{host}]"
    if port and port != 443:
        host = f"{host}:{port}"
    # Path case is significant to the server, and so are repeated separators: only a single
    # trailing separator is dropped. Collapsing "//" would merge paths a server may distinguish.
    path = url.path or "/"
    if path != "/" and path.endswith("/") and not path.endswith("//"):
        path = path[:-1]
    query = sorted(
        (k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if not _is_tracking(k)
    )
    fragment = "" if url.fragment.casefold() in DECORATIVE_FRAGMENTS else url.fragment
    return urlunsplit(("https", host, path, urlencode(query), fragment))


@dataclass(frozen=True)
class Opportunity:
    title: str
    company: str
    url: str
    location: str
    skills: tuple[str, ...]

    @classmethod
    def normalize(cls, item: dict) -> "Opportunity":
        if not isinstance(item, dict):
            raise ValueError("Job entry must be an object")
        title, company = clean(item.get("title", "")), clean(item.get("company", ""))
        if not title or not company:
            raise ValueError(URL_REQUIRED)
        canonical = canonical_url(item.get("url", ""))
        skills = item.get("skills", [])
        if not isinstance(skills, list):
            raise ValueError("Skills must be a list")
        return cls(
            title,
            company,
            canonical,
            clean(item.get("location", "")).casefold(),
            tuple(sorted({clean(s).casefold() for s in skills if clean(s)})),
        )

    @property
    def key(self) -> str:
        return fingerprint(self.url)


@dataclass(frozen=True)
class Profile:
    skills: tuple[str, ...]
    locations: tuple[str, ...] = ("remote",)
    threshold: int = 70

    def __post_init__(self):
        if not self.skills or not all(clean(s) for s in self.skills):
            raise ValueError("At least one skill is required")
        if not 0 <= self.threshold <= 100:
            raise ValueError("Threshold must be between 0 and 100")
        object.__setattr__(
            self, "skills", tuple(sorted({clean(s).casefold() for s in self.skills}))
        )
        object.__setattr__(
            self, "locations", tuple(sorted({clean(s).casefold() for s in self.locations}))
        )
        if not self.locations:
            raise ValueError("At least one accepted location is required")
        # A configured location whose work mode is unstated could never match anything, which
        # would silently reject every opportunity. Say so at configuration time instead.
        unusable = [s for s in self.locations if not Location.parse(s).usable]
        if unusable:
            raise ValueError(
                "Accepted locations must state a work mode (remote, hybrid or onsite): "
                + ", ".join(unusable)
            )
        # Eligibility reads exclusions from the posting, not from the configuration, so an
        # excluded region here would be silently ignored.
        negative = [s for s in self.locations if Location.parse(s).excluded]
        if negative:
            raise ValueError(
                "Accepted locations cannot exclude a region; state the region to accept: "
                + ", ".join(negative)
            )

    @property
    def accepted_locations(self) -> tuple[Location, ...]:
        return tuple(Location.parse(s) for s in self.locations)


@dataclass(frozen=True)
class Review:
    id: str
    opportunity: Opportunity
    score: int
    eligible: bool
    advances: bool
    reasons: tuple[str, ...]
    draft: str

    def payload(self) -> dict:
        return asdict(self)


def evaluate(job: Opportunity, profile: Profile) -> Review:
    matched = set(job.skills) & set(profile.skills)
    score = int(100 * len(matched) / len(profile.skills))
    stated = Location.parse(job.location)
    eligible = any(accepted.accepts(stated) for accepted in profile.accepted_locations)
    reasons = [
        f"Matched {len(matched)}/{len(profile.skills)} configured skills",
        "Matched: " + (", ".join(sorted(matched)) or "none"),
        "Missing: " + (", ".join(sorted(set(profile.skills) - matched)) or "none"),
    ]
    reasons.append(f"Location read as {stated.describe()}")
    reasons.append(
        "Location eligible"
        if eligible
        else "Location missing"
        if not stated.stated
        else "Location states more than one work mode; not resolved automatically"
        if stated.work_mode == AMBIGUOUS
        else "Location work mode not stated; eligibility needs an explicit remote, hybrid or onsite"
        if not stated.usable
        else "Location outside configured eligibility"
    )
    advances = eligible and score > profile.threshold
    reasons.append(f"Score must be strictly above {profile.threshold}")
    draft = f"I would like to learn more about {job.title} at {job.company}. Reference: {job.url}"
    version = fingerprint(
        {"job": asdict(job), "profile": asdict(profile), "draft": draft, "rule_version": 1}
    )
    return Review(version, job, score, eligible, advances, tuple(reasons), draft)
