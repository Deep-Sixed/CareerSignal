from email.message import EmailMessage

import pytest

from communications.message import Message, html_text
from recruiting.extraction import extract


@pytest.mark.parametrize(
    "tag",
    [
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
    ],
)
@pytest.mark.parametrize("attribute", ['aria-hidden="true"', "hidden"])
def test_hidden_void_element_does_not_hide_following_job(tag, attribute):
    html = (
        f"<{tag} {attribute}>"
        "<h2>Engineer</h2><p>Company: Example Company</p>"
        '<a href="https://jobs.example.com/123">Apply</a>'
    )
    items = extract(html_text(html))
    assert len(items) == 1 and items[0].opportunity
    assert items[0].opportunity.url == "https://jobs.example.com/123"


@pytest.mark.parametrize("link_text", ["Apply", "Apply now", "View job", "<span>Apply</span> now"])
def test_navigation_links_do_not_conflict_with_job_link(link_text):
    html = (
        '<nav><a href="https://example.com/home">Home</a></nav>'
        "<h2>Engineer</h2><p>Company: Example Company</p>"
        '<a href="https://example.com/company">Company website</a>'
        f'<a href="https://jobs.example.com/123">{link_text}</a>'
        '<footer><a href="https://example.com/preferences">Preferences</a></footer>'
    )
    items = extract(html_text(html))
    assert len(items) == 1 and items[0].opportunity
    assert items[0].opportunity.url == "https://jobs.example.com/123"


def test_unrecognized_link_cannot_supply_missing_job_url():
    items = extract(
        html_text(
            "<h2>Engineer</h2><p>Company: Example Company</p>"
            '<a href="https://example.com/home">Home</a>'
        )
    )
    assert items[0].opportunity is None
    assert items[0].reason == "Missing required fields: url"


def test_nested_hidden_container_still_hides_content_and_then_resumes():
    text = html_text("<div hidden><img hidden><span>Secret</span></div><p>Visible</p>")
    assert text == "Visible"


def test_normalization_and_stable_namespace_identity():
    first = Message("mailbox-a", "id-1", text="Title: Engineer\r\nCompany: Example Company  \r\n")
    second = Message("mailbox-a", "id-1", text="Title: Engineer\nCompany: Example Company")
    assert first == second and first.digest == second.digest
    assert first.key != Message("mailbox-b", "id-1", text=first.text).key
    assert first.key == Message("mailbox-a", "id-1", text="changed").key
    assert first.digest != Message("mailbox-a", "id-1", text="changed").digest
    assert Message("local", text="same").key == Message("local", text="same").key
    assert Message("local", text="same").key != Message("local", text="different").key


def test_html_visible_text_and_href_without_script_content():
    html = (
        "<h2>Engineer &amp; Analyst</h2><p>Company: Example Company</p>"
        '<p>Apply: <a href="https://jobs.example.com/1">Apply now</a></p>'
        "<script>Company: Fake</script><div hidden>Title: Hidden</div>"
    )
    text = html_text(html)
    assert "Title: Engineer & Analyst" in text
    assert "Apply: https://jobs.example.com/1" in text
    assert "Fake" not in text and "Hidden" not in text


def test_mime_alternatives_not_double_counted_and_attachments_ignored():
    msg = EmailMessage()
    msg["Message-ID"] = "<synthetic-1@example.com>"
    msg["From"] = "recruiter@example.com"
    msg["Subject"] = "Synthetic alert"
    msg.set_content("Role: Engineer\nCompany: Example Company")
    msg.add_alternative("<h2>Duplicate alternative</h2>", subtype="html")
    msg.add_attachment(
        b"private attachment is not parsed",
        maintype="application",
        subtype="octet-stream",
        filename="attachment.bin",
    )
    parsed = Message.from_bytes(msg.as_bytes(), namespace="test-mailbox")
    assert parsed.external_id == "<synthetic-1@example.com>"
    assert parsed.format == "text" and "Duplicate" not in parsed.content
    assert "attachment" not in parsed.content
    override = Message.from_bytes(
        msg.as_bytes(), namespace="test-mailbox", provider_id="provider-1"
    )
    assert override.external_id == "provider-1"


def test_attached_forwarded_message_is_not_intake():
    main = EmailMessage()
    main.set_content("Body")
    attachment = EmailMessage()
    attachment.set_content("Role: hidden attachment")
    main.add_attachment(attachment)
    parsed = Message.from_bytes(main.as_bytes(), namespace="local")
    assert parsed.text == "Body"


@pytest.mark.parametrize(
    "raw",
    [
        b'Content-Type: multipart/mixed; boundary="missing"\r\n\r\ninvalid',
        b"Content-Type: text/plain; charset=not-a-charset\r\n\r\nbody",
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n\xff",
        b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!",
    ],
)
def test_malformed_mime_is_explicit_failure(raw):
    with pytest.raises(ValueError):
        Message.from_bytes(raw, namespace="local")


def test_empty_and_oversized_messages_rejected():
    with pytest.raises(ValueError):
        Message("local")
    with pytest.raises(ValueError):
        Message("", text="body")
    with pytest.raises(ValueError):
        Message("local", text="a" * 2_000_001)


@pytest.mark.parametrize(
    "style",
    [
        "display:none",
        "DISPLAY: NONE",
        "display:none !important",
        "max-height:0;display:none;overflow:hidden",
        "visibility:hidden",
        "visibility: hidden;",
    ],
)
def test_inline_hidden_preheader_cannot_contaminate_a_visible_job(style):
    """Bulk email hides preheaders with inline styles, not the hidden attribute."""
    text = html_text(
        f'<div style="{style}">Title: Preheader Junk</div>'
        "<h2>Engineer</h2><p>Company: Example Company</p>"
        '<a href="https://jobs.example.com/1">Apply</a>'
    )
    assert "Preheader Junk" not in text
    items = extract(text)
    assert len(items) == 1 and items[0].opportunity
    assert items[0].opportunity.title == "Engineer"
    assert items[0].opportunity.url == "https://jobs.example.com/1"


def test_inline_style_that_does_not_hide_is_left_visible():
    assert "Shown" in html_text('<div style="color:red;margin:0">Shown</div>')


def test_visible_content_after_an_inline_hidden_block_survives():
    assert html_text('<div style="display:none">Secret</div><p>Visible</p>') == "Visible"


@pytest.mark.parametrize(
    "opening",
    ["<div hidden>", '<span aria-hidden="true">', '<div style="display:none">', "<script>"],
)
def test_unterminated_hidden_container_is_explicit_malformed_input(opening):
    """Silently hiding the remainder would drop real jobs and report an empty message."""
    with pytest.raises(ValueError, match="Malformed HTML"):
        html_text(opening + "Swallowed<h2>Engineer</h2><p>Company: Example Company</p>")
