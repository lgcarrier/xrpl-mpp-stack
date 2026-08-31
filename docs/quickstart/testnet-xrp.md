# Testnet XRP quickstart

This path exercises a one-time `xrpl` / `charge` exchange. Use disposable
Testnet wallets only.

## Install

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

## Configure

Set these values in `.env`:

```dotenv
NETWORK_ID=testnet
XRPL_NETWORK=testnet
XRPL_RPC_URL=https://s.altnet.rippletest.net:51234/
MY_DESTINATION_ADDRESS=rSellerTestnetAddress
MERCHANT_XRPL_ADDRESS=rSellerTestnetAddress
FACILITATOR_BEARER_TOKEN=replace-with-a-random-gateway-token
MPP_CHALLENGE_SECRET=replace-with-a-long-random-challenge-key
REDIS_URL=redis://127.0.0.1:6379/0
XRPL_WALLET_SEED=sBuyerTestnetSeed
XRPL_MPP_EXPECTED_RECIPIENT=rSellerTestnetAddress
XRPL_MPP_MAX_SPEND=0.001
PAYMENT_CURRENCY=XRP
PRICE_AMOUNT=1000
PRICE_CURRENCY=XRP
```

The payer account needs enough Testnet XRP for the exact amount and network
fee. The destination must match the seller and facilitator configuration.

## Run

Start Redis, the facilitator, and the merchant through the development Compose
profile:

```bash
docker compose up --build redis facilitator merchant
```

Then run the trace buyer in another terminal:

```bash
docker compose --profile demo run --rm buyer
```

The Compose merchant explicitly opts into unencrypted HTTP for its private
container-to-container facilitator hop. The seller examples permit plaintext
only for a literal loopback facilitator; their normal client requires HTTPS for
every remote origin. Do not translate this development command into a
plaintext production deployment.

The buyer first receives `402`, signs the authoritative challenge, retries once,
and prints the unlocked response. Inspect the returned `Payment-Receipt` only on
the successful response.

## Verify the full live path

The external-network test is not part of normal `pytest`:

```bash
RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s
```

Faucets and public RPC endpoints can be temporarily unavailable. A skipped or
blocked live test does not replace the deterministic local suite.
