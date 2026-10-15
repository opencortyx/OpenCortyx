"""YAML → the JSON tree the walker reads (.yaml .yml). A multi-document stream becomes records.

YAML gets its own parser rather than sharing JSON's because it is a genuinely different model: a
stream may hold SEVERAL documents (`---` separated), and it is a near-superset of JSON — which is
why its priority sits below JSON's and why it refuses scalar results. Without that refusal,
`yaml.safe_load("Hello world")` returns the string and every prose file in the corpus would be
claimed as valid YAML.
"""
from __future__ import annotations

from ..base import Parser, ParseError, ParseResult, Record



def _string_key_loader():
    """A SafeLoader that keeps mapping KEYS as the text that was written.

    YAML 1.1 resolves bare `on`, `off`, `yes`, `no`, `true`, `false` to booleans — the "Norway
    problem" (`NO` becomes False). That is defensible for values, but as a KEY it is destructive:
    a GitHub-workflow `on:` becomes the Python object `True`, so the key is no longer addressable by
    name and any code doing `key.upper()` raises `'bool' object has no attribute 'upper'` — which is
    exactly how this surfaced.

    Overriding key construction (rather than disabling the bool resolver globally) keeps VALUES
    correctly typed — `enabled: yes` is still True — while `on:` stays the string "on".
    """
    import yaml

    class _KeyPreservingLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        out = {}
        for key_node, value_node in node.value:
            # Use the raw scalar text for keys, so "on" stays "on" rather than becoming True.
            if isinstance(key_node, yaml.ScalarNode):
                key = key_node.value
            else:
                key = loader.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    key = str(key)
            out[key] = loader.construct_object(value_node, deep=deep)
        return out

    _KeyPreservingLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
    return _KeyPreservingLoader


class YamlParser(Parser):
    name = "yaml"
    label = "YAML"
    extensions = ("yaml", "yml")
    mime_types = ("application/x-yaml", "text/yaml")
    # BELOW JsonParser on purpose: YAML parses most JSON, so JSON must claim JSON first.
    priority = 40

    @classmethod
    def sniff(cls, content: bytes) -> float:
        head = content[:512].lstrip()
        if not head or head[:1] in (b"{", b"[", b"<"):
            return 0.0
        if head.startswith(b"---"):
            return 0.85
        # `key: value` on an early line is the usual YAML shape. Weak on purpose — prose containing
        # a colon must not be claimed, so the registry verifies by parsing.
        for line in content[:1024].split(b"\n")[:8]:
            s = line.strip()
            if not s or s.startswith(b"#"):
                continue
            if b":" in s and not s.endswith(b":") and b" " not in s.split(b":", 1)[0]:
                return 0.35
            break
        return 0.0

    def parse(self, content: bytes, filename: str = "") -> ParseResult:
        try:
            import yaml
        except ImportError as e:                                  # pragma: no cover
            raise ParseError("PyYAML is required to parse YAML") from e
        try:
            docs = [d for d in yaml.load_all(self.decode_text(content), _string_key_loader())
                    if d is not None]
        except yaml.YAMLError as e:
            raise ParseError(f"invalid YAML: {str(e)[:200]}") from e
        if not docs:
            raise ParseError("YAML stream contained no documents")
        if not any(self.is_container(d) for d in docs):
            raise ParseError("YAML parsed to a scalar, not a mapping or sequence")

        if len(docs) == 1:
            return ParseResult(data=docs[0], parser=self.name, root_name="document",
                               metadata={"documents": 1})
        # A multi-document stream is a record collection, same shape as JSONL.
        records = [Record(data=d, index=i, location=f"document {i + 1}") for i, d in enumerate(docs)]
        return ParseResult(data={"records": docs}, parser=self.name, records=records,
                           root_name="records", metadata={"documents": len(docs)})
