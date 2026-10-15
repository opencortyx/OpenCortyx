pip install -q --root-user-action=ignore --index-url https://download.pytorch.org/whl/cpu torch torchvision >/dev/null 2>&1
pip install -q --root-user-action=ignore "docling==2.129.0" python-docx "pydantic>=2.10,<3" >/dev/null 2>&1
python -c "import importlib.metadata as m; print('docling', m.version('docling'), 'pydantic', m.version('pydantic'), 'torch', m.version('torch'))"
cd /app
echo "=== app still imports ==="; PYTHONPATH=/app python -W ignore -c "import opencortyx.main; print('ok')" 2>&1 | tail -2
echo "=== probe ==="; PYTHONPATH=/app python -W ignore _verify/probe.py 2>&1 | grep -v "UserWarning\|protected_namespaces\|^$\|warnings.warn" | tail -70
echo "=== unit suite ==="; python -W ignore -m pytest -q -p no:warnings 2>&1 | tail -8
