#!/bin/sh
set -e
case "$1" in
  web)
    export FLEET_PROCESS=web
    python manage.py migrate --noinput
    python manage.py ensure_admin
    if [ "${FLEET_SEED_DEMO:-false}" = "true" ]; then python manage.py seed_demo; fi
    exec gunicorn fleet_project.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2} --access-logfile -
    ;;
  receiver)
    export FLEET_PROCESS=receiver
    # wait for the web container to apply migrations
    i=0; until python manage.py migrate --check >/dev/null 2>&1 || [ $i -ge 30 ]; do i=$((i+1)); sleep 2; done
    exec python manage.py run_receiver --host 0.0.0.0 --port 5027
    ;;
  sim)
    export FLEET_PROCESS=simulator
    shift
    i=0; until python manage.py migrate --check >/dev/null 2>&1 || [ $i -ge 30 ]; do i=$((i+1)); sleep 2; done
    python manage.py seed_demo --devices-only
    exec python manage.py simulate_device "$@"
    ;;
  *)
    exec python manage.py "$@"
    ;;
esac
