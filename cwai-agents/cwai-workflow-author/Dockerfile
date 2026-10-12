# workflow-author-agent container image (Cisco Crosswork AI ADK, langchain extra).
#
# Build (from this dir):
#   docker build -f Dockerfile -t workflow-author-agent:1.0.0 .
FROM python:3.12-slim

WORKDIR /app

COPY cwai_adk-2.0.0rc2.dev0-py3-none-any.whl ./
RUN pip install --no-cache-dir "cwai_adk-2.0.0rc2.dev0-py3-none-any.whl[langchain]"

COPY main.py ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
