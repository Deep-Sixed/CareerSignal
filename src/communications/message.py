"""Normalize supplied messages without fetching mail, links, or attachments."""

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

from recruiting.models import fingerprint

MAX_MESSAGE_BYTES = 2_000_000


def normalize_text(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("Message text must be a string without NUL characters")
    return "\n".join(
        line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ).strip()


class _VisibleHTML(HTMLParser):
    """Keep visible text and explicit link targets; never execute or retrieve content."""

    BLOCKS = {"p", "div", "li", "article", "section", "br", "tr", "h1", "h2", "h3", "h4"}
    SKIP = {"script", "style", "head", "template"}
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    JOB_LINK_TEXT = {"apply", "apply now", "view job"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.hidden:
            if tag not in self.VOID:
                self.hidden.append(tag)
            return
        if tag in self.SKIP or "hidden" in attrs or attrs.get("aria-hidden") == "true":
            if tag not in self.VOID:
                self.hidden.append(tag)
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag in {"h2", "h3", "h4"}:
            self.parts.append("Title: ")
        if tag == "a":
            self.links.append((attrs.get("href", ""), len(self.parts)))

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                while self.hidden.pop() != tag:
                    pass
            return
        if tag == "a" and self.links:
            url, start = self.links.pop()
            # Emit an explicit field. Recruiting validation decides whether it is usable.
            if url:
                prefix = "".join(self.parts[:start]).rsplit("\n", 1)[-1].strip().lower()
                if prefix in {"url:", "apply:", "job url:"}:
                    self.parts[start:] = [url]
                elif " ".join("".join(self.parts[start:]).split()).casefold() in self.JOB_LINK_TEXT:
                    self.parts.extend(["\nURL: ", url, "\n"])
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(value: str) -> str:
    parser = _VisibleHTML()
    parser.feed(value)
    parser.close()
    return normalize_text("".join(parser.parts))


@dataclass(frozen=True)
class Message:
    namespace: str
    external_id: str = ""
    sender: str = ""
    subject: str = ""
    text: str = ""
    html: str = ""

    def __post_init__(self):
        for field in ("namespace", "external_id", "sender", "subject", "text", "html"):
            object.__setattr__(self, field, normalize_text(getattr(self, field)))
        if not self.namespace:
            raise ValueError("A source namespace is required")
        if not self.text and not self.html:
            raise ValueError("Message body is missing")
        if sum(len(getattr(self, f).encode()) for f in ("text", "html")) > MAX_MESSAGE_BYTES:
            raise ValueError("Message exceeds the extraction size limit")

    @property
    def digest(self):
        return fingerprint([self.sender, self.subject, self.text, self.html])

    @property
    def key(self):
        # Namespace should identify the mailbox/provider, not just the provider product.
        return "message:" + fingerprint([self.namespace, self.external_id or self.digest])

    @property
    def content(self):
        # MIME alternatives are not additive. Prefer nonempty plain text consistently.
        return self.text if self.text else html_text(self.html)

    @property
    def format(self):
        return "text" if self.text else "html"

    @classmethod
    def from_bytes(cls, raw: bytes, *, namespace: str, provider_id: str = ""):
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("Message exceeds the extraction size limit")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        parts = {"text/plain": [], "text/html": []}

        def bodies(part):
            if part.get_content_disposition() == "attachment" or part.get_filename():
                return
            if part.get_content_type() == "message/rfc822":
                return
            if part.defects:
                raise ValueError("Malformed MIME message")
            if part.is_multipart():
                for child in part.iter_parts():
                    yield from bodies(child)
            else:
                yield part

        for part in bodies(message):
            if part.defects:
                raise ValueError("Malformed MIME message")
            if (
                part.is_multipart()
                or part.get_content_disposition() == "attachment"
                or part.get_filename()
            ):
                continue
            kind = part.get_content_type()
            if kind not in parts:
                continue
            try:
                payload = part.get_payload(decode=True)
                if part.defects:
                    raise ValueError("Malformed MIME encoding")
                value = payload.decode(part.get_content_charset() or "utf-8", errors="strict")
            except (UnicodeError, LookupError) as exc:
                raise ValueError("Unsupported or invalid MIME charset") from exc
            parts[kind].append(value)
        if any(len(values) > 1 for values in parts.values()):
            raise ValueError("Ambiguous multiple inline bodies")
        return cls(
            namespace,
            provider_id or str(message.get("Message-ID", "")),
            str(message.get("From", "")),
            str(message.get("Subject", "")),
            "\n".join(parts["text/plain"]),
            "\n".join(parts["text/html"]),
        )
