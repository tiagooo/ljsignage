#!/bin/sh
# Arranque do serviço web: migrações, leitura do devices.yaml e gunicorn.
# O worker só arranca depois de o web estar saudável (docker-compose.yml).
set -e

flask db upgrade
if ! flask devices sync; then
    echo "AVISO: config/devices.yaml não foi carregado (ver a mensagem acima)." >&2
fi

exec gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 2 \
    --worker-class gthread \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    "lj_signage:create_app()"
