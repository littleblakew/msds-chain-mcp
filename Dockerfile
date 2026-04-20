FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py server_remote.py oauth.py ./

ENV MSDS_MCP_HOST=0.0.0.0
ENV MSDS_MCP_PORT=8080
ENV MSDS_MCP_TRANSPORT=streamable-http

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health')" || exit 1

CMD ["python", "server_remote.py"]
