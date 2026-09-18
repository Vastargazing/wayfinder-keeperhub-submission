"""Historical exploratory cap search, not a boundary proof.

Uses latest and treats every RPC error as rejection. DO NOT use its estimate as
an established contract cap. Pinned raw success/revert boundary responses and
provider-error classification are required for a verified cap.
"""
import httpx
from eth_utils import function_signature_to_4byte_selector as s4
RPC="https://sepolia.base.org"; FAUCET="0xD9145b5F45Ad4519c7ACcD6E0A4A82e83bB8A6Dc"
USDC="0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f"; W="0x7BEa5c59c12aebF9343087EF1513f651678291F5"
def call(data):
    with httpx.Client(timeout=30,trust_env=False) as c:
        return c.post(RPC,json={"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":FAUCET,"from":W,"data":data},"latest"]}).json()
sel="0x"+s4("mint(address,address,uint256)").hex()
a=lambda x: x.lower().replace("0x","").rjust(64,"0")
def ok(amt): return "result" in call(sel+a(USDC)+a(W)+f"{amt:064x}")
lo,hi=10_001*10**6, 2**128
while lo+1<hi:
    mid=(lo+hi)//2
    if ok(mid): lo=mid
    else: hi=mid
print(f"UNVERIFIED estimated max mint amount (raw, 6dp) = {lo}  = {lo/10**6:,.6f} USDC")
print(f"first rejected            = {hi}")
