import re
from typing import Optional, Union, List

from sendgrid.helpers.mail import Mail, From


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HAS_HTML_RE = re.compile(r"<html\b", re.IGNORECASE)
_HAS_BODY_RE = re.compile(r"<body\b", re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img\b", re.IGNORECASE)


def _strip_html_tags(value: str) -> str:
    text = _HTML_TAG_RE.sub(" ", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _normalize_html_document(html_content: str) -> str:
    html = (html_content or "").strip()
    if not html:
        return "<html><body></body></html>"

    has_html = bool(_HAS_HTML_RE.search(html))
    has_body = bool(_HAS_BODY_RE.search(html))

    if has_html and has_body:
        return html
    if has_html and not has_body:
        return html.replace("</html>", "<body></body></html>")
    return f"<html><body>{html}</body></html>"


def _enforce_text_to_image_ratio(
    html_content: str,
    plain_text_content: str,
    min_text_chars_per_image: int = 140,
) -> tuple[str, str]:
    html = html_content or ""
    plain = (plain_text_content or "").strip()

    image_count = len(_IMG_TAG_RE.findall(html))
    if image_count <= 0:
        return html, plain

    current_text_len = len(_strip_html_tags(html))
    required_len = image_count * min_text_chars_per_image
    if current_text_len >= required_len:
        return html, plain

    addendum = (
        "This message contains important information from Happy Client Flow. "
        "If links or images do not display correctly, please contact our support team "
        "or reply to this email for assistance."
    )

    if "</body>" in html.lower():
        html = re.sub(
            r"</body>",
            f"<p>{addendum}</p></body>",
            html,
            flags=re.IGNORECASE,
            count=1,
        )
    else:
        html = f"{html}<p>{addendum}</p>"

    plain = f"{plain}\n\n{addendum}".strip()
    return html, plain


def build_sendgrid_mail(
    *,
    from_email: str,
    to_emails: Union[str, List[str]],
    subject: str,
    html_content: str,
    plain_text_content: str,
    from_name: Optional[str] = None,
) -> Mail:
    if plain_text_content is None:
        raise ValueError("plain_text_content is required and must be explicit.")

    normalized_html = _normalize_html_document(html_content)
    final_html, final_plain = _enforce_text_to_image_ratio(
        normalized_html,
        plain_text_content,
    )

    sender = From(from_email, from_name) if from_name else from_email
    return Mail(
        from_email=sender,
        to_emails=to_emails,
        subject=subject,
        html_content=final_html,
        plain_text_content=final_plain,
    )
