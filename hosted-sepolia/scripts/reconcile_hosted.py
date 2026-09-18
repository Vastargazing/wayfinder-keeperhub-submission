#!/usr/bin/env python3
"""GET/RPC-only recovery command for an existing journal; never submits.

Verification evidence is written locally. --apply additionally invokes the real
SDK pending->submitted transition after a fresh lookup. This command does not
fund, reset, create a workflow, broadcast, or claim strategy completion.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from hosted_sepolia.recovery import CHAIN_ID, build_execution
from keeperhub_executor.abis import default_resolver
from keeperhub_executor.client import KeeperHubClient


async def reconcile(execution, *, apply=False):
    pending = execution.journal.pending(execution.wallet_address, CHAIN_ID)
    observations = []
    for row in pending:
        answer = await execution.executor.lookup(row["operation_id"])
        observations.append(
            {
                "operationId": row["operation_id"],
                "outcome": answer.outcome.value,
                "txnHash": answer.txn_hash,
                "detail": answer.detail,
            }
        )
    if apply:
        # Re-runs ownership + effect verification before the journal transition.
        await execution.resolve_pending(CHAIN_ID)
    return {
        "observations": observations,
        "applied": apply,
        "goalVerified": False,
        "scope": "local evidence and optional journal reconciliation; no submit",
    }


async def main(args):
    if not args.journal.is_file():
        raise ValueError("an existing SDK journal is required")
    key = os.environ["KEEPERHUB_API_KEY"]
    client = KeeperHubClient(
        os.environ.get("KEEPERHUB_BASE_URL", "https://app.keeperhub.com"), key
    )
    execution = build_execution(
        journal_path=args.journal,
        client=client,
        workflow_id=args.workflow,
        wallet_address=args.wallet,
        rpc_url=args.rpc_url,
        abi_resolver=default_resolver,
    )  # allow_submit remains False
    try:
        result = await reconcile(execution, apply=args.apply)
        print(json.dumps(result, indent=2).replace(key, "<redacted>"))
        return 0 if all(r["outcome"] == "landed" for r in result["observations"]) else 2
    finally:
        execution.journal.close()
        await execution.executor.close()
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
