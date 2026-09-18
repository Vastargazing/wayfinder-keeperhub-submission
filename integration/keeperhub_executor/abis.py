"""ABI resolution for the KeeperHub executor.

KeeperHub's write-contract node needs an ABI. The Wayfinder envelope carries
only ``to`` and ``data``, so something has to supply one. Three sources, in the
order the executor tries them:

1. an explicit per-address registration (``register_abi``);
2. the bundled minimal ABIs below, matched **by selector** across every bundled
   entry — enough for the ERC-20 and Aave V3 Pool calls the SDK's
   ``AaveV3Adapter`` emits, which is the path this integration proves;
3. a caller-supplied resolver (e.g. one that calls KeeperHub's own
   ``/api/web3/fetch-abi``, which is the explorer cascade + EIP-2535 Diamond
   merge described in BRAP-CALLDATA.md §4.2).

Selector matching is safe here because :func:`~.calldata.decode_and_verify`
re-encodes and compares bytes afterwards: a wrong ABI that happens to share a
selector cannot slip through, it just fails the byte check and the submission is
refused. The ABI is a *hypothesis*; byte equality is the test.
"""

from __future__ import annotations

import json
from typing import Any, Callable

AbiResolver = Callable[[int, str, str], "list[dict[str, Any]] | None"]

ERC20_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "transfer",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "transferFrom",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "allowance",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

AAVE_V3_POOL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "supply",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "withdraw",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "borrow",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "referralCode", "type": "uint16"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "repay",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "setUserUseReserveAsCollateral",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "useAsCollateral", "type": "bool"},
        ],
        "outputs": [],
    },
]

WETH9_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "deposit",
        "stateMutability": "payable",
        "inputs": [],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "withdraw",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": [],
    },
]

BUNDLED: list[list[dict[str, Any]]] = [ERC20_ABI, AAVE_V3_POOL_ABI, WETH9_ABI]

_REGISTRY: dict[tuple[int, str], list[dict[str, Any]]] = {}


def register_abi(chain_id: int, address: str, abi: list[dict[str, Any]] | str) -> None:
    """Pin an ABI for one contract. Highest priority."""
    parsed = json.loads(abi) if isinstance(abi, str) else abi
    _REGISTRY[(int(chain_id), address.lower())] = parsed


def default_resolver(
    chain_id: int, address: str, selector: str
) -> list[dict[str, Any]] | None:
    """Registry first, then any bundled ABI that declares this selector."""
    from .calldata import find_function_by_selector  # local import: avoid a cycle

    pinned = _REGISTRY.get((int(chain_id), address.lower()))
    if pinned is not None:
        return pinned
    for abi in BUNDLED:
        if find_function_by_selector(abi, selector) is not None:
            return abi
    return None
