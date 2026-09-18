"""Actual status argparse/producer; synthetic Chain only, no RPC invocation."""
import asyncio,runpy,sys
from unittest.mock import patch
class ModelChain:
    def __init__(self,*a,**kw):pass
    async def fork_block(self):return 123
    async def block_number(self):return 123
    async def rpc(self,method,params):
        assert method=='eth_chainId';return '0x2105'
    async def position(self,*a,**kw):return {'synthetic':True,'block':'0x7b'}
    async def onchain_calldata_matches(self,*a,**kw):return {'synthetic':True,'match':True}
with patch('asyncio.run',lambda c:c.close()):
    ns=runpy.run_path('moonwell/scripts/run_loop.py')
ns['main'].__globals__['Chain']=ModelChain
sys.argv=['moonwell/scripts/run_loop.py',*sys.argv[1:]]
asyncio.run(ns['main']())
