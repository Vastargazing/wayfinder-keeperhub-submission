import httpx, json
from eth_utils import keccak, to_checksum_address
RPC="https://sepolia.base.org"; POOL="0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
n=[0]
def rpc(m,p):
    n[0]+=1
    with httpx.Client(timeout=60,trust_env=False) as c:
        return c.post(RPC,json={"jsonrpc":"2.0","id":n[0],"method":m,"params":p}).json()
def t0(s): return "0x"+keccak(text=s).hex()
SUP=t0("Supply(address,address,address,uint256,uint16)")
WDR=t0("Withdraw(address,address,address,uint256)")
head=int(rpc("eth_blockNumber",[])["result"],16)
STEP=9_999
def scan(topic, max_chunks=60):
    hi=head
    for _ in range(max_chunks):
        lo=max(0,hi-STEP)
        r=rpc("eth_getLogs",[{"address":POOL,"topics":[topic],"fromBlock":hex(lo),"toBlock":hex(hi)}])
        if r.get("result"): return r["result"][-1], (lo,hi)
        hi=lo-1
        if hi<=0: break
    return None,None
def A(t): return to_checksum_address("0x"+t[-40:])
for name,topic in [("Supply",SUP),("Withdraw",WDR)]:
    lg,win=scan(topic)
    print(f"\n=== {name}  topic0={topic} ===")
    if not lg:
        print(f"  none found in last {60*STEP} blocks"); continue
    print(f"  block {int(lg['blockNumber'],16)}  tx {lg['transactionHash']}  logIndex {int(lg['logIndex'],16)}")
    print(f"  emitter {lg['address']}  (== pool: {lg['address'].lower()==POOL.lower()})")
    for i,t in enumerate(lg["topics"]): print(f"    topic{i}: {t}" + (f"   -> addr {A(t)}" if i>0 and t[2:26]=='0'*24 else ""))
    d=bytes.fromhex(lg["data"][2:])
    print(f"  data words: {len(d)//32}")
    for i in range(0,len(d),32):
        w=d[i:i+32].hex()
        extra = f"  addr={A(w)}" if w[:24]=="0"*24 and int(w,16)!=0 else f"  uint={int(w,16)}"
        print(f"    w{i//32}: {w}{extra}")
