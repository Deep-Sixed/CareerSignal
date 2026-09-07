"""Immutable, normalized inputs and explainable review packets."""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def clean(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return " ".join(value.split())


@dataclass(frozen=True)
class Opportunity:
    title: str
    company: str
    url: str
    location: str
    skills: tuple[str, ...]

    @classmethod
    def normalize(cls, item: dict) -> "Opportunity":
        title, company = clean(item["title"]), clean(item["company"])
        url = urlsplit(clean(item["url"]))
        if not title or not company or url.scheme != "https" or not url.hostname:
            raise ValueError("Title, company and HTTPS job URL are required")
        if url.username or url.password:
            raise ValueError("Job URLs cannot contain credentials")
        query = sorted(
            (k, v)
            for k, v in parse_qsl(url.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
        )
        canonical = urlunsplit(("https", url.netloc.lower(), url.path or "/", urlencode(query), ""))
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
    eligible = bool(job.location) and job.location in profile.locations
    reasons = [
        f"Matched {len(matched)}/{len(profile.skills)} configured skills",
        "Matched: " + (", ".join(sorted(matched)) or "none"),
        "Missing: " + (", ".join(sorted(set(profile.skills) - matched)) or "none"),
    ]
    reasons.append(
        "Location eligible"
        if eligible
        else "Location missing"
        if not job.location
        else "Location outside configured eligibility"
    )
    advances = eligible and score > profile.threshold
    reasons.append(f"Score must be strictly above {profile.threshold}")
    draft = f"I would like to learn more about {job.title} at {job.company}. Reference: {job.url}"
    version = fingerprint(
        {"job": asdict(job), "profile": asdict(profile), "draft": draft, "rule_version": 1}
    )
    return Review(version, job, score, eligible, advances, tuple(reasons), draft)
