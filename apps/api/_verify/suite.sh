cd /app && python -m ruff check --fix --select F401 opencortyx/drafting/sampling.py opencortyx/ingest/unstructured/docling/assembler.py opencortyx/ingest/walkers/unstructured.py >/dev/null 2>&1
echo "=== ruff ==="; python -m ruff check opencortyx/ tests/ 2>&1 | tail -4
echo "=== suite ==="; python -W ignore -m pytest -q -p no:warnings 2>&1 | tail -8
