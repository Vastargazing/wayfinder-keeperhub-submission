"""One place for the mode labels this demo must never blur.

Repeated verbatim in evidence files so a reader of a raw JSON never has to
guess which guarantees are in play.
"""

MODE = {
    "chain": "FORK",  # anvil fork of Base mainnet, chain id 8453, :8545
    "signer": "DEV/TEST SIGNER",  # KeeperHub stand's ethers.Wallet patch, NOT Turnkey
    "keeperhub": "SELF-HOSTED (local docker build of the public repo)",
    "sdk_key_material": "NONE (no private key, no Wayfinder API key in the SDK process)",
    "strategy_code": "REAL (wayfinder_paths.strategies.moonwell_wsteth_loop_strategy)",
    "adapters": "REAL (MoonwellAdapter, BRAPAdapter, BalanceAdapter, TokenAdapter)",
    "seam": "REAL (branch seam/external-executor, ExternalExecution + OperationJournal)",
    "executor": "REAL (research/integration/keeperhub_executor, imported unmodified)",
    "swap_quote": "STAND-IN (LI.FI keyless quote; BRAP requires a Wayfinder API key we do not have)",
    "token_metadata_and_prices": "STUB (LI.FI /v1/token; TOKEN_CLIENT requires a Wayfinder API key)",
    "steth_apr": "STUB (constant; the real call is a public Lido endpoint, see README)",
    "funding": "HARNESS CHEATCODE (anvil_setStorageAt on the USDC balance slot)",
}

NOT_PROVEN = [
    "Turnkey custody, policies, quorum or gas sponsorship",
    "hosted KeeperHub",
    "mainnet or testnet execution",
    "a real BRAP quote (no Wayfinder API key available)",
    "a real TOKEN_CLIENT response shape beyond the fields this path reads",
]
