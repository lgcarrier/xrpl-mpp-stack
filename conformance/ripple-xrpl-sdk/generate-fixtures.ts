/**
 * Generate deterministic cross-language fixtures from ripple/xrpl-mpp-sdk.
 *
 * CI copies this file into the pinned SDK checkout's scripts/ directory before
 * running it. Keeping the imports relative to that checkout makes both method
 * schemas and mppx Receipt validation come from the locked TypeScript tree.
 */

import { Receipt } from 'mppx'
import { charge } from '../sdk/src/Methods.js'
import { channel } from '../sdk/src/channel/Methods.js'
import { challengeInvoiceId } from '../sdk/src/utils/binding.js'

const EXPECTED_REF = '6907484c92d217da406e2f3d7b5e6587703c6ea8'
const activeRef = process.env.RIPPLE_XRPL_MPP_SDK_REF
if (activeRef !== EXPECTED_REF) {
  throw new Error(`RIPPLE_XRPL_MPP_SDK_REF must equal ${EXPECTED_REF}`)
}

const PAYER = 'rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe'
const RECIPIENT = 'rf5kMNrUqgLzJT8YUzxM1pptc5r3Lfx1J9'
const CHANNEL_ID = 'AB'.repeat(32)
const SIGNATURE = 'CD'.repeat(64)
const TX_HASH = 'EF'.repeat(32)
const TIMESTAMP = '2026-08-30T12:00:00Z'

const chargeChallengeId = 'charge-fixture-001'
const openChallengeId = 'session-open-fixture-001'
const voucherChallengeId = 'session-voucher-fixture-001'
const closeChallengeId = 'session-close-fixture-001'

function receipt(reference: string, externalId: string): unknown {
  return Receipt.from({
    status: 'success',
    method: 'xrpl',
    timestamp: TIMESTAMP,
    reference,
    externalId,
  })
}

const chargeRequest = charge.schema.request.parse({
  amount: '1000000',
  currency: 'XRP',
  recipient: RECIPIENT,
  description: 'deterministic charge fixture',
  externalId: 'order-001',
  methodDetails: {
    reference: 'charge-reference-001',
    network: 'testnet',
    invoiceId: '01'.repeat(32),
    destinationTag: 7,
    sourceTag: 593184257,
    memos: [{ type: 'text/plain', data: 'order-001' }],
  },
})

const sessionRequest = channel.schema.request.parse({
  amount: '50000',
  currency: 'XRP',
  channelId: CHANNEL_ID,
  recipient: RECIPIENT,
  description: 'deterministic session fixture',
  externalId: 'session-001',
  methodDetails: {
    reference: 'session-reference-001',
    network: 'testnet',
    cumulativeAmount: '200000',
  },
})

const output = {
  source: {
    repository: 'https://github.com/ripple/xrpl-mpp-sdk',
    commit: EXPECTED_REF,
    packageVersion: '0.1.0',
    generator: 'conformance/ripple-xrpl-sdk/generate-fixtures.ts',
  },
  constants: {
    payer: PAYER,
    recipient: RECIPIENT,
    channelId: CHANNEL_ID,
    signature: SIGNATURE,
    txHash: TX_HASH,
    timestamp: TIMESTAMP,
  },
  invoiceId: {
    challengeId: chargeChallengeId,
    value: challengeInvoiceId(chargeChallengeId),
  },
  charge: {
    method: { name: charge.name, intent: charge.intent },
    request: chargeRequest,
    payloads: {
      transaction: charge.schema.credential.payload.parse({
        type: 'transaction',
        blob: '12000022',
      }),
      hash: charge.schema.credential.payload.parse({
        type: 'hash',
        hash: TX_HASH,
      }),
    },
    receipt: receipt(TX_HASH, chargeChallengeId),
  },
  session: {
    method: { name: channel.name, intent: channel.intent },
    request: sessionRequest,
    openRequest: channel.schema.request.parse({
      amount: '0',
      currency: 'XRP',
      channelId: '',
      recipient: RECIPIENT,
      methodDetails: { network: 'testnet', cumulativeAmount: '0' },
    }),
    payloads: {
      open: channel.schema.credential.payload.parse({
        action: 'open',
        transaction: '12000022',
        amount: '0',
        signature: SIGNATURE,
      }),
      voucher: channel.schema.credential.payload.parse({
        action: 'voucher',
        channelId: CHANNEL_ID,
        amount: '250000',
        signature: SIGNATURE,
      }),
      close: channel.schema.credential.payload.parse({
        action: 'close',
        channelId: CHANNEL_ID,
        amount: '250000',
        signature: SIGNATURE,
      }),
    },
    receipts: {
      open: receipt(`open:${CHANNEL_ID}:${TX_HASH}`, openChallengeId),
      voucher: receipt(`${CHANNEL_ID}:250000`, voucherChallengeId),
      close: receipt(`${CHANNEL_ID}:250000`, closeChallengeId),
    },
  },
}

process.stdout.write(`${JSON.stringify(output, null, 2)}\n`)
