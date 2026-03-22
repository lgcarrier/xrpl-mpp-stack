# xrpl-mpp-facilitator

FastAPI facilitator for XRPL-backed MPP HTTP payments.

## Install

```bash
pip install xrpl-mpp-facilitator
```

## Run

Set the core runtime variables first:

- `MY_DESTINATION_ADDRESS`
- `FACILITATOR_BEARER_TOKEN`
- `REDIS_URL`
- `MPP_CHALLENGE_SECRET`

Then start the service:

```bash
xrpl-mpp-facilitator --reload
```

## Endpoints

- `GET /health`
- `GET /supported`
- `POST /charge`
- `POST /session`

The facilitator validates XRPL-signed payments, settles `charge` requests, and manages Redis-backed state for `session`.

## Python App Factory

```python
from xrpl_mpp_facilitator import create_app

app = create_app()
```

The default settings are loaded from environment variables, including `.env` when present.
