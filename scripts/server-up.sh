#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE=${1:-/opt/modus-sales-bot/.env.production}
export ENV_FILE

if [ ! -f "$ENV_FILE" ]; then
    echo "Production env file not found: $ENV_FILE" >&2
    exit 1
fi
if [ ! -r "$ENV_FILE" ]; then
    echo "Production env file is not readable by the service user: $ENV_FILE" >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker CLI is not installed." >&2
    exit 1
fi

cd "$project_root"
docker compose --env-file "$ENV_FILE" config --quiet
docker compose --env-file "$ENV_FILE" up -d --build db migrate web worker

attempt=0
while [ "$attempt" -lt 30 ]; do
    if docker compose --env-file "$ENV_FILE" exec -T web \
        python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).read()" \
        >/dev/null 2>&1; then
        docker compose --env-file "$ENV_FILE" run --rm --no-deps web \
            python scripts/check_keycloak_config.py
        if [ "${REGISTER_TELEGRAM_WEBHOOK:-true}" = "true" ]; then
            public_base_url=$(docker compose --env-file "$ENV_FILE" run --rm --no-deps web \
                python -c "from app.config import Settings; print(Settings().public_base_url)")
            docker compose --env-file "$ENV_FILE" run --rm --no-deps web \
                python scripts/register_webhook.py --url "$public_base_url"
        fi
        docker compose --env-file "$ENV_FILE" ps
        echo "Sales bot is ready."
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done

docker compose --env-file "$ENV_FILE" ps >&2
echo "Readiness check failed after 60 seconds." >&2
exit 1
