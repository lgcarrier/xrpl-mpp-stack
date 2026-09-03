from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from xrpl.models.requests import AMMInfo, RipplePathFind
from xrpl.wallet import Wallet

from devtools import rlusd_fund as fund
from xrpl_mpp_core import RLUSD_HEX, RLUSD_TESTNET_ISSUER, TF_PARTIAL_PAYMENT


class StubResponse:
    def __init__(self, result: dict, successful: bool = True) -> None:
        self.result = result
        self._successful = successful

    def is_successful(self) -> bool:
        return self._successful


def test_compute_send_max_drops_applies_slippage_and_rounds_up() -> None:
    assert fund.compute_send_max_drops("1234567", 500) == 1_296_296
    assert fund.compute_send_max_drops("1234567", 0) == 1_234_567


def test_build_rlusd_self_payment_is_exact_and_issuer_bound() -> None:
    address = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
    paths = [[{"currency": RLUSD_HEX, "issuer": RLUSD_TESTNET_ISSUER}]]

    payment = fund.build_rlusd_self_payment(
        address=address,
        issuer=RLUSD_TESTNET_ISSUER,
        amount=Decimal("4.25"),
        send_max_drops=12_500_000,
        paths=paths,
    )
    wire = payment.to_xrpl()

    delivered = wire.get("DeliverMax", wire.get("Amount"))
    assert wire["Account"] == address
    assert wire["Destination"] == address
    assert delivered == {
        "currency": RLUSD_HEX,
        "issuer": RLUSD_TESTNET_ISSUER,
        "value": "4.25",
    }
    assert wire["SendMax"] == "12500000"
    assert "Paths" not in wire
    assert int(wire["Flags"]) & TF_PARTIAL_PAYMENT == 0


def test_build_rlusd_self_payment_rejects_noncanonical_issuer() -> None:
    with pytest.raises(fund.FundingError, match="official Testnet issuer"):
        fund.build_rlusd_self_payment(
            address="rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
            issuer="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
            amount=Decimal("1"),
            send_max_drops=2_000_000,
            paths=[],
        )


def test_wallet_file_round_trip_is_private(tmp_path: Path) -> None:
    wallet = Wallet.create()
    wallet_path = tmp_path / "private" / "wallet.json"

    fund.save_wallet_file(wallet_path, wallet, RLUSD_TESTNET_ISSUER)
    loaded = fund.load_wallet_file(wallet_path, RLUSD_TESTNET_ISSUER)

    assert loaded.classic_address == wallet.classic_address
    assert loaded.seed == wallet.seed
    assert wallet_path.stat().st_mode & 0o777 == 0o600
    assert wallet_path.parent.stat().st_mode & 0o777 == 0o700


def test_wallet_file_loader_refuses_symlink(tmp_path: Path) -> None:
    wallet = Wallet.create()
    real_path = tmp_path / "wallet.json"
    link_path = tmp_path / "wallet-link.json"
    fund.save_wallet_file(real_path, wallet, RLUSD_TESTNET_ISSUER)
    os.symlink(real_path, link_path)

    with pytest.raises(fund.FundingError, match="symlink"):
        fund.load_wallet_file(link_path, RLUSD_TESTNET_ISSUER)


def test_existing_default_wallet_directory_is_hardened(
    tmp_path: Path, monkeypatch
) -> None:
    private_directory = tmp_path / ".live-test-wallets"
    private_directory.mkdir(mode=0o755)
    os.chmod(private_directory, 0o755)
    monkeypatch.setattr(fund, "DEFAULT_WALLET_DIRECTORY", private_directory)

    fund._private_directory(private_directory)

    assert private_directory.stat().st_mode & 0o777 == 0o700


def test_atomic_write_failure_preserves_previous_state(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"stable": true}\n', encoding="utf-8")

    def fail_replace(_source, _destination) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(fund.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        fund.atomic_write_private_json(state_path, {"stable": False})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {"stable": True}


def test_load_funding_state_rejects_malformed_json(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(fund.FundingError, match="invalid RLUSD funding state"):
        fund.load_funding_state(
            state_path,
            address="rWallet",
            issuer=RLUSD_TESTNET_ISSUER,
        )


def test_faucet_retry_cooldown_prevents_duplicate_request(
    tmp_path: Path, monkeypatch
) -> None:
    state = fund.RLUSDFundingState(
        classic_address="rWallet",
        issuer=RLUSD_TESTNET_ISSUER,
        last_faucet_attempt_at=fund._utc_now().isoformat(),
    )
    monkeypatch.setattr(fund, "_safe_xrp_balance", lambda *_args: 0)
    monkeypatch.setattr(
        fund,
        "request_testnet_xrp",
        lambda *_args: pytest.fail("faucet must not be called during cooldown"),
    )

    with pytest.raises(fund.SettlementPendingError, match="reconciliation window"):
        fund.ensure_testnet_xrp(
            client=object(),
            address="rWallet",
            minimum_drops=10,
            state=state,
            state_path=tmp_path / "state.json",
        )


def test_uncertain_faucet_outcome_is_journaled_before_returning_pending(
    tmp_path: Path, monkeypatch
) -> None:
    state_path = tmp_path / "state.json"
    state = fund.RLUSDFundingState(
        classic_address="rWallet",
        issuer=RLUSD_TESTNET_ISSUER,
    )
    monkeypatch.setattr(fund, "_safe_xrp_balance", lambda *_args: 0)
    monkeypatch.setattr(
        fund,
        "request_testnet_xrp",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("unknown outcome")),
    )
    monkeypatch.setattr(fund, "_wait_for_xrp_balance", lambda *_args, **_kwargs: 0)

    with pytest.raises(fund.SettlementPendingError, match="outcome is uncertain"):
        fund.ensure_testnet_xrp(
            client=object(),
            address="rWallet",
            minimum_drops=10,
            state=state,
            state_path=state_path,
        )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["last_faucet_attempt_at"] is not None
    assert persisted["last_faucet_balance_drops"] == 0


def test_fund_is_noop_when_target_is_already_met(tmp_path: Path, monkeypatch) -> None:
    wallet = Wallet.create()
    monkeypatch.setattr(
        fund,
        "get_validated_trustline_balance",
        lambda *_args, **_kwargs: Decimal("10"),
    )
    monkeypatch.setattr(fund, "_safe_xrp_balance", lambda *_args: 99_000_000)
    monkeypatch.setattr(
        fund,
        "ensure_testnet_xrp",
        lambda *_args, **_kwargs: pytest.fail("faucet must not run"),
    )

    result = fund.fund_rlusd_wallet(
        client=object(),
        wallet=wallet,
        state_path=tmp_path / "state.json",
        wallet_path=tmp_path / "wallet.json",
    )

    assert result.status == "ready"
    assert result.rlusd_balance == Decimal("10")


def test_fund_buys_only_the_target_shortfall(tmp_path: Path, monkeypatch) -> None:
    wallet = Wallet.create()
    balances = iter((Decimal("4"), Decimal("4"), Decimal("10")))
    observed: dict[str, Decimal] = {}
    monkeypatch.setattr(
        fund,
        "get_validated_trustline_balance",
        lambda *_args, **_kwargs: next(balances),
    )
    monkeypatch.setattr(fund, "ensure_testnet_xrp", lambda *_args, **_kwargs: 99_000_000)
    monkeypatch.setattr(
        fund, "_ensure_rlusd_trustline_journaled", lambda *_args, **_kwargs: None
    )

    def quote(_client, _address, _issuer, amount):
        observed["quoted"] = amount
        return fund.RLUSDPathQuote(10_000_000, [], "test")

    monkeypatch.setattr(fund, "quote_rlusd_path", quote)
    monkeypatch.setattr(fund, "_assert_spendable_xrp", lambda *_args, **_kwargs: None)

    def submit(_client, _wallet, _transaction, **kwargs):
        observed["submitted"] = kwargs["expected_rlusd_amount"]
        return "A" * 64

    monkeypatch.setattr(fund, "submit_journaled_transaction", submit)
    monkeypatch.setattr(fund, "get_validated_balance", lambda *_args: 88_000_000)

    result = fund.fund_rlusd_wallet(
        client=object(),
        wallet=wallet,
        state_path=tmp_path / "state.json",
        wallet_path=tmp_path / "wallet.json",
        target_rlusd=Decimal("10"),
        max_xrp_drops=35_000_000,
    )

    assert observed == {"quoted": Decimal("6"), "submitted": Decimal("6")}
    assert result.status == "funded"
    assert result.rlusd_balance == Decimal("10")


def test_post_slippage_cap_refuses_before_build_or_sign(tmp_path: Path, monkeypatch) -> None:
    wallet = Wallet.create()
    balances = iter((Decimal("0"), Decimal("0")))
    monkeypatch.setattr(
        fund,
        "get_validated_trustline_balance",
        lambda *_args, **_kwargs: next(balances),
    )
    monkeypatch.setattr(fund, "ensure_testnet_xrp", lambda *_args, **_kwargs: 99_000_000)
    monkeypatch.setattr(
        fund, "_ensure_rlusd_trustline_journaled", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        fund,
        "quote_rlusd_path",
        lambda *_args, **_kwargs: fund.RLUSDPathQuote(34_000_001, [], "test"),
    )
    monkeypatch.setattr(
        fund,
        "build_rlusd_self_payment",
        lambda *_args, **_kwargs: pytest.fail("must refuse before building"),
    )

    with pytest.raises(fund.SpendLimitExceededError, match="exceeds --max-xrp"):
        fund.fund_rlusd_wallet(
            client=object(),
            wallet=wallet,
            state_path=tmp_path / "state.json",
            wallet_path=tmp_path / "wallet.json",
            max_xrp_drops=35_000_000,
            slippage_bps=500,
        )


def test_transaction_fee_cap_refuses_before_signing(tmp_path: Path, monkeypatch) -> None:
    wallet = Wallet.create()
    transaction = fund.build_rlusd_self_payment(
        address=wallet.classic_address,
        issuer=RLUSD_TESTNET_ISSUER,
        amount=Decimal("1"),
        send_max_drops=1_000_000,
    )
    prepared = replace(transaction, fee="101", last_ledger_sequence=120)
    state = fund.RLUSDFundingState(
        classic_address=wallet.classic_address,
        issuer=RLUSD_TESTNET_ISSUER,
    )
    monkeypatch.setattr(fund, "current_validated_ledger_index", lambda _client: 100)
    monkeypatch.setattr(fund, "autofill", lambda _transaction, _client: prepared)
    monkeypatch.setattr(
        fund,
        "sign",
        lambda *_args: pytest.fail("fee cap must be checked before signing"),
    )

    with pytest.raises(fund.SpendLimitExceededError, match="fee 101 drops exceeds cap 100"):
        fund.submit_journaled_transaction(
            client=object(),
            wallet=wallet,
            transaction=transaction,
            purpose="XRP-to-RLUSD conversion",
            max_fee_drops=100,
            state=state,
            state_path=tmp_path / "state.json",
            expected_rlusd_amount=Decimal("1"),
        )

    assert state.pending_transaction is None
    assert not (tmp_path / "state.json").exists()


def test_reserve_check_refuses_when_send_max_would_spend_reserved_xrp(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        fund,
        "get_validated_account_root",
        lambda *_args: {
            "account_data": {"Balance": "7400099", "OwnerCount": 2}
        },
    )

    class Client:
        def request(self, _request):
            return StubResponse(
                {
                    "state": {
                        "network_id": fund.TESTNET_NETWORK_ID,
                        "validated_ledger": {
                            "reserve_base": "1000000",
                            "reserve_inc": "200000",
                        },
                    }
                }
            )

    with pytest.raises(fund.SpendLimitExceededError, match="insufficient spendable XRP"):
        fund._assert_spendable_xrp(
            Client(),
            "rWallet",
            send_max_drops=1_000_000,
            max_fee_drops=100,
        )


def test_simulation_skips_only_explicitly_unsupported_servers(monkeypatch) -> None:
    payment = fund.build_rlusd_self_payment(
        address="rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        issuer=RLUSD_TESTNET_ISSUER,
        amount=Decimal("1"),
        send_max_drops=1_000_000,
    )
    monkeypatch.setattr(
        fund,
        "simulate",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("method not implemented")),
    )

    fund._simulate_exact_payment(
        object(),
        payment,
        issuer=RLUSD_TESTNET_ISSUER,
        expected_rlusd_amount=Decimal("1"),
    )

    monkeypatch.setattr(
        fund,
        "simulate",
        lambda *_args: StubResponse({"engine_result": "tecPATH_DRY"}),
    )
    with pytest.raises(
        fund.LiquidityUnavailableError,
        match=r"found no complete route \(tecPATH_DRY\)",
    ):
        fund._simulate_exact_payment(
            object(),
            payment,
            issuer=RLUSD_TESTNET_ISSUER,
            expected_rlusd_amount=Decimal("1"),
        )


def test_path_quote_is_bound_to_wallet_and_exact_destination() -> None:
    address = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
    result = {
        "validated": True,
        "source_account": address,
        "destination_account": address,
        "destination_amount": {
            "currency": RLUSD_HEX,
            "issuer": RLUSD_TESTNET_ISSUER,
            "value": "10",
        },
        "alternatives": [
            {"source_amount": "27000000", "paths_computed": []},
            {"source_amount": "26000000", "paths_computed": []},
        ],
    }

    class Client:
        def request(self, _request):
            return StubResponse(result)

    quote = fund.quote_rlusd_path(
        Client(), address, RLUSD_TESTNET_ISSUER, Decimal("10")
    )
    assert quote.source_amount_drops == 26_000_000

    result["source_account"] = "rWrong"
    with pytest.raises(fund.FundingError, match="source account mismatch"):
        fund.quote_rlusd_path(
            Client(), address, RLUSD_TESTNET_ISSUER, Decimal("10")
        )


def test_path_quote_falls_back_to_amm_when_pathfinding_has_no_route() -> None:
    address = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
    requests: list[type] = []

    class Client:
        def request(self, request):
            requests.append(type(request))
            if isinstance(request, RipplePathFind):
                return StubResponse(
                    {
                        "validated": True,
                        "source_account": address,
                        "destination_account": address,
                        "destination_amount": {
                            "currency": RLUSD_HEX,
                            "issuer": RLUSD_TESTNET_ISSUER,
                            "value": "10",
                        },
                        "alternatives": [],
                    }
                )
            assert isinstance(request, AMMInfo)
            return StubResponse(
                {
                    "amm": {
                        "amount": "1000000",
                        "amount2": {
                            "currency": RLUSD_HEX,
                            "issuer": RLUSD_TESTNET_ISSUER,
                            "value": "100",
                        },
                        "trading_fee": 0,
                    }
                }
            )

    quote = fund.quote_rlusd_path(
        Client(), address, RLUSD_TESTNET_ISSUER, Decimal("10")
    )

    assert quote == fund.RLUSDPathQuote(111_112, [], "amm")
    assert requests == [RipplePathFind, AMMInfo]


def test_validated_pending_is_recorded_without_rebroadcast(tmp_path: Path, monkeypatch) -> None:
    expected = Decimal("6")
    state = fund.RLUSDFundingState(
        classic_address="rWallet",
        issuer=RLUSD_TESTNET_ISSUER,
        pending_transaction=fund.PendingTransaction(
            purpose="XRP-to-RLUSD conversion",
            tx_hash="A" * 64,
            signed_tx_blob="B" * 64,
            first_ledger_sequence=100,
            last_ledger_sequence=120,
            created_at="2026-09-03T00:00:00+00:00",
            expected_rlusd_amount="6",
        ),
    )
    result = {
        "validated": True,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "delivered_amount": {
                "currency": RLUSD_HEX,
                "issuer": RLUSD_TESTNET_ISSUER,
                "value": str(expected),
            },
        },
    }

    class Client:
        def request(self, _request):
            return StubResponse(result)

    monkeypatch.setattr(
        fund,
        "submit_and_wait",
        lambda *_args, **_kwargs: pytest.fail("validated transaction must not rebroadcast"),
    )
    assert fund.reconcile_pending_transaction(Client(), state, tmp_path / "state.json")
    assert state.pending_transaction is None
    assert state.completed_transaction_hashes == ["A" * 64]


def test_pending_retry_reuses_only_the_identical_signed_blob(tmp_path: Path, monkeypatch) -> None:
    pending = fund.PendingTransaction(
        purpose="RLUSD trustline",
        tx_hash="A" * 64,
        signed_tx_blob="C" * 64,
        first_ledger_sequence=100,
        last_ledger_sequence=120,
        created_at="2026-09-03T00:00:00+00:00",
    )
    state = fund.RLUSDFundingState(
        classic_address="rWallet",
        issuer=RLUSD_TESTNET_ISSUER,
        pending_transaction=pending,
    )

    class Client:
        def request(self, _request):
            return StubResponse({"error": "txnNotFound", "searched_all": False})

    monkeypatch.setattr(fund, "current_validated_ledger_index", lambda _client: 110)
    submitted: list[str] = []

    def submit(blob, _client, **_kwargs):
        submitted.append(blob)
        raise TimeoutError("still pending")

    monkeypatch.setattr(fund, "submit_and_wait", submit)
    with pytest.raises(fund.SettlementPendingError, match="remains pending"):
        fund.reconcile_pending_transaction(Client(), state, tmp_path / "state.json")

    assert submitted == [pending.signed_tx_blob]
    assert state.pending_transaction == pending


def test_pending_retry_clears_a_terminal_rebroadcast_failure(
    tmp_path: Path, monkeypatch
) -> None:
    pending = fund.PendingTransaction(
        purpose="RLUSD trustline",
        tx_hash="A" * 64,
        signed_tx_blob="C" * 64,
        first_ledger_sequence=100,
        last_ledger_sequence=120,
        created_at="2026-09-03T00:00:00+00:00",
    )
    state = fund.RLUSDFundingState(
        classic_address="rWallet",
        issuer=RLUSD_TESTNET_ISSUER,
        pending_transaction=pending,
    )
    state_path = tmp_path / "state.json"

    class Client:
        def request(self, _request):
            return StubResponse({"error": "txnNotFound", "searched_all": False})

    monkeypatch.setattr(fund, "current_validated_ledger_index", lambda _client: 110)
    monkeypatch.setattr(
        fund,
        "submit_and_wait",
        lambda *_args, **_kwargs: StubResponse(
            {
                "validated": True,
                "meta": {"TransactionResult": "tecPATH_DRY"},
            }
        ),
    )

    with pytest.raises(fund.FundingError, match="failed with tecPATH_DRY"):
        fund.reconcile_pending_transaction(Client(), state, state_path)

    assert state.pending_transaction is None
    assert state.completed_transaction_hashes == []
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["pending_transaction"] is None


def test_only_authoritative_not_found_can_expire_pending(tmp_path: Path, monkeypatch) -> None:
    pending = fund.PendingTransaction(
        purpose="RLUSD trustline",
        tx_hash="A" * 64,
        signed_tx_blob="C" * 64,
        first_ledger_sequence=100,
        last_ledger_sequence=120,
        created_at="2026-09-03T00:00:00+00:00",
    )
    state = fund.RLUSDFundingState(
        classic_address="rWallet",
        issuer=RLUSD_TESTNET_ISSUER,
        pending_transaction=pending,
    )

    class Client:
        def request(self, _request):
            return StubResponse({"error": "txnNotFound", "searched_all": True})

    monkeypatch.setattr(fund, "current_validated_ledger_index", lambda _client: 120)
    assert fund.reconcile_pending_transaction(Client(), state, tmp_path / "state.json") is False
    assert state.pending_transaction is None


def test_expired_pending_is_not_rebroadcast_without_authoritative_absence(
    tmp_path: Path, monkeypatch
) -> None:
    pending = fund.PendingTransaction(
        purpose="RLUSD trustline",
        tx_hash="A" * 64,
        signed_tx_blob="C" * 64,
        first_ledger_sequence=100,
        last_ledger_sequence=120,
        created_at="2026-09-03T00:00:00+00:00",
    )
    state = fund.RLUSDFundingState(
        classic_address="rWallet",
        issuer=RLUSD_TESTNET_ISSUER,
        pending_transaction=pending,
    )

    class Client:
        def request(self, _request):
            return StubResponse({"error": "txnNotFound", "searched_all": False})

    monkeypatch.setattr(fund, "current_validated_ledger_index", lambda _client: 120)
    monkeypatch.setattr(
        fund,
        "submit_and_wait",
        lambda *_args, **_kwargs: pytest.fail("expired transaction must not rebroadcast"),
    )

    with pytest.raises(
        fund.SettlementPendingError,
        match="past LastLedgerSequence.*not authoritative",
    ):
        fund.reconcile_pending_transaction(Client(), state, tmp_path / "state.json")

    assert state.pending_transaction == pending


def test_cli_rejects_non_testnet_before_creating_wallet(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(fund, "resolve_live_testnet_rpc_url", lambda _url: "https://mainnet")
    monkeypatch.setattr(fund, "probe_rpc_network_id", lambda _url: 0)
    monkeypatch.setattr(
        fund,
        "create_test_wallet",
        lambda: pytest.fail("wallet must not be created for the wrong network"),
    )

    exit_code = fund.main(
        ["--new-wallet", "--wallet-file", str(tmp_path / "wallet.json")]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Testnet-only" in output


def test_cli_rejects_target_above_trustline_limit_before_network_or_wallet(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        fund,
        "resolve_live_testnet_rpc_url",
        lambda _url: pytest.fail("oversized target must be rejected before network access"),
    )
    monkeypatch.setattr(
        fund,
        "create_test_wallet",
        lambda: pytest.fail("oversized target must be rejected before wallet creation"),
    )

    exit_code = fund.main(
        [
            "--new-wallet",
            "--wallet-file",
            str(tmp_path / "wallet.json"),
            "--target-rlusd",
            "100000.000001",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload == {
        "error": "target RLUSD balance exceeds trust-line limit 100000",
        "status": "refused",
    }
    assert not (tmp_path / "wallet.json").exists()


def _configure_pending_cli(monkeypatch, tmp_path: Path) -> tuple[Wallet, Path]:
    wallet = Wallet.create()
    cache_path = tmp_path / "xrpl-testnet-wallets.json"
    state_path = tmp_path / "rlusd-demo-buyer.funding.json"
    monkeypatch.setattr(
        fund, "resolve_live_testnet_rpc_url", lambda _url: "https://testnet"
    )
    monkeypatch.setattr(
        fund, "probe_rpc_network_id", lambda _url: fund.TESTNET_NETWORK_ID
    )
    monkeypatch.setattr(fund, "JsonRpcClient", lambda _url: object())
    monkeypatch.setattr(fund, "wallet_cache_path", lambda: cache_path)
    monkeypatch.setattr(fund, "private_file_lock", lambda _path: nullcontext())
    monkeypatch.setattr(
        fund,
        "_select_wallet",
        lambda _client, *, new_wallet, wallet_file, issuer: (
            wallet,
            wallet_file or cache_path,
            state_path,
        ),
    )

    def pending(*_args, **_kwargs):
        raise fund.SettlementPendingError("transaction remains pending")

    monkeypatch.setattr(fund, "fund_rlusd_wallet", pending)
    return wallet, cache_path


def test_cli_pending_cached_wallet_retries_without_wallet_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    wallet, cache_path = _configure_pending_cli(monkeypatch, tmp_path)

    assert fund.main(["--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pending"
    assert payload["address"] == wallet.classic_address
    assert "walletFile" not in payload
    assert payload["resumeHint"] == "rerun the same command without --wallet-file"

    assert fund.main([]) == 3
    output = capsys.readouterr().out
    assert "rerun the same command without --wallet-file" in output
    assert str(cache_path) not in output


def test_cli_pending_standalone_wallet_reports_resume_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _configure_pending_cli(monkeypatch, tmp_path)
    wallet_path = tmp_path / "standalone-wallet.json"

    assert fund.main(
        ["--json", "--new-wallet", "--wallet-file", str(wallet_path)]
    ) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["walletFile"] == str(wallet_path)
    assert "resumeHint" not in payload


def test_human_output_never_contains_wallet_seed(tmp_path: Path, capsys) -> None:
    wallet = Wallet.create()
    result = fund.RLUSDFundingResult(
        status="funded",
        classic_address=wallet.classic_address,
        network="testnet",
        issuer=RLUSD_TESTNET_ISSUER,
        xrp_balance_drops=70_000_000,
        rlusd_balance=Decimal("10"),
        target_rlusd=Decimal("10"),
        transaction_hashes=("A" * 64,),
        state_path=tmp_path / "state.json",
        wallet_path=tmp_path / "wallet.json",
    )

    fund._print_result(result, as_json=False)
    assert wallet.seed not in capsys.readouterr().out


def test_cli_json_success_has_stable_shape_and_never_contains_seed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    wallet, cache_path = _configure_pending_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(
        fund,
        "fund_rlusd_wallet",
        lambda *_args, **_kwargs: fund.RLUSDFundingResult(
            status="funded",
            classic_address=wallet.classic_address,
            network="testnet",
            issuer=RLUSD_TESTNET_ISSUER,
            xrp_balance_drops=70_000_000,
            rlusd_balance=Decimal("10"),
            target_rlusd=Decimal("10"),
            transaction_hashes=("A" * 64,),
            state_path=tmp_path / "state.json",
            wallet_path=cache_path,
        ),
    )

    assert fund.main(["--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "address": wallet.classic_address,
        "issuer": RLUSD_TESTNET_ISSUER,
        "network": "testnet",
        "rlusdBalance": "10",
        "stateFile": str(tmp_path / "state.json"),
        "status": "funded",
        "targetRLUSD": "10",
        "transactionHashes": ["A" * 64],
        "walletFile": str(cache_path),
        "xrpBalanceDrops": 70_000_000,
    }
    assert wallet.seed not in output


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (fund.SpendLimitExceededError, "configured cap exceeded"),
        (fund.LiquidityUnavailableError, "no complete route"),
    ],
)
def test_cli_json_refusal_has_stable_shape(
    tmp_path: Path,
    monkeypatch,
    capsys,
    error_type,
    message: str,
) -> None:
    wallet, _cache_path = _configure_pending_cli(monkeypatch, tmp_path)

    def refuse(*_args, **_kwargs):
        raise error_type(message)

    monkeypatch.setattr(fund, "fund_rlusd_wallet", refuse)

    assert fund.main(["--json"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "address": wallet.classic_address,
        "error": message,
        "resumeHint": fund.CACHED_WALLET_RESUME_HINT,
        "status": "refused",
    }


def test_cli_failure_after_new_wallet_selection_reports_recovery_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    wallet, _cache_path = _configure_pending_cli(monkeypatch, tmp_path)
    wallet_path = tmp_path / "standalone-wallet.json"

    def fail(*_args, **_kwargs):
        raise fund.FundingError("post-selection validation failed")

    monkeypatch.setattr(fund, "fund_rlusd_wallet", fail)

    assert fund.main(
        ["--json", "--new-wallet", "--wallet-file", str(wallet_path)]
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "address": wallet.classic_address,
        "error": "post-selection validation failed",
        "status": "failed",
        "walletFile": str(wallet_path),
    }
