"""Common durable SDK authorization checks for every KeeperHub profile."""
from wayfinder_paths.core.utils.executor import build_envelope, envelope_digest


class LocalAuthorization:
    def __init__(self, journal, wallet, chain_id):
        self.journal = journal
        self.wallet = wallet
        self.chain_id = chain_id

    def authorized(self, operation_id, txn_hash=None):
        rows = self.journal.entries()
        row = next((r for r in rows if r['operation_id'] == operation_id), None)
        if row is None:
            raise ValueError('operation absent from SDK journal')
        env = row['envelope']
        if (env != build_envelope(env) or row['digest'] != envelope_digest(env)
            or row['sender'] != env['from'].lower() or row['chain_id'] != env['chainId']
            or env['from'].lower() != self.wallet.lower() or env['chainId'] != self.chain_id
            or row['state'] not in {'pending', 'submitted'}):
            raise ValueError('SDK authorization chain/sender/state/digest is inconsistent')
        if txn_hash:
            if row['txn_hash'] and row['txn_hash'].lower() != txn_hash.lower():
                raise ValueError('candidate conflicts with SDK journal hash')
            if any(r['operation_id'] != operation_id and r['txn_hash']
                   and r['txn_hash'].lower() == txn_hash.lower() for r in rows):
                raise ValueError('candidate hash used by another operation')
        return row

    def before_submit(self, operation_id, envelope):
        row = self.authorized(operation_id)
        if row['state'] != 'pending' or row['txn_hash'] or row['envelope'] != envelope:
            raise ValueError('submit differs from durable pending authorization')

    def verify_record(self, operation_id, txn_hash, record):
        # Import here to keep the encoder in one place without a module cycle.
        from .executor import encode_from_recorded_input, ether_string_to_wei
        env = self.authorized(operation_id, txn_hash)['envelope']
        if (record.get('operationId') != operation_id
            or str(record.get('network')) != str(env['chainId'])
            or str(record.get('contractAddress', '')).lower() != env['to'].lower()
            or (encode_from_recorded_input(record) or '').lower() != env['data'].lower()
            or 'ethValue' not in record
            or ether_string_to_wei(record['ethValue']) != env['value']):
            raise ValueError('KeeperHub recorded input differs from SDK authorization')
        return env


class ReadOnlyJournal:
    """Diagnostic view of an existing SDK journal; never creates or migrates it.

    An operator inspection binds this to a real executor, so it has to answer the
    journal lifecycle that executor uses: ``entries()`` for the durable
    authorization, and ``invalidate_validation()`` at every attempt boundary.
    It answers those and nothing else, and it is deliberately not an
    ``OperationJournal`` subclass — inheriting a writer to obtain one hook would
    carry ``begin``, ``record_hash`` and the rest of the transitions into a
    read-only path.

    The connection is opened ``mode=ro``: SQLite itself refuses a write on it, an
    absent database is reported rather than created, and a journal whose schema
    does not match is reported rather than migrated.
    """
    def __init__(self, path):
        import sqlite3
        from pathlib import Path
        self._conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
        self._conn.row_factory = sqlite3.Row

    def entries(self):
        import json
        result = []
        for row in self._conn.execute('SELECT * FROM operations ORDER BY created_at'):
            item = dict(row)
            item['envelope'] = json.loads(item['envelope'])
            result.append(item)
        return result

    def invalidate_validation(self):
        """Drop the permission this view holds to accept a result: it holds none.

        The executor calls this whenever an attempt starts or fails. A journal
        that grants a single-use authority after its own verification forgets it
        here — authority, never evidence. This view grants none: it cannot record
        a hash, mark anything consumed or move an operation, so there is nothing
        to forget and doing nothing is the whole correct answer.

        It is explicitly empty rather than absent because absent is not the same
        thing. Without it every diagnostic lookup raised ``AttributeError``
        before reading any history, and the hook in the executor's own error
        handlers replaced the failure being reported with that ``AttributeError``
        instead of letting it reach the operator.
        """

    def close(self):
        self._conn.close()
