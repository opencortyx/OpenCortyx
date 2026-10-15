"""INI / properties → the JSON tree the walker reads (.ini .cfg .properties .conf).

Each section becomes an object of its keys; not routed by default, reached by content sniffing."""
from __future__ import annotations

from ..base import Parser, ParseError, ParseResult


class IniParser(Parser):
    name = "ini"
    label = "INI / properties"
    extensions = ("ini", "cfg", "properties", "conf")
    mime_types = ()
    # Lowest of the config family: `.conf` files are frequently YAML or TOML in practice, so those
    # parsers get first refusal and this catches genuine INI.
    priority = 20

    @classmethod
    def sniff(cls, content: bytes) -> float:
        head = content[:512].lstrip()
        if head.startswith(b"[") and b"]" in head.split(b"\n", 1)[0]:
            return 0.4
        return 0.0

    def parse(self, content: bytes, filename: str = "") -> ParseResult:
        import configparser
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            cp.read_string(self.decode_text(content))
        except configparser.Error as e:
            raise ParseError(f"invalid INI: {str(e)[:200]}") from e
        data = {s: dict(cp.items(s)) for s in cp.sections()}
        if cp.defaults():
            data["DEFAULT"] = dict(cp.defaults())
        if not data:
            raise ParseError("INI file had no sections")
        return ParseResult(data=data, parser=self.name, root_name="config",
                           metadata={"sections": len(data)})
