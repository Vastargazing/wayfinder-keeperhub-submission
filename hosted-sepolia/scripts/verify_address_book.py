"""Verify every candidate Base Sepolia Aave address ON CHAIN, using the SDK's own ABIs."""
import os
import asyncio, json, sys
sys.path.insert(0, os.environ.get("WAYFINDER_SDK", "../wayfinder/upstream"))
from web3 import AsyncWeb3, AsyncHTTPProvider
from wayfinder_paths.core.constants.aave_v3_abi import (
    UI_POOL_DATA_PROVIDER_ABI, UI_INCENTIVE_DATA_PROVIDER_V3_ABI, UI_POOL_RESERVE_KEYS,
    WRAPPED_TOKEN_GATEWAY_V3_ABI,
)
RPC="https://sepolia.base.org"
PROVIDER="0xE4C23309117Aa30342BFaae6c95c6478e0A4Ad00"
UIPOOL="0x3cB7B00B6C09B71998124196691e8bF2694De863"
UIINC="0xDB1412acf288D5bE057f8e90fd7b1BF4f84bB3B1"
GATEWAY="0x0568130e794429D2eEBC4dafE18f25Ff1a1ed8b6"
USDC="0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f"
AUSDC="0x10F1A9D11CDf50041f3f8cB7191CBE2f31750ACC"

async def main():
    w3 = AsyncWeb3(AsyncHTTPProvider(RPC, request_kwargs={"timeout": 40}))
    out = {}
    out["chain_id"] = await w3.eth.chain_id
    for name, addr in [("ui_pool_data_provider",UIPOOL),("ui_incentive_data_provider",UIINC),("wrapped_token_gateway",GATEWAY)]:
        code = await w3.eth.get_code(w3.to_checksum_address(addr))
        out[f"{name}_code_len"] = len(code)

    ui = w3.eth.contract(address=w3.to_checksum_address(UIPOOL), abi=UI_POOL_DATA_PROVIDER_ABI)
    reserves, base_ccy = await ui.functions.getReservesData(w3.to_checksum_address(PROVIDER)).call()
    rows=[]
    for r in reserves:
        d = dict(zip(UI_POOL_RESERVE_KEYS, r))
        rows.append({"underlyingAsset": d["underlyingAsset"], "symbol": d["symbol"], "decimals": d["decimals"],
                     "aTokenAddress": d["aTokenAddress"], "variableDebtTokenAddress": d["variableDebtTokenAddress"],
                     "isActive": d["isActive"], "isFrozen": d["isFrozen"], "isPaused": d["isPaused"],
                     "supplyCap": d["supplyCap"], "liquidityRate": d["liquidityRate"],
                     "priceInMarketReferenceCurrency": d["priceInMarketReferenceCurrency"]})
    out["ui_pool_reserve_count"] = len(rows)
    out["ui_pool_reserves"] = rows
    usdc_row = [r for r in rows if r["underlyingAsset"].lower()==USDC.lower()]
    out["usdc_row_found"] = bool(usdc_row)
    if usdc_row:
        out["usdc_atoken_matches"] = usdc_row[0]["aTokenAddress"].lower()==AUSDC.lower()
        out["usdc_row"] = usdc_row[0]
    out["base_currency_info"] = list(base_ccy)

    try:
        uii = w3.eth.contract(address=w3.to_checksum_address(UIINC), abi=UI_INCENTIVE_DATA_PROVIDER_V3_ABI)
        inc = await uii.functions.getReservesIncentivesData(w3.to_checksum_address(PROVIDER)).call()
        out["ui_incentive_rows"] = len(inc)
        out["ui_incentive_underlyings"] = [str(r[0]) for r in inc]
    except Exception as e:
        out["ui_incentive_error"] = f"{type(e).__name__}: {e}"

    try:
        gw = w3.eth.contract(address=w3.to_checksum_address(GATEWAY), abi=WRAPPED_TOKEN_GATEWAY_V3_ABI)
        out["gateway_getWETHAddress"] = await gw.functions.getWETHAddress().call()
    except Exception as e:
        out["gateway_error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out, indent=2, default=str))
    await w3.provider.disconnect()

asyncio.run(main())
