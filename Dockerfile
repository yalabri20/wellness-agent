FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000 \
    DEMO_MODE=true \
    API_BASE_URL=http://127.0.0.1:8000 \
    DATABASE_URL=sqlite:///./wellness.db

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

RUN chmod +x /app/deploy/start.sh \
    && mkdir -p /app/.runlogs

EXPOSE 10000

CMD ["/app/deploy/start.sh"]
