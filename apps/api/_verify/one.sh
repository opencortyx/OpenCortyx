cd /app && python -m ruff check opencortyx/ tests/ 2>&1 | grep -E "^[a-z].*F[0-9]+" 
python -W ignore -m pytest -q -p no:warnings tests/test_authoring_drafts.py -k "document_becomes" 2>&1 | grep -E "^E |assert|Error" | head -20
