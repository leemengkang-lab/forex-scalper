FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy only what's needed for the install so layer cache is effective
COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

# Create a non-root user and the data directory the bot writes its SQLite DB to
RUN useradd --no-create-home --shell /bin/false botuser \
    && mkdir -p /app/data \
    && chown botuser:botuser /app/data

USER botuser

CMD ["python", "-m", "forex_scalper.live"]