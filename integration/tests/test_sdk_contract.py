"""Behavioral compatibility of the shipped SDK journal with KeeperHub lookup."""
from keeperhub_executor.executor import KeeperHubExecutor
from wayfinder_paths.core.utils.executor import OperationJournal


async def test_lookup_empty_history_preserves_pending_without_send(tmp_path):
    class Client:
        reads = 0

        async def executions(self, workflow_id):
            self.reads += 1
            return []

        async def close(self):
            pass

    client = Client()
    executor = KeeperHubExecutor(
        client=client, workflow_id="synthetic-contract",
        wallet_address="0x" + "1" * 40, chain_id=84532,
        rpc_url="http://127.0.0.1:9", execution_profile="direct",
    )
    journal = OperationJournal(tmp_path / "contract.sqlite")
    executor.bind_journal(journal)
    operation_id = journal.begin({
        "chainId": 84532, "from": executor.wallet_address,
        "to": "0x" + "2" * 40, "data": "0x", "value": 0,
    })
    before = journal.entries()
    try:
        for _ in range(2):
            result = await executor.lookup(operation_id)
            assert result.outcome.value == "indeterminate"
            assert "do not resend" in result.detail.lower()
            assert journal.entries() == before
        assert client.reads == 2
        assert executor.submits == 0
        assert before[0]["state"] == "pending"
        assert not before[0]["consumed"]
    finally:
        await executor.close()
        journal.close()
