"""Documents → text: PDF, Office, HTML, e-books, notebooks, images, and plain-text formats.

The rich formats are read by their layout reader (layout/readers/) and returned as Markdown, so
headings, lists and tables survive into chunking. Chunking the text is the unstructured pipeline's
job (`ingest/unstructured/chunking.py`). langdetect is imported lazily.
"""
import hashlib
import io
import re
import zipfile
from typing import Any, Dict

#: Office documents: zip containers of XML, never text. Decoding their bytes as UTF-8 (the old
#: fall-through) ingested the compressed archive as prose, chunks of `PK\x03\x04 word/styles.xml`.
OFFICE_EXTENSIONS = frozenset({"docx", "pptx", "odt", "odp"})
#: Read by their layout reader (layout/readers/) and returned as Markdown.
LAYOUT_EXTENSIONS = frozenset({
    "pdf", "docx", "docm", "doc", "pptx", "pptm", "ppt", "odt", "odp", "ods", "rtf", "epub", "ipynb",
    "html", "htm", "xhtml", "pages", "key", "numbers", "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff",
    "webp", "svg",
})
#: Formats that can only be read whole: a zip's directory and a PDF's xref sit at the END of the
#: file, so a prefix of either is unreadable. Samplers must hand these over complete.
WHOLE_FILE_EXTENSIONS = OFFICE_EXTENSIONS | {"pdf"}


class UnreadableDocument(ValueError):
    """The file cannot be read as a document: a corrupt archive, a PDF with no extractable text, or
    content that is blank. Raised rather than returned as text, because a marker string such as
    "[unreadable docx: …]" was stored as the document's content and the run reported success."""


def parse_content(content: bytes, mime_type: str = "text/plain", filename: str = "") -> Dict[str, Any]:
    """Bytes → {raw_text, checksum, file_size_bytes, language, word_count, category}.

    Raises UnreadableDocument when nothing readable comes out of the file."""
    file_size_bytes = len(content)
    checksum = hashlib.sha256(content).hexdigest()
    extension = filename.split(".")[-1].lower() if "." in filename else ""
    raw_text, category = "", "document"

    if mime_type == "application/pdf" or extension in LAYOUT_EXTENSIONS:
        raw_text = _layout_markdown(content, filename or f"document.{extension or 'pdf'}", extension)
    elif extension == "eml":
        raw_text = _parse_eml(content)
    elif extension in ("txt", "md", "tex", "log", "rst"):
        raw_text = content.decode("utf-8", errors="ignore")[:200000]
    elif extension in ("json", "xml", "yaml", "yml", "toml"):
        raw_text, category = content.decode("utf-8", errors="ignore")[:50000], "structured"
    elif extension in ("py", "js", "ts", "java", "cpp", "rs", "go", "php", "rb", "sql"):
        raw_text, category = content.decode("utf-8", errors="ignore")[:60000], "code"
    else:
        raw_text = content.decode("utf-8", errors="ignore")[:50000]
    if not _meaningful(raw_text):
        raise UnreadableDocument(f"'{filename or 'file'}' has no readable text")

    return {
        "raw_text": raw_text, "checksum": checksum, "file_size_bytes": file_size_bytes,
        "language": _detect_lang(raw_text), "word_count": len(raw_text.split()), "category": category,
    }


#: Page separators a converter adds between pages ('-----'). Not content: a document that is nothing but
#: separators has no readable text.
_SEPARATOR_LINE = re.compile(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def _meaningful(text: str) -> bool:
    return bool(_SEPARATOR_LINE.sub("", text or "").strip())


def _parse_eml(content: bytes) -> str:
    """An email → its subject and decoded body. The raw bytes carry the body base64- or
    quoted-printable-encoded whenever it is not plain ASCII, so reading them as text stores gibberish."""
    from email import policy
    from email.parser import BytesParser
    msg = BytesParser(policy=policy.default).parsebytes(content)
    parts = [f"Subject: {msg['subject']}"] if msg["subject"] else []
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        parts.append(text)
    return "\n\n".join(parts)[:200000]


def _layout_markdown(content: bytes, filename: str, extension: str) -> str:
    """The document read by its layout reader, as Markdown. An Office file whose reader fails still
    gives its paragraph text, straight from the archive's XML."""
    from opencortyx.ingest.formats.unstructured.layout import LayoutEngine
    try:
        assembler = LayoutEngine.convert(content, filename=filename)
        text = assembler.to_markdown()
        if text.strip() or extension not in _OFFICE_PARTS:
            return text[:200000]
    except Exception as e:                                       # noqa: BLE001 — the XML read below
        if extension not in _OFFICE_PARTS:
            raise UnreadableDocument(f"'{filename}' could not be read ({e})") from e
    return _office_xml_text(content, extension)


#: Where each format keeps its text, and the closing tag that ends a paragraph there.
_OFFICE_PARTS = {
    "docx": (r"word/document\.xml", r"</w:p>"),
    "pptx": (r"ppt/slides/slide\d+\.xml", r"</a:p>"),
    "odt": (r"content\.xml", r"</text:(?:p|h)>"),
    "odp": (r"content\.xml", r"</text:(?:p|h)>"),
}


def _office_xml_text(content: bytes, extension: str) -> str:
    """Paragraph text straight from the archive's XML: no layout, but never the archive bytes."""
    part, para_end = _OFFICE_PARTS[extension]
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = sorted((n for n in z.namelist() if re.fullmatch(part, n)),
                           key=lambda n: [int(d) for d in re.findall(r"\d+", n)] or [0])
            xml = "\n".join(z.read(n).decode("utf-8", errors="ignore") for n in names)
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise UnreadableDocument(f"not a readable .{extension} file ({e})") from e
    xml = re.sub(para_end, "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    for ent, ch in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(ent, ch)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()[:200000]


def _detect_lang(text: str) -> str:
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return detect(text[:2000])[:10] if text.strip() else "en"
    except Exception:
        return "en"


def extract_title(text: str, default: str = "Untitled") -> str:
    if "<title>" in text:
        try:
            return text.split("<title>")[1].split("</title>")[0].strip()
        except Exception:
            pass
    first = (text.split("\n", 1)[0] if text else "").strip()
    return first if 5 < len(first) < 100 else default
