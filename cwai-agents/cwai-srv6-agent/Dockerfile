# srv6-migration-agent container image (Cisco Crosswork AI ADK, langchain extra).
#
# Build (from this dir):
#   docker build -f Dockerfile -t srv6-migration-agent:1.0.0 .
FROM python:3.12-slim

WORKDIR /app

# Install the ADK from the bundled wheel (langchain extra = ADKChatModel / OTELGraphHandler).
COPY cwai_adk-2.0.0rc2.dev0-py3-none-any.whl ./
RUN pip install --no-cache-dir "cwai_adk-2.0.0rc2.dev0-py3-none-any.whl[langchain]"

# Agent source; agent.start() runs the message handler when the container starts.
COPY main.py recorded-fabric.json ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
