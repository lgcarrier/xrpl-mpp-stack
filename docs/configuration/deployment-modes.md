# Deployment modes

## Local Testnet development

Run Redis, the facilitator, the seller example, and a buyer on one workstation.
Use `testnet`, a Testnet JSON-RPC endpoint, and disposable Testnet wallets.
The Compose trace keeps plaintext buyer traffic on its private development
network. `XRPLPaymentTransport` itself permits plaintext only for loopback with
the explicit `allow_insecure_localhost=True` opt-in.

```bash
docker compose up --build redis facilitator merchant
python -m examples.buyer_httpx
```

Authorize the buyer with `XRPL_MPP_EXPECTED_RECIPIENT` and a user-asset-unit
`XRPL_MPP_MAX_SPEND`; the example converts XRP to wire-level drops before
constructing its transport policy.

The Compose merchant command explicitly constructs its facilitator client with
`allow_insecure_http=True` for the private `http://facilitator:8000` network
hop. The normal merchant app has no environment switch for this and permits
plaintext only for a literal localhost or loopback facilitator. Use an HTTPS
origin outside this exact development profile.

## Single seller

Use `GATEWAY_AUTH_MODE=single_token` when one trusted seller deployment calls
one facilitator. Store a strong `FACILITATOR_BEARER_TOKEN` in both services,
restrict facilitator ingress, use TLS, and keep API docs disabled.

The bearer token authenticates the seller to the facilitator. It is not an MPP
payment credential and must never be forwarded to buyers.

## Multiple seller gateways

Use Redis-backed gateway authentication when one facilitator serves multiple
sellers. Provision distinct gateway tokens so rate limiting, revocation, and
audit identity are independent. Do not share one public token across tenants.

## Validated settlement

`validated` is the only supported charge settlement mode. Pull mode submits the
signed blob and waits for a validated `tesSUCCESS`; submission acceptance alone
does not authorize access. Push mode resolves the supplied hash and requires a
matching successful validated transaction. Configure a validation timeout that
fits the deployment. A pending or timeout response means the outcome is unknown,
not unpaid: never initiate a fresh payment. Reconcile the returned
`paymentReference` against validated ledger state, then retry the same credential
only after that status check if the original transaction did not settle.

Push-mode transaction hashes still require resolution to a matching payment;
do not treat an arbitrary submitted hash as authorization.

## PaymentChannel deployment

PaymentChannel state requires atomic shared storage. All facilitator replicas
must use the same Redis data set and the same challenge-verification keys. A
replica-local cache cannot safely enforce the cumulative high-water rule.

Set `PAYCHANNEL_PAYER_PUBLIC_KEY` to the allowed funder claim key. The
facilitator verifies the validated ledger channel on every accepted claim path,
including funding, parties, key, settle delay, and closing window.
`PAYCHANNEL_SETTLEMENT_MARGIN_SECONDS` reserves time to redeem before
`Expiration` or `CancelAfter`; it is a rejection boundary, not a warning.

An operator may configure `PAYCHANNEL_RECIPIENT_SEED` to let the facilitator
redeem the retained cumulative claim and await validation. This recipient-side
`PaymentChannelClaim` pays the recipient but does not close/delete the channel
or refund unused XRP. The funder controls `tfClose`; `CancelAfter` is the other
on-ledger release path. Without a recipient signer, MPP `close` durably
finalizes the session and retains its final voucher for a separate redemption
workflow.

Prefer an injected KMS/HSM-backed `RecipientSigner` when the facilitator is
constructed as a library; the environment seed is only the built-in local
adapter. Background redemption is opt-in through
`PAYCHANNEL_REDEEM_INTERVAL_SECONDS`. It is bounded by the configured batch and
fee caps and coordinated across replicas with a Redis lease. Optional idle
finalization closes only the MPP session state after redeeming; recipient
redemption always submits `PaymentChannelClaim` with `Flags=0`, never `tfClose`.

Open or fund a channel outside MPP when desired. `PaymentChannelFund` is an XRPL
operation, not an MPP `session` action. A matching externally opened channel is
imported from validated ledger state on its first voucher/close, and validated
funding increases are adopted before a subsequent cumulative claim advances.
If an MPP channel open returns settlement pending, reconcile its transaction-hash
`paymentReference` and retry the same signed `PaymentChannelCreate` credential;
never create a fresh channel transaction while the original outcome is unknown.

## Native MCP

Embed `xrpl-mpp-mcp` in the MCP server that owns the paid operation. It supports
`tools/call`, `resources/read`, and `prompts/get`, capability advertisement,
root or MCP-nested `_meta`, operation binding, replay-safe processing, and
payment-specific JSON-RPC errors.

Do not create a second HTTP challenge exchange around MCP. The MCP transport is
the protocol boundary for those operations.

## Hooks and outcome relay

Hooks run inside application architecture and should receive identifiers and
outcomes only. Configure timeouts and failure policy explicitly.

The outcome relay accepts only a validated, allowlisted receipt projection. It
requires HTTPS except for an explicit loopback development override, adds an
idempotency key, and rejects secret-bearing keys. It is not a settlement
service, webhook standard, or required MPP component.
