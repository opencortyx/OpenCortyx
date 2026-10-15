import io

p = "opencortyx/ingest/unstructured/ingest.py"
s = io.open(p, encoding="utf-8").read()


def swap(old, new):
    global s
    assert old in s, old[:90]
    s = s.replace(old, new, 1)


swap('''        from opencortyx.ingest.formats.unstructured.docling.engine import DoclingEngine
        assembler = DoclingEngine.convert(content, filename=filename, doc_id=str(source_id))
        ast_nodes = assembler.nodes''', '''        from opencortyx.ingest.formats.unstructured.docling.engine import DoclingEngine
        # In a thread: layout analysis is seconds of CPU per page, and run inline it froze this
        # worker's event loop — every other coroutine on it (progress writes, heartbeats) stalled.
        assembler = await asyncio.to_thread(
            DoclingEngine.convert, content, filename=filename, doc_id=str(source_id))
        layout_reader = {"engine": assembler.engine, "fallback_reason": assembler.fallback_reason}
        if assembler.engine != "docling":
            log.warning("document.layout_fallback", filename=filename,
                        reason=assembler.fallback_reason)
        ast_nodes = assembler.nodes''')

swap('''            parent_nid = created_nodes.get(ast_node.parent_id) if ast_node.parent_id else source_id''',
     '''            # A top-level element's parent is the document root, whose AST id is the Source id as
            # a string — not a key in `created_nodes`. Looking it up returned None and every
            # top-level heading was written with no CONTAINS edge from its Source at all.
            parent_nid = created_nodes.get(ast_node.parent_id) or source_id''')

if "\nimport asyncio\n" not in s:
    swap("import re\nimport uuid\n", "import asyncio\nimport re\nimport uuid\n")
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched")
