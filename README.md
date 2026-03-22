# xrpl-mpp-stack

Python-first XRPL infrastructure for MPP HTTP payments.

This repo migrates the original XRPL x402 stack onto the MPP HTTP model:

- `xrpl-mpp-core`: shared MPP models, header codecs, challenge helpers, and XRPL asset utilities
- `xrpl-mpp-facilitator`: FastAPI facilitator for XRPL `charge` and `session`
- `xrpl-mpp-middleware`: ASGI middleware that emits `WWW-Authenticate: Payment` and verifies `Authorization: Payment`
- `xrpl-mpp-client`: HTTPX transport and signer for XRPL-backed MPP retries
- `xrpl-mpp-payer`: CLI, proxy, and MCP payer runtime

## Supported intents

- `charge`: one request, one XRPL payment
- `session`: prepaid XRPL session with `open`, `use`, `top_up`, and `close`

## HTTP wire contract

- `402` responses return one or more `WWW-Authenticate: Payment ...` headers
- paid retries use `Authorization: Payment <base64url-jcs-credential>`
- successful paid responses include `Payment-Receipt: <base64url-jcs-receipt>`
- auth-scheme matching for `WWW-Authenticate` and `Authorization` is case-insensitive; docs use canonical `Payment`
- `402` responses use `Cache-Control: no-store`
- successful paid responses use `Cache-Control: private`

Seller-side `PaymentMiddlewareASGI` now rejects protected-route request bodies larger than `32768` bytes by default. Override that ceiling with `PaymentMiddlewareASGI(..., max_request_body_bytes=...)` when needed.

## Local demo

```bash
cp .env.example .env
docker compose up --build facilitator merchant
python -m examples.buyer_httpx
```

`.env.example` is a template. Before you run the demo, fill in `MY_DESTINATION_ADDRESS`, `FACILITATOR_BEARER_TOKEN`, `MPP_CHALLENGE_SECRET`, and `XRPL_WALLET_SEED`, then switch `NETWORK_ID`, `XRPL_NETWORK`, and `XRPL_RPC_URL` to XRPL Testnet values such as `xrpl:1` and `https://s.altnet.rippletest.net:51234`.

`docker compose` passes that same `.env` file into the facilitator and merchant containers, and `python -m examples.buyer_httpx` now auto-loads `.env` from the repo root for local runs.

The merchant example protects `GET /premium` with MPP `charge`. The buyer example signs the XRPL payment, retries automatically, and prints the unlocked response.

## CLI

```bash
xrpl-mpp pay https://merchant.example/premium --amount 0.001 --asset XRP --dry-run
xrpl-mpp proxy https://merchant.example --port 8787
xrpl-mpp mcp
```

## Verification

Focused migration coverage currently lives in the MPP-native test set:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q \
  tests/test_mpp_http.py \
  tests/test_stack_package_exports.py \
  tests/test_xrpl_mpp_package.py \
  tests/test_xrpl_mpp_client.py \
  tests/test_xrpl_mpp_middleware.py \
  tests/test_xrpl_mpp_payer.py \
  tests/test_xrpl_mpp_local_integration.py \
  tests/test_examples.py
```
