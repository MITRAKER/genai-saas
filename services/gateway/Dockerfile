FROM python:3.12.16-slim

# Don't write .pyc files, and send logs straight to Docker instead of buffering.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
# Dependencies first: Docker reuses this layer until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The code stays owned by root, so the app can read it but never change it.
COPY app ./app

# Run as an unprivileged user: a compromised process gets fewer privileges.
# A numeric id works even where the host has no "appuser" in /etc/passwd.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home appuser
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
