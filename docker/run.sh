#!/bin/bash
set -e

echo "Running migrations..."
/app/.venv/bin/python manage.py migrate --no-input

echo "Collecting static files..."
/app/.venv/bin/python manage.py collectstatic --no-input

echo "Starting server..."
exec /app/.venv/bin/uvicorn claude_manager.asgi:application \
  --host 0.0.0.0 \
  --port 8080 \
  --proxy-headers \
  --forwarded-allow-ips='*'
