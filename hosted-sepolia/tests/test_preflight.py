"""Offline preflight controls, never contact RPC."""

import importlib.util
from pathlib import Path


def module():
    path = Path(__file__).parents[1] / "scripts/preflight.py"
    spec = importlib.util.spec_from_file_location("preflight_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wrong_chain_stops_before_simulations(monkeypatch, capsys):
    m = module()
    calls = []

    def rpc(method, params):
        calls.append(method)
        return {"result": "0x1"}

    monkeypatch.setattr(m, "rpc", rpc)
    assert m.main() != 0
    assert calls == ["eth_chainId"]


def test_failed_dry_run_is_not_ready_exit_zero(monkeypatch, capsys):
    m = module()

    def rpc(method, params):
        if method == "eth_chainId":
            return {"result": hex(m.CHAIN_ID)}
        if method == "eth_blockNumber":
            return {"result": "0x100"}
        return {
            "error": {"code": 3, "message": "execution reverted", "data": "0x47bc4b2c"}
        }

    monkeypatch.setattr(m, "rpc", rpc)
    assert m.main() != 0


def hosted_module(monkeypatch):
    monkeypatch.setenv("KEEPERHUB_API_KEY", "offline-fixture")
    monkeypatch.setenv("KEEPERHUB_WALLET_ADDRESS", "0x" + "11" * 20)
    monkeypatch.setenv("KEEPERHUB_CHAIN_ID", "84532")
    path = Path(__file__).parents[1] / "scripts/preflight_keeperhub.py"
    spec = importlib.util.spec_from_file_location("hosted_preflight_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hosted_wrong_chain_stops_without_dry_run(monkeypatch, capsys):
    m = hosted_module(monkeypatch)
    m.CHAIN = "8453"

    def forbidden(*args, **kwargs):
        raise AssertionError("dry run on wrong chain")

    monkeypatch.setattr(m, "dry_run", forbidden)
    assert m.main() == 3


def test_hosted_independent_dry_runs_never_claim_sequence_readiness(
    monkeypatch, capsys
):
    import json

    m = hosted_module(monkeypatch)
    monkeypatch.setattr(
        m,
        "dry_run",
        lambda *a, **kw: {"http_status": 200, "response": {"wouldRevert": False}},
    )
    assert m.main() == 2
    out = json.loads(capsys.readouterr().out)
    assert (
        out["all_independent_simulations_ok"] is True and out["sequence_ready"] is False
    )
