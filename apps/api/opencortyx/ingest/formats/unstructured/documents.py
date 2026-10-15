"""Documents → text: PDF, Office (docx/pptx/odt), and plain-text formats.

Chunking the text is the unstructured pipeline's job (`ingest/unstructured/chunking.py`). Optional heavy libs (pymupdf4llm/pypdf for PDF, langdetect) are imported lazily so this
module always imports even when they are absent.
"""
import hashlib
import io
import os
import re
import tempfile
import zipfile
from typing import Any, Dict

#: Office documents: zip containers of XML, never text. Decoding their bytes as UTF-8 (the old
#: fall-through) ingested the compressed archive as prose, chunks of `PK\x03\x04 word/styles.xml`.
OFFICE_EXTENSIONS = frozenset({"docx", "pptx", "odt", "odp"})
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

    if mime_type == "application/pdf" or extension == "pdf":
        raw_text = _parse_pdf(content)
    elif extension == "eml":
        raw_text = _parse_eml(content)
    elif extension in OFFICE_EXTENSIONS:
        raw_text = _parse_office(content, extension)
    elif extension in ("txt", "md", "rtf", "tex", "log", "rst", "html", "htm"):
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


#: Page separators a converter adds between pages ('-----' from pymupdf4llm). Not content: a PDF that is
#: nothing but separators has no readable text.
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


def _parse_pdf(content: bytes) -> str:
    try:
        import pymupdf4llm
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            return pymupdf4llm.to_markdown(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(p.extract_text() for p in reader.pages if p.extract_text())
    except Exception as e:
        raise UnreadableDocument(f"not a readable PDF ({e})") from e


def _parse_office(content: bytes, extension: str) -> str:
    """Office document → Markdown through Docling, so headings and tables survive into chunking;
    the XML text alone when Docling cannot read it."""
    if extension in ("docx", "pptx"):
        try:
            from opencortyx.ingest.formats.unstructured.docling.engine import DoclingEngine
            converter = DoclingEngine.get_converter()
            if converter is not None:
                with tempfile.NamedTemporaryFile(suffix="." + extension, delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                try:
                    text = converter.convert(tmp_path).document.export_to_markdown()
                    if text.strip():
                        return text[:200000]
                finally:
                    os.remove(tmp_path)
        except Exception:                                   # noqa: BLE001 — the XML read below
            pass
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
