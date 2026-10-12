# qos-migration-agent container image (Cisco Crosswork AI ADK, langchain extra).
FROM python:3.12-slim

WORKDIR /app

COPY cwai_adk-2.0.0rc2.dev0-py3-none-any.whl ./
RUN pip install --no-cache-dir "cwai_adk-2.0.0rc2.dev0-py3-none-any.whl[langchain]"

# Agent source + bundled skill and sample source configs.
COPY main.py ./
COPY skills ./skills
COPY sources ./sources

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
