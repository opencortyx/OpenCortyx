FROM python:3.11-slim
WORKDIR /app

# requirements.lock pins every dependency; regenerate it after editing requirements.txt (see README.md).
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --upgrade pip setuptools && pip install --no-cache-dir -r requirements.lock

COPY . .

EXPOSE 8001
ENV MCP_TRANSPORT=sse \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8001 \
    OPENCORTYX_API_URL=http://api:8000

CMD ["python", "server.py"]
