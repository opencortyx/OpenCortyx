pip install -q --root-user-action=ignore --index-url https://download.pytorch.org/whl/cpu torch torchvision >/dev/null 2>&1
pip install -q --root-user-action=ignore "docling==2.129.0" python-docx "pydantic>=2.10,<3" >/dev/null 2>&1
cd /app
echo "=== why did PDF fail in docling ==="
PYTHONPATH=/app python -W ignore - <<'PY' 2>&1 | grep -v "UserWarning\|protected_namespaces\|warnings.warn\|^$" | tail -25
import sys; sys.argv=["x"]
exec(open("_verify/probe.py").read().split("def show")[0])
from docling.document_converter import DocumentConverter
import tempfile, os, traceback
fd, p = tempfile.mkstemp(suffix=".pdf"); os.write(fd, make_pdf()); os.close(fd)
try:
    r = DocumentConverter().convert(p)
    print("docling PDF OK:", [(i.label, getattr(i,'text','')[:30]) for i,_ in r.document.iterate_items()][:12])
except Exception as e:
    print("DOCLING PDF ERROR:", type(e).__name__, str(e)[:600])
PY
echo "=== failing tests, NEW pydantic ==="
python -W ignore -m pytest -q -p no:warnings tests/test_memory_contradiction_path.py 2>&1 | grep -E "Error|error|passed|failed" | head -8
pip install -q --root-user-action=ignore "pydantic==2.9.2" "pydantic-core==2.23.4" >/dev/null 2>&1
echo "=== same tests, ORIGINAL pydantic 2.9.2 ==="
python -W ignore -m pytest -q -p no:warnings tests/test_memory_contradiction_path.py 2>&1 | grep -E "passed|failed" | tail -2
echo "=== full suite, ORIGINAL pydantic ==="
python -W ignore -m pytest -q -p no:warnings 2>&1 | tail -3
