"""Host policy at the constructor and actual CLI/parser, without TCP sockets."""
from pathlib import Path
import socket
import pytest
from operator_console import server
from operator_console.__main__ import main

GOOD = [('127.0.0.1','127.0.0.1'),('127.0.0.0','127.0.0.0'),('127.255.255.255','127.255.255.255'),('localhost','127.0.0.1')]
BAD = ['', '0.0.0.0', '192.168.1.1', '8.8.8.8', 'example.com', 'localhost.', 'LOCALHOST', '127.1', '2130706433', '0x7f000001', '0177.0.0.1', '127.00.0.1', '127.0.0.1 ', ' 127.0.0.1', '::1', '::', '::ffff:127.0.0.1']

@pytest.fixture
def constructors(monkeypatch):
    calls=[]
    class Spy:
        def __init__(self,address,handler):self.server_address=address;calls.append(address)
        def serve_forever(self):pass
        def server_close(self):pass
    monkeypatch.setattr(server,'ThreadingHTTPServer',Spy)
    def no_dns(*a,**k):raise AssertionError('host validation must not use DNS')
    monkeypatch.setattr(socket,'getaddrinfo',no_dns)
    monkeypatch.setattr(socket,'gethostbyname',no_dns)
    return calls

@pytest.mark.parametrize('host,normalized',GOOD)
def test_direct_and_cli_accept_only_explicit_loopback(host,normalized,constructors,tmp_path):
    server.make_server(server.ConsoleConfig(tmp_path),host,8765)
    assert constructors==[(normalized,8765)]
    assert main(['--state-dir',str(tmp_path),'--host',host,'--port','8765'])==0
    assert constructors==[(normalized,8765)]*2

@pytest.mark.parametrize('host',BAD)
def test_refusal_precedes_constructor_in_direct_and_cli(host,constructors,tmp_path,capsys):
    with pytest.raises(ValueError,match='loopback'):
        server.make_server(server.ConsoleConfig(tmp_path),host,8765)
    assert constructors==[]
    assert main(['--state-dir',str(tmp_path),'--host',host])==2
    err=capsys.readouterr().err
    assert 'loopback' in err and 'Traceback' not in err
    assert constructors==[]

def test_default_and_help(constructors,tmp_path,capsys):
    assert main(['--state-dir',str(tmp_path)])==0
    assert constructors==[('127.0.0.1',8765)]
    with pytest.raises(SystemExit) as outcome:main(['--help'])
    assert outcome.value.code==0
    assert '127.0.0.0/8' in capsys.readouterr().out
