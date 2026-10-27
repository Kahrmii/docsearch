"""Reading mbox mailboxes: split a mailbox into messages and turn each message into index fields.

Standard library only. The file is only read (never opened for writing) and streamed message by message,
so mailboxes of many gigabytes are fine. Attachments are not extracted: their number and names are recorded."""
import datetime as dt
import email
import email.policy
import email.utils
import html.parser
import os
import re

MBOX_EXTENSIONS = {".mbox", ".mbx"}
# The line that starts a message: "From MAILER-DAEMON Tue Oct 27 21:24:26 2026", "From - Mon Jan 01 00:00:00 2018",
# "From 1234@xxx Mon Jan  1 00:00:00 2018". Strict on purpose, so a "From " inside a text is not taken as a separator.
FROM_LINE = re.compile(rb"^From \S+ +\w{3} +\w{3} +\d{1,2} +\d{2}:\d{2}(?::\d{2})? +(?:\S+ +)?\d{4}")
QUOTED_FROM = re.compile(rb"(?m)^>(>*From )")  # mboxrd: "From " at a line start inside a text is quoted with ">"
MIN_YEAR, MAX_YEAR = 1980, 2100  # dates outside are treated as broken


def looks_like_mbox(path):
    """.mbox/.mbx files, and files without extension (Thunderbird) whose first line is a "From " separator."""
    extension = os.path.splitext(path)[1].lower()
    if extension in MBOX_EXTENSIONS:
        return os.path.getsize(path) > 0
    if extension:
        return False
    try:
        with open(path, "rb") as f:
            return bool(FROM_LINE.match(f.readline(500)))
    except OSError:
        return False


def iter_messages(path):
    """(number starting at 1, raw bytes of the message) for every message, streamed from the file."""
    number, current = 0, None
    with open(path, "rb") as f:
        for line in f:
            if line.startswith(b"From ") and FROM_LINE.match(line):
                if current is not None:
                    yield number, QUOTED_FROM.sub(rb"\1", b"".join(current))
                number, current = number + 1, []
            elif current is not None:
                current.append(line)
    if current is not None:
        yield number, QUOTED_FROM.sub(rb"\1", b"".join(current))


def count_messages(path):
    with open(path, "rb") as f:
        return sum(1 for line in f if line.startswith(b"From ") and FROM_LINE.match(line))


# ---- Text of a message ---------------------------------------------------------------------------

class _TextExtractor(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "table", "ul", "ol", "blockquote", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup):
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # broken markup: fall back to removing the tags
        text = re.sub(r"<[^>]*>", " ", markup)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def body_text(msg):
    """Plain text of the message (the HTML part converted to text if there is no plain part)."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # unknown charset or broken encoding
        text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    return html_to_text(text) if part.get_content_type() == "text/html" else text


# ---- Header fields -------------------------------------------------------------------------------

def _header(msg, name):
    try:
        return re.sub(r"\s+", " ", str(msg[name] or "")).strip()
    except Exception:
        return ""


def addresses(msg, name):
    """[(display name, lowercase address)] of From/To/Cc."""
    try:
        values = [str(v) for v in msg.get_all(name, [])]
    except Exception:
        return []
    return [(display.strip(), addr.strip().lower()) for display, addr in email.utils.getaddresses(values) if addr]


def mail_date(msg):
    """Date of the message in UTC, or None if it is missing or broken."""
    try:
        value = email.utils.parsedate_to_datetime(str(msg["Date"]))
    except (TypeError, ValueError, IndexError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    value = value.astimezone(dt.timezone.utc)
    return value if MIN_YEAR <= value.year <= MAX_YEAR else None


def _show(pairs):
    return ", ".join(f"{name} <{addr}>" if name and name != addr else addr for name, addr in pairs)


def build_mail_doc(raw, number, mbox_path, file_mtime, max_chars):
    """One index document for one message. Same fields as for any file, plus the mail_* fields."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    subject = _header(msg, "Subject") or "(no subject)"
    senders, to, cc = addresses(msg, "From"), addresses(msg, "To"), addresses(msg, "Cc")
    date = mail_date(msg)
    try:
        attachments = [part.get_filename() or "(unnamed)" for part in msg.iter_attachments()]
    except Exception:
        attachments = []

    head = [f"Subject: {subject}", f"From: {_show(senders)}", f"To: {_show(to)}"]  # headers are searchable text, too
    if cc:
        head.append(f"Cc: {_show(cc)}")
    if date:
        head.append("Date: " + date.strftime("%Y-%m-%d %H:%M"))
    if attachments:
        head.append("Attachments (not indexed): " + ", ".join(attachments))
    content = re.sub(r"\n{3,}", "\n\n", ("\n".join(head) + "\n\n" + body_text(msg)).replace("\r", "")).strip()
    truncated = len(content) > max_chars
    content = content[:max_chars]

    name = os.path.basename(mbox_path)
    when = date or dt.datetime.fromtimestamp(file_mtime, dt.timezone.utc)
    metadata = {key: value for key, value in (
        ("message_id", _header(msg, "Message-ID")), ("in_reply_to", _header(msg, "In-Reply-To")),
        ("references", _header(msg, "References")), ("list_id", _header(msg, "List-Id"))) if value}
    return {
        "filename": f"{name}#{number}",
        "path": mbox_path.replace("\\", "/"),
        "extension": "mail",
        "size_bytes": len(raw),
        "mtime": when.isoformat(),  # the mail date, so "per year" statistics work for mails
        "content": content,
        "content_length": len(content),
        "content_truncated": truncated,
        "content_type": "message/rfc822",
        "author": senders[0][1] if senders else None,
        "created": date.isoformat() if date else None,
        "page_count": None,
        "language": None,
        "indexed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "metadata": metadata,
        "mail_from": senders[0][1] if senders else None,
        "mail_to": [addr for _, addr in to],
        "mail_cc": [addr for _, addr in cc],
        "mail_subject": subject[:1024],
        "mail_date": date.isoformat() if date else None,
        "mail_message_id": metadata.get("message_id"),
        "mail_attachments": len(attachments),
        "mail_attachment_names": attachments[:50],
        "mail_folder": os.path.splitext(name)[0],
    }
