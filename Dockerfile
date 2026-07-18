FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY maea_gateway/ ./maea_gateway/
COPY config/ ./config/

EXPOSE 8080

CMD ["uvicorn", "maea_gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
