FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . .
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install --no-cache-dir -c requirements.lock . \
    && groupadd --gid 10001 ama \
    && useradd --uid 10001 --gid ama --no-create-home ama \
    && mkdir -p /data/files \
    && chown -R ama:ama /data /app
USER ama
CMD ["uvicorn", "ama.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
