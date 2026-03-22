#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <env-file>" >&2
  exit 1
fi

ASSET_ENV=$1

if [ ! -f "$ASSET_ENV" ]; then
  echo "Env file not found: $ASSET_ENV" >&2
  exit 1
fi

pick_port() {
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

FACILITATOR_PORT=$(pick_port)
MERCHANT_PORT=$(pick_port)
while [ "$MERCHANT_PORT" = "$FACILITATOR_PORT" ]; do
  MERCHANT_PORT=$(pick_port)
done

PROJECT_NAME="demo_$(basename "$ASSET_ENV" | tr '.' '_' )_$$"

FACILITATOR_PORT=$FACILITATOR_PORT MERCHANT_PORT=$MERCHANT_PORT exec docker compose \
  --project-name "$PROJECT_NAME" \
  --env-file "$ASSET_ENV" \
  --profile demo \
  run --rm buyer
