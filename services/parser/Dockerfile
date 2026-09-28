ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp/parser
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-impress poppler-utils fonts-dejavu-core fonts-liberation && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY vendor/pptx-template-parser /app/upstream
RUN pip install --no-cache-dir /app/upstream
COPY services/parser/bridge.py /app/bridge.py
RUN mkdir -p /data/jobs && chown -R 1000:1000 /data
USER 1000:1000
CMD ["python", "/app/bridge.py"]
