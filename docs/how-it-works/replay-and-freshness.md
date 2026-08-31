# Replay, freshness, and binding

Payment proof is useful only when it is tied to the challenge and accepted once.

## Challenge binding

The stack validates:

- the echoed challenge object and ID;
- method and intent;
- RFC 3339 expiry;
- optional request digest;
- HMAC-bound challenge fields and rotation key;
- named XRPL network;
- payer source DID and transaction account;
- recipient, currency, exact amount, invoice, tags, and memos.

For charges, the challenge `invoiceId` (or its deterministic challenge-derived
value) must equal the transaction `InvoiceID`; arbitrary truncation is never a
fallback. PayChannel sessions bind `channelId` and the prior `cumulativeAmount`
into each new challenge.

A buyer must never sign from discovery metadata alone. Discovery can be stale;
the runtime challenge contains the authoritative terms.

## Charge replay state

Before settlement, the facilitator reserves the payment reference in Redis. A
concurrent duplicate cannot be submitted twice. On success, the marker is
committed until at least the authenticated challenge expiry, plus the ledger
validation window and a clock-skew margin. The configured pending and processed
TTLs are floors, not caps, so a short operator setting cannot reopen replay while
the credential can still settle. A charge without a usable authenticated expiry
fails closed because no safe finite retention can be derived. On a safe,
definitive submission failure, the reservation can be released according to
settlement state; an ambiguous result stays reserved for reconciliation.

Charge invoice and transaction/blob replay keys include the named XRPL network.
This isolates mainnet, testnet, and devnet markers when multiple facilitator
deployments share Redis. Upgrading from an earlier release starts a new
network-scoped key space; keep the old unscoped keys until their configured TTLs
expire, but do not copy them into every network because that would recreate the
cross-network collision.

Push mode validates the referenced transaction rather than trusting a hash.
Pull mode decodes and validates the blob before submission. In both modes the
ledger result and freshness window are checked.

Every pull-mode charge transaction and every PayChannel open must carry
`LastLedgerSequence`; otherwise a stolen signed blob could remain submitable
indefinitely. The bound must still be live relative to the validated ledger. If
the challenge expires, it must additionally not outlast that authenticated
expiry. If `CancelAfter` is present, it must leave the configured recipient
settlement margin before the channel can disappear.

## PaymentChannel high-water state

The atomic channel record binds the channel to network, payer, recipient, and
signing key. A voucher is accepted only when its cumulative amount is greater
than the stored high-water amount. Equal or lower amounts are replay or rollback
attempts.

All facilitator replicas must share the same Redis state. Do not enforce this
rule with process-local memory in a multi-replica production deployment.

Each PayChannel challenge ID is also claimed atomically after the credential's
party binding, claim signature, and ledger state have been verified, but before
the channel high-water mark advances. This prevents one valid challenge from
authorizing multiple successively higher cumulative claims without letting a
malformed proof consume the challenge. The marker outlives `challenge.expires`
plus a clock-skew margin; when `expires` is absent, it is retained indefinitely
because no finite replay window is safe.

## Key rotation

`MPP_CHALLENGE_SECRET` signs new challenges.
`MPP_CHALLENGE_PREVIOUS_SECRETS` verifies challenges signed before rotation.
Keep previous keys until their maximum challenge lifetime has elapsed, then
remove them. Never place challenge keys in buyer-visible configuration.

## Operational logging

Log stable identifiers, state transitions, settlement results, and safe failure
codes. Redact or omit:

- payment credential values;
- signed transaction blobs and claim signatures;
- wallet seeds and private keys;
- seller gateway bearer tokens;
- full receipt headers when not operationally necessary.
