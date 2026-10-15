"""TOML → the JSON tree the walker reads (.toml). Tables become objects."""
from __future__ import annotations

from ..base import Parser, ParseError, ParseResult


class TomlParser(Parser):
    name = "toml"
    label = "TOML"
    extensions = ("toml",)
    mime_types = ("application/toml",)
    priority = 45

    @classmethod
    def sniff(cls, content: bytes) -> float:
        head = content[:512].lstrip()
        if head.startswith(b"["):
            # `[section]` on its own line is TOML; `[1,2,3]` is JSON.
            first = head.split(b"\n", 1)[0].strip()
            if first.endswith(b"]") and b"," not in first and b'"' not in first[:1]:
                return 0.8
        for line in content[:1024].split(b"\n")[:8]:
            s = line.strip()
            if not s or s.startswith(b"#"):
                continue
            if b"=" in s:
                return 0.3
            break
        return 0.0

    def parse(self, content: bytes, filename: str = "") -> ParseResult:
        try:
            import tomllib                                       # py3.11+
        except ImportError:                                      # pragma: no cover
            try:
                import tomli as tomllib                          # type: ignore
            except ImportError as e:
                raise ParseError("tomllib/tomli is required to parse TOML") from e
        try:
            data = tomllib.loads(self.decode_text(content))
        except Exception as e:                                   # noqa: BLE001
            raise ParseError(f"invalid TOML: {str(e)[:200]}") from e
        if not self.is_container(data):
            raise ParseError("TOML parsed to nothing usable")
        return ParseResult(data=data, parser=self.name, root_name="document",
                           metadata={"tables": len([k for k, v in data.items()
                                                    if isinstance(v, dict)])})
