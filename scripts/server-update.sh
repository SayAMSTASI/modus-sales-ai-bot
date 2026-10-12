#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE=${1:-/opt/modus-sales-bot/.env.production}
TARGET_REF=${2:-origin/main}
GIT_REMOTE=${3:-origin}
export ENV_FILE

if [ ! -f "$ENV_FILE" ] || [ ! -r "$ENV_FILE" ]; then
    echo "Production env file is missing or unreadable: $ENV_FILE" >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    echo "Git CLI is not installed." >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker CLI is not installed." >&2
    exit 1
fi

cd "$project_root"
if [ -n "$(git status --porcelain)" ]; then
    echo "Repository has local changes; update cancelled to avoid overwriting them." >&2
    exit 1
fi

previous_commit=$(git rev-parse HEAD)
git fetch --prune "$GIT_REMOTE"
target_commit=$(git rev-parse --verify "${TARGET_REF}^{commit}")
if ! git merge-base --is-ancestor "$previous_commit" "$target_commit"; then
    echo "Target $TARGET_REF is not a fast-forward from $previous_commit." >&2
    exit 1
fi
git merge --ff-only "$TARGET_REF"

current_commit=$(git rev-parse HEAD)
short_commit=$(git rev-parse --short=12 HEAD)
state_dir=$(dirname -- "$ENV_FILE")/deploy-state
backup_dir=$state_dir/backups
mkdir -p "$backup_dir"

export SALES_BOT_IMAGE="${SALES_BOT_IMAGE_REPOSITORY:-modus-sales-bot}:$short_commit"
docker compose --env-file "$ENV_FILE" config --quiet
docker compose --env-file "$ENV_FILE" up -d db

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_file=$backup_dir/pre-update-$timestamp-$short_commit.sql
docker compose --env-file "$ENV_FILE" exec -T db \
    sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' >"$backup_file"
chmod 600 "$backup_file"

if ! ./scripts/server-up.sh "$ENV_FILE"; then
    echo "Deployment failed." >&2
    echo "Previous commit: $previous_commit" >&2
    echo "Database backup: $backup_file" >&2
    echo "Automatic rollback is intentionally disabled because migrations may need review." >&2
    exit 1
fi

printf '%s\n' "$previous_commit" >"$state_dir/previous-commit.txt"
printf '%s\n' "$current_commit" >"$state_dir/current-commit.txt"
printf '%s\n' "$SALES_BOT_IMAGE" >"$state_dir/current-image.txt"

echo "Production update completed."
echo "Commit: $current_commit"
echo "Image: $SALES_BOT_IMAGE"
echo "Backup: $backup_file"
