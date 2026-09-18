"""C3 deterministic snapshot boundaries and logical fingerprint controls."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from wayfinder_paths.core.utils.executor import OperationJournal, build_envelope

import runbuilder
from operator_console import sources
from operator_console.actions import CHECK_STATE
from operator_console.model import build_view
from operator_console.render import primary_action, render_page
from test_console_lifecycle import rewrite


def envelope(n):
    return build_envelope({'chainId':8453, 'from':runbuilder.WALLET,
                           'to':runbuilder.TOKEN,'data':f'0x{n:02x}', 'value':0})


def insert(writer, name, n):
    row,_=writer.bind_step(name+'/send/0',envelope(n))
    writer.record_hash(row['operation_id'],'0x'+f'{n:064x}', consumed=True)
    return row


class ConnectionProxy:
    def __init__(self, conn, before):
        object.__setattr__(self,'conn',conn)
        object.__setattr__(self,'before',before)
        object.__setattr__(self,'closed',False)
    def __setattr__(self,key,value):
        setattr(self.conn,key,value)
    def execute(self,sql,*args):
        self.before(self.conn,sql)
        return self.conn.execute(sql,*args)
    def close(self):
        self.conn.close()
        object.__setattr__(self,'closed',True)


def test_writer_commits_between_selects_but_snapshot_stays_consistent(tmp_path, monkeypatch):
    path=tmp_path/'sdk.sqlite'
    writer=OperationJournal(path)
    try:
        assert writer._conn.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
        writer._conn.execute('PRAGMA wal_autocheckpoint=0')
        insert(writer,'one',1)
        real_connect=sqlite3.connect
        readers=[]; transactions=[]; fired=[]
        def barrier(conn,sql):
            if sql.startswith('SELECT'):
                transactions.append(conn.in_transaction)
            if sql=='SELECT step_id, operation_id FROM execution_steps':
                fired.append(insert(writer,'two',2)['operation_id'])
        def connect(*a,**kw):
            c=ConnectionProxy(real_connect(*a,**kw),barrier);readers.append(c);return c
        monkeypatch.setattr(sources.sqlite3,'connect',connect)
        snapshot=sources.read_journal(path)
        assert fired, 'barrier did not run'
        assert all(snapshot.operation(op) for op in snapshot.step_bindings.values()), 'mixed snapshot binding'
        assert len(snapshot.operations)==len(snapshot.step_bindings)==1
        assert all(transactions), 'all SELECTs must share an explicit transaction'
        assert all(r.closed for r in readers)
        assert len(writer.entries())==2, 'the writer really committed'
    finally:
        writer.close()


def test_wal_changes_logical_hash_without_changing_main_file(tmp_path):
    path=tmp_path/'sdk.sqlite';writer=OperationJournal(path)
    try:
        writer._conn.execute('PRAGMA wal_autocheckpoint=0')
        insert(writer,'one',1)
        first=sources.read_journal(path); main=path.read_bytes()
        insert(writer,'two',2)
        second=sources.read_journal(path)
        assert path.read_bytes()==main, 'control requires unchanged main database file'
        assert first.operations!=second.operations
        assert first.sha256!=second.sha256, 'WAL logical change must change snapshot digest'
        assert second.sha256==sources.read_journal(path).sha256
    finally:
        writer.close()
    moved=tmp_path/'moved.sqlite';shutil.copyfile(path,moved)
    assert sources.read_journal(path).sha256==sources.read_journal(moved).sha256


@pytest.mark.parametrize('field', ['run_id','binding','hash','envelope','consumed'])
def test_projection_hash_covers_returned_semantic_fields(tmp_path,field):
    root=runbuilder.write_run(tmp_path/'run');path=root/'sdk-journal.sqlite'
    before=sources.read_journal(path)
    writer=OperationJournal(path)
    try:
        if field=='run_id': writer._conn.execute("UPDATE moonwell_run SET run_id=?",('b'*32,))
        if field=='binding': writer._conn.execute("UPDATE execution_steps SET step_id=step_id||'/changed'")
        if field=='hash': writer._conn.execute("UPDATE operations SET txn_hash=?",('0x'+'f'*64,))
        if field=='envelope':
            e=dict(before.operations[0]['envelope']);e['value']='42'
            writer._conn.execute('UPDATE operations SET envelope=? WHERE operation_id=?',(json.dumps(e),before.operations[0]['operation_id']))
        if field=='consumed': writer._conn.execute('UPDATE operations SET consumed=1-consumed')
        after=sources.read_journal(path)
        assert before.sha256!=after.sha256, field
    finally: writer.close()


@pytest.mark.parametrize('kind',['checkpoint','manifest'])
def test_json_parse_and_external_hash_use_one_byte_read(tmp_path,monkeypatch,kind):
    root=runbuilder.write_run(tmp_path/'run')
    journal=sources.read_journal(root/'sdk-journal.sqlite')
    checkpoint=sources.read_checkpoint(root/'run-plan.json')
    path=root/('run-plan.json' if kind=='checkpoint' else 'run-manifest.json')
    original=path.read_bytes(); calls=[]
    real_read=Path.read_bytes
    def replacing_read(p):
        raw=real_read(p)
        if p==path:
            calls.append(1)
            p.write_bytes(b'{}')  # deterministic writer replace after first bytes
        return raw
    monkeypatch.setattr(Path,'read_bytes',replacing_read)
    if kind=='checkpoint':
        result=sources.read_checkpoint(path)
        assert result.data==json.loads(original)['data'], 'parse used a second file version'
        assert result.integrity=='verified'
    else:
        result=sources.read_manifest(path,root,journal=journal,checkpoint=checkpoint)
        assert result.data==json.loads(original), 'parse used a second file version'
        assert result.bound
    assert result.sha256==hashlib.sha256(original).hexdigest()
    assert calls==[1]


def test_writer_transition_between_sqlite_and_json_refuses_combined_completion(tmp_path,monkeypatch):
    root=runbuilder.write_run(tmp_path/'run', steps=[(runbuilder.BORROW,runbuilder.POOL,'0x00','started','pending')])
    original=sources.read_journal
    def read_then_writer_commits(path):
        old=original(path)
        writer=OperationJournal(path)
        try: writer.record_hash(old.operations[0]['operation_id'],'0x'+'1'*64,consumed=True)
        finally: writer.close()
        def completed(d):
            d['calls'][runbuilder.BORROW].update(state='done',result=[True,'0x'+'1'*64])
            d['iterations'][-1].update(status='done',lend_amt_wei=1)
        rewrite(root/'run-plan.json',completed)
        return old
    monkeypatch.setattr(sources,'read_journal',read_then_writer_commits)
    state=sources.load_state_directory(root,root/'run-manifest.json')
    view=build_view(state)
    assert view.stop.blocked and view.stop.status=='uncertain'
    assert not view.steps and not state.manifest.bound
    assert 'lacks its submitted journal operation' in ' '.join(view.stop.paragraphs)
    assert primary_action(view) is CHECK_STATE
    assert 'Not atomic' in render_page(view)


@pytest.mark.parametrize('failure',['select','envelope','database'])
def test_failed_reads_close_connection_and_do_not_write_sources(tmp_path,monkeypatch,failure):
    root=runbuilder.write_run(tmp_path/'run');path=root/'sdk-journal.sqlite'
    if failure=='envelope':
        writer=OperationJournal(path)
        writer._conn.execute("UPDATE operations SET envelope='broken'");writer.close()
    if failure=='database': path.write_bytes(b'not sqlite')
    before={p:p.read_bytes() for p in root.iterdir() if p.is_file()}
    real=sqlite3.connect; readers=[]
    def barrier(conn,sql):
        if failure=='select' and sql.startswith('SELECT step_id'):
            raise sqlite3.OperationalError('injected read error')
    def connect(*a,**kw):
        c=ConnectionProxy(real(*a,**kw),barrier);readers.append(c);return c
    monkeypatch.setattr(sources.sqlite3,'connect',connect)
    state=sources.load_state_directory(root)
    assert state.journal is None and state.problems
    assert all(r.closed for r in readers)
    view=build_view(state)
    assert view.stop.blocked and view.stop.status=='uncertain'
    assert primary_action(view) is CHECK_STATE
    render_page(view)
    for p,raw in before.items(): assert p.read_bytes()==raw


def test_same_run_checkpoint_result_hash_contradiction_is_visible(tmp_path):
    root=runbuilder.write_run(tmp_path/'run')
    rewrite(root/'run-plan.json', lambda d: d['calls'][runbuilder.BORROW].update(result=[True,'0x'+'f'*64]))
    state=sources.load_state_directory(root,root/'run-manifest.json')
    view=build_view(state)
    assert view.stop.blocked and view.stop.status=='uncertain'
    assert not view.steps and not state.manifest.bound
    assert 'result contradicts its journal hash' in render_page(view)
