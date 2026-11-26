FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FLEET_SQLITE_PATH=/data/fleet.sqlite3 FLEET_LOG_DIR=/data/logs
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -r -u 1001 fleet && mkdir -p /data/logs && chown -R fleet /data /app \
    && FLEET_SQLITE_PATH=/tmp/build.sqlite3 FLEET_LOG_DIR=/tmp/buildlogs python manage.py collectstatic --noinput -v0
USER fleet
EXPOSE 8000 5027
COPY deploy/docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["sh", "/entrypoint.sh"]
CMD ["web"]
