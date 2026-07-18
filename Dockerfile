FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY maea_gateway/ maea_gateway/
COPY config/ /etc/maea/

EXPOSE 8080
ENV MAEA_CONFIG_PATH=/etc/maea/tenants.yaml

CMD ["uvicorn", "maea_gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
