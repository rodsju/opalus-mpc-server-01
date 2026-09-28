FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY entrypoint.sh /entrypoint.sh


RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Cloud Run define $PORT automaticamente
# CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers","--reload"]
CMD ["sh", "-c", "mcpo --host 0.0.0.0 --port ${PORT:-8001} --api-key \"$MCP_API_KEY\" -- python /app/src/main.py"]