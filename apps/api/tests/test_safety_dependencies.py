"""Libraries whose absence the code survives silently, but less safely, must be installed.

`xml_to_json` falls back to the stdlib parser when defusedxml is missing, which expands entities
(billion-laughs). defusedxml used to arrive with Docling; when Docling was removed it would have
quietly gone with it.
"""


def test_defusedxml_is_installed():
    import defusedxml.ElementTree  # noqa: F401
