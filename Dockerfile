FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN echo "[BUILD] Installing Python dependencies..." \
    && pip install --no-cache-dir --disable-pip-version-check --quiet -r requirements.txt \
    && echo "[BUILD] Dependencies installed successfully."

COPY src ./src

CMD ["python", "-m", "src.bot"]
