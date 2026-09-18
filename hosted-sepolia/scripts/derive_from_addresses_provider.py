import httpx, json
from eth_utils import function_signature_to_4byte_selector as s4, to_checksum_address, keccak
RPC="https://sepolia.base.org"
POOL="0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
AUSDC="0x10F1A9D11CDf50041f3f8cB7191CBE2f31750ACC"
n=[0]
def call(to,data):
    n[0]+=1
    with httpx.Client(timeout=30,trust_env=False) as c:
        return c.post(RPC,json={"jsonrpc":"2.0","id":n[0],"method":"eth_call","params":[{"to":to,"data":data},"latest"]}).json()
def sel(s): return "0x"+s4(s).hex()
def A(w): return to_checksum_address("0x"+w[-40:])
def get(to,sig,label):
    r=call(to,sel(sig))
    if "result" in r and len(r["result"])>=66:
        v=A(r["result"][2:]); print(f"  {label:38} {v}"); return v
    print(f"  {label:38} ERROR {str(r.get('error'))[:60]}"); return None

print("=== from Pool ===")
ap = get(POOL,"ADDRESSES_PROVIDER()","ADDRESSES_PROVIDER")
print("\n=== from PoolAddressesProvider ===")
if ap:
    get(ap,"getPool()","getPool")
    get(ap,"getPriceOracle()","getPriceOracle (oracle)")
    get(ap,"getPoolDataProvider()","getPoolDataProvider")
    get(ap,"getACLManager()","getACLManager")
    get(ap,"getPoolConfigurator()","getPoolConfigurator")
    r=call(ap, sel("getMarketId()"))
    if "result" in r:
        raw=bytes.fromhex(r["result"][2:]); ln=int.from_bytes(raw[32:64],"big")
        print(f"  {'getMarketId':38} {raw[64:64+ln].decode()}")
    # getAddress(bytes32) for UI providers - try known ids
    for name in ["UI_POOL_DATA_PROVIDER","UI_INCENTIVE_DATA_PROVIDER","WALLET_BALANCE_PROVIDER","INCENTIVES_CONTROLLER","EMISSION_MANAGER"]:
        idb = keccak(text=name).hex()
        r=call(ap, sel("getAddress(bytes32)")+idb)
        if "result" in r and int(r["result"],16)!=0:
            print(f"  getAddress({name}) -> {A(r['result'][2:])}")
print("\n=== from aToken ===")
get(AUSDC,"getIncentivesController()","rewards_controller")
get(AUSDC,"RESERVE_TREASURY_ADDRESS()","treasury")
