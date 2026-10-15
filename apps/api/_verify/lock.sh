python -m venv /opt/poetry >/dev/null && /opt/poetry/bin/pip install -q poetry==1.8.2 >/dev/null 2>&1
cd /app && /opt/poetry/bin/poetry lock --no-update > /tmp/lock.log 2>&1; rc=$?
echo "POETRY EXIT $rc"; grep -v "^ *$" /tmp/lock.log | tail -25
