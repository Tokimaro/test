#!/bin/sh
set -e
# миграции БД применяются при каждом старте (идемпотентно)
alembic upgrade head
exec "$@"
