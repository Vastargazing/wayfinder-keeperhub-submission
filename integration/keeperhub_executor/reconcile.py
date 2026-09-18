"""Read-only evidence probes. Chain candidates are diagnostics, never operation
identity or permission to resend. Database absence also lacks request quiescence.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ChainMatch:
    tx_hash: str
    block_number: int
    nonce: int
    status: int | None


class ChainReconciler:
    """Find a transaction on chain by (sender, to, calldata, value).

    Deliberately narrow. It answers "is there a transaction from our address
    carrying exactly these bytes in this block range", nothing more. Two
    matches is not a stronger answer than one — it is *ambiguous*, and the
    caller must treat it as such, because a legitimate repeat of an identical
    envelope (two 100 USDC approvals) is indistinguishable from a double send
    at the calldata level alone.
    """

    def __init__(self, rpc_url: str, *, max_blocks: int = 5000) -> None:
        self.rpc_url = rpc_url
        self.max_blocks = max_blocks
        self._client = httpx.AsyncClient(timeout=30, trust_env=False)
        self._block_cache: dict[int, dict[str, Any]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def rpc(self, method: str, params: list[Any]) -> Any:
        resp = await self._client.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]

    async def latest_block(self) -> int:
        return int(await self.rpc("eth_blockNumber", []), 16)

    async def fork_block(self) -> int | None:
        """The block the local fork branched at; local blocks start after it."""
        try:
            info = await self.rpc("anvil_nodeInfo", [])
            return int(info["forkConfig"]["forkBlockNumber"])
        except Exception:  # noqa: BLE001
            return None

    async def block_timestamp(self, number: int) -> int | None:
        block = self._block_cache.get(number)
        if block is None:
            block = await self.rpc("eth_getBlockByNumber", [hex(number), False])
        if block is None:
            return None
        return int(block["timestamp"], 16)

    async def block_at_or_after(self, epoch: float, lo: int, hi: int) -> int:
        """First block at or after `epoch`, by binary search. Clamped to [lo, hi]."""
        left, right = lo, hi
        answer = hi
        while left <= right:
            mid = (left + right) // 2
            ts = await self.block_timestamp(mid)
            if ts is None:
                break
            if ts >= epoch:
                answer = mid
                right = mid - 1
            else:
                left = mid + 1
        return max(lo, min(answer, hi))

    async def find(
        self,
        *,
        sender: str,
        to: str | None,
        data: str,
        value: int,
        from_block: int | None = None,
        to_block: int | None = None,
    ) -> tuple[list[ChainMatch], dict[str, Any]]:
        """Return every matching transaction plus a description of the scan.

        The scan window is reported so a caller can tell "searched and found
        nothing" from "could not search the relevant range" — only the first
        is evidence.
        """
        latest = await self.latest_block() if to_block is None else to_block
        fork = await self.fork_block()
        lo = from_block
        if lo is None:
            lo = (fork + 1) if fork is not None else max(0, latest - self.max_blocks)
        lo = max(lo, latest - self.max_blocks)
        sender_l = sender.lower()
        to_l = to.lower() if to else None
        data_l = data.lower()

        matches: list[ChainMatch] = []
        scanned = 0
        for number in range(lo, latest + 1):
            block = self._block_cache.get(number)
            if block is None:
                block = await self.rpc("eth_getBlockByNumber", [hex(number), True])
                if block is None:
                    continue
                # Only cache blocks that can no longer change.
                if number < latest:
                    self._block_cache[number] = block
            scanned += 1
            for tx in block.get("transactions", []):
                if tx.get("from", "").lower() != sender_l:
                    continue
                tx_to = (tx.get("to") or "").lower() or None
                if tx_to != to_l:
                    continue
                if (tx.get("input") or "0x").lower() != data_l:
                    continue
                if int(tx.get("value", "0x0"), 16) != int(value):
                    continue
                matches.append(
                    ChainMatch(
                        tx_hash=tx["hash"],
                        block_number=int(tx["blockNumber"], 16),
                        nonce=int(tx["nonce"], 16),
                        status=None,
                    )
                )
        scan = {
            "from_block": lo,
            "to_block": latest,
            "blocks_scanned": scanned,
            "fork_block": fork,
            "complete": from_block is None or lo <= (from_block or lo),
        }
        return matches, scan

    async def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        return await self.rpc("eth_getTransactionReceipt", [tx_hash])


class KeeperHubDbProbe:
    """Reads the two KeeperHub tables that have no HTTP surface, via docker exec.

    * ``idempotency_records`` — the earliest trace that a submission request
      arrived (inserted before the execution row and before dispatch). Needed
      for diagnostics. Its absence cannot close a delayed-POST window or
      establish quiescence; it does not permit ``never_seen``.
    * ``pending_transactions`` — ``(wallet_address, chain_id, nonce) -> tx_hash,
      execution_id``, written by ``NonceManager.recordTransaction`` at
      ``lib/web3/chain-adapter/evm.ts:468`` **immediately before the receipt
      wait**. It therefore survives the receipt failure that empties
      ``workflow_executions.transaction_hashes``. Caveat: the primary key is
      ``(wallet, chain, nonce)`` with ``ON CONFLICT DO UPDATE``, so a later
      transaction reusing a nonce overwrites the row (OBSERVED on this stand:
      the attempt-1 hash from research/stand/REPORT.md is gone, replaced by the
      attempt-2 hash at the same nonce). Validate a hit's provenance and treat a miss
      as "no information", never as "nothing was sent".

    Every method returns ``None`` when the probe itself cannot run — which the
    caller must treat as "unknown", never as "no record". That distinction is
    the whole point of the three-valued contract.
    """

    def __init__(
        self,
        *,
        container: str = "upstream-db-1",
        database: str = "keeperhub",
        user: str = "postgres",
    ) -> None:
        self.container = container
        self.database = database
        self.user = user

    def _psql(self, sql: str) -> str | None:
        try:
            proc = subprocess.run(
                [
                    "docker", "exec", "-i", self.container,
                    "psql", "-U", self.user, "-d", self.database, "-tA", "-c", sql,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:  # noqa: BLE001
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    async def _rows(self, sql: str) -> list[dict[str, Any]] | None:
        out = await asyncio.get_running_loop().run_in_executor(None, self._psql, sql)
        if out is None:
            return None
        try:
            return json.loads(out)
        except Exception:  # noqa: BLE001
            return None

    async def record(self, *, scope: str, key: str) -> dict[str, Any] | None:
        """One row as a dict, ``{}`` for "queried, no such row", None for "could not query"."""
        if not _safe_literal(scope) or not _safe_literal(key):
            return None
        rows = await self._rows(
            "select coalesce(json_agg(row_to_json(t)), '[]'::json)::text from ("
            "select id, organization_id, scope, idempotency_key, status, "
            "response_status, resource_id, lock_version, created_at, expires_at "
            f"from idempotency_records where scope = '{scope}' "
            f"and idempotency_key = '{key}') t"
        )
        if rows is None:
            return None
        return rows[0] if rows else {}

    async def pending_transactions(
        self, *, execution_id: str
    ) -> list[dict[str, Any]] | None:
        """Broadcast hashes KeeperHub recorded for this run before the receipt wait."""
        if not _safe_literal(execution_id):
            return None
        return await self._rows(
            "select coalesce(json_agg(row_to_json(t)), '[]'::json)::text from ("
            "select wallet_address, chain_id, nonce, tx_hash, execution_id, status, "
            "submitted_at, confirmed_at from pending_transactions "
            f"where execution_id = '{execution_id}') t"
        )


def _safe_literal(value: str) -> bool:
    """Refuse anything that is not a plain identifier-ish token.

    These queries are string-interpolated because the transport is
    ``docker exec psql -c``; nothing here ever sees attacker input (operation
    ids are uuid4 hex, execution ids are KeeperHub-generated), but the guard
    keeps it that way.
    """
    return bool(value) and all(c.isalnum() or c in "-_:.@+" for c in value)


#: Kept for callers that imported the older, narrower name.
IdempotencyProbe = KeeperHubDbProbe
