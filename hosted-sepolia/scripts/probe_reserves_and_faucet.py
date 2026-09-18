import json, httpx
from eth_utils import function_signature_to_4byte_selector as s4, to_checksum_address
RPC="https://sepolia.base.org"
POOL="0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
USDC="0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f"
CIRCLE="0x036CbD53842c5426634e7929541eC2318f3dCF7e"
FAUCET="0xD9145b5F45Ad4519c7ACcD6E0A4A82e83bB8A6Dc"
W="0x7BEa5c59c12aebF9343087EF1513f651678291F5"
n=[0]
def rpc(m,p):
    n[0]+=1
    with httpx.Client(timeout=30,trust_env=False) as c:
        b=c.post(RPC,json={"jsonrpc":"2.0","id":n[0],"method":m,"params":p}).json()
    return b
def call(to,data,frm=None):
    tx={"to":to,"data":data}
    if frm: tx["from"]=frm
    return rpc("eth_call",[tx,"latest"])
def sel(sig): return "0x"+s4(sig).hex()
def a(x): return x.lower().replace("0x","").rjust(64,"0")
def u(x): return f"{x:064x}"

print("=== getReservesList ===")
r=call(POOL, sel("getReservesList()"))
if "result" in r:
    raw=bytes.fromhex(r["result"][2:]); cnt=int.from_bytes(raw[32:64],"big")
    lst=[to_checksum_address("0x"+raw[64+i*32+12:64+i*32+32].hex()) for i in range(cnt)]
    print(f"count={cnt}")
    for x in lst: print("  ",x, "<-- AAVE TEST USDC" if x.lower()==USDC.lower() else ("<-- CIRCLE USDC" if x.lower()==CIRCLE.lower() else ""))
else: print(r)

print("\n=== getReserveData word-by-word for CIRCLE USDC ===")
r=call(POOL, sel("getReserveData(address)")+a(CIRCLE))
raw=bytes.fromhex(r["result"][2:])
for i in range(0,len(raw),32):
    w=raw[i:i+32].hex()
    if int(w,16)!=0: print(f"  word{i//32}: {w}")

print("\n=== getReserveAToken / getReserveVariableDebtToken ===")
for sig in ["getReserveAToken(address)","getReserveVariableDebtToken(address)"]:
    for tok,label in [(USDC,"aaveUSDC"),(CIRCLE,"circleUSDC")]:
        r=call(POOL, sel(sig)+a(tok))
        print(f"  {sig} {label}: {r.get('result', r.get('error'))}")

print("\n=== faucet selectors from bytecode ===")
code=rpc("eth_getCode",[FAUCET,"latest"])["result"]
b=bytes.fromhex(code[2:])
# crude PUSH4 scan
cands=set()
i=0
while i < len(b):
    op=b[i]
    if op==0x63 and i+5<=len(b):
        cands.add(b[i+1:i+5].hex()); i+=5
    elif 0x60<=op<=0x7f:
        i+= 1 + (op-0x5f)
    else: i+=1
known={}
for sig in ["mint(address,address,uint256)","mint(address,uint256)","isPermissioned()","setPermissioned(bool)",
            "owner()","transferOwnership(address)","MAX_MINT_AMOUNT()","maxMintAmount()","getMaxMintAmount(address)",
            "MINT_TIMELOCK()","mintTimelock()","timelock()","getLastMintTimestamp(address,address)",
            "setProtectedOfChild(address,bool)","transferOwnershipOfChild(address[],address)",
            "renounceOwnership()","setMintable(address,bool)","isMintable(address)",
            "MAX_MINT_AMOUNT(address)","_maxMintAmount()","mintLimit()","getMintAmount(address)"]:
    h=s4(sig).hex()
    if h in cands: known[h]=sig
print("  matched:", json.dumps(known, indent=4))
print(f"  total push4 candidates: {len(cands)}")
print("  unmatched sample:", sorted(cands)[:40])

print("\n=== simulate faucet mint from wallet ===")
for amt in [1_000_000, 10_000_000, 1]:
    r=call(FAUCET, sel("mint(address,address,uint256)")+a(USDC)+a(W)+u(amt), frm=W)
    print(f"  mint(usdc, wallet, {amt}) ->", r.get("result", r.get("error")))
