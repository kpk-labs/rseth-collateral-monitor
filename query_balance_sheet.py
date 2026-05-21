"""
Purpose: Build the full collateral balance sheet of rsETH — assets vs. liabilities.
         Maps where every unit of underlying collateral sits across all protocol contracts.

Inputs:
    - RPC_URL env var (or --rpc CLI arg): Ethereum mainnet JSON-RPC endpoint
    - Optional --block: block number to query at (defaults to latest)
    - Optional --rps: max RPC calls per second (default 5)

Outputs:
    Matrix of (location × asset) with amounts in native units (18 decimals).
    Rows: per NDC (direct / EigenLayer active / EigenLayer queue),
          LRTDepositPool, LRTUnstakingVault,
          LRTWithdrawalManager locked, WM unlocked, WM Aave.
    Columns: one per supported asset + ETH.
    Footer: column totals (total collateral per asset) and rsETH total supply.

Logic:
    1. Load supported assets from LRTConfig.getSupportedAssetList() + ETH sentinel.
    2. Load active NodeDelegators from LRTDepositPool.getNodeDelegatorQueue() (live, not hardcoded).
       For each NDC, read elOperatorDelegatedTo() as the label.
    3. For each NodeDelegator, read three layers per asset:
       - Direct balance: ERC20.balanceOf(NDC) / ETH balance
       - EigenLayer active: NDC.getAssetBalance(asset) for LSTs,
                            NDC.getEffectivePodShares() for ETH
       - EigenLayer queue: NDC.getAssetUnstaking(asset)
    4. For LRTDepositPool and LRTUnstakingVault: ERC20.balanceOf / ETH balance.
    5. For LRTWithdrawalManager:
       - Locked (assetsCommitted): assets still in UnstakingVault awaiting unlockQueue
       - Unlocked (contract balance): owed to users pending completeWithdrawal
       - Aave (getAaveBalance): ETH parked in aWETH while waiting for claims
    6. Print matrix and totals.

Assumptions:
    - All supported assets use 18 decimals.
    - ETH_TOKEN sentinel = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE.
    - getEffectivePodShares() returns ETH in wei (18 decimals).
    - getAssetUnstaking(ETH_TOKEN) returns wei (EigenPod shares == ETH wei).

Known limitations:
    - Does not convert to a common ETH unit (no oracle calls); amounts are in native units.
    - Legacy NodeDelegators (5 removed from queue) are not queried.
    - getAssetBalance may briefly undercount during an EigenLayer slashing event.
"""

import argparse
import os
import sys
import time
from collections import deque
from web3 import Web3, HTTPProvider

# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding-window rate limiter: at most max_per_second calls per second."""

    def __init__(self, max_per_second: int):
        self.max_per_second = max_per_second
        self._timestamps: deque[float] = deque()

    def tick(self) -> None:
        now = time.monotonic()
        while self._timestamps and self._timestamps[0] <= now - 1.0:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_per_second:
            sleep_for = self._timestamps[0] + 1.0 - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._timestamps and self._timestamps[0] <= now - 1.0:
                self._timestamps.popleft()
        self._timestamps.append(time.monotonic())


class RateLimitedHTTPProvider(HTTPProvider):
    def __init__(self, *args, rate_limiter: RateLimiter, **kwargs):
        super().__init__(*args, **kwargs)
        self._rate_limiter = rate_limiter

    def make_request(self, method, params):
        self._rate_limiter.tick()
        return super().make_request(method, params)


# ── Addresses ─────────────────────────────────────────────────────────────────

ETH_TOKEN          = Web3.to_checksum_address("0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE")
LRT_CONFIG         = Web3.to_checksum_address("0x947Cb49334e6571ccBFEF1f1f1178d8469D65ec7")
LRT_DEPOSIT_POOL   = Web3.to_checksum_address("0x036676389e48133B63a802f8635AD39E752D375D")
LRT_UNSTAKING_VAULT= Web3.to_checksum_address("0xc66830E2667bc740c0BED9A71F18B14B8c8184bA")
WITHDRAWAL_MANAGER = Web3.to_checksum_address("0x62De59c08eB5dAE4b7E6F7a8cAd3006d6965ec16")
RSETH              = Web3.to_checksum_address("0xA1290d69c65A6Fe4DF752f95823fae25cB99e5A7")
LRT_ORACLE         = Web3.to_checksum_address("0x349A73444b1a310BAe67ef67973022020d70020d")

# ── ABIs ──────────────────────────────────────────────────────────────────────

ABI_LRT_CONFIG = [
    {"name": "getSupportedAssetList", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address[]"}]},
]

ABI_LRT_DEPOSIT_POOL = [
    {"name": "getNodeDelegatorQueue", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address[]"}]},
]

ABI_ERC20 = [
    {"name": "symbol",    "type": "function", "stateMutability": "view",
     "inputs": [],        "outputs": [{"type": "string"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [],          "outputs": [{"type": "uint256"}]},
]

ABI_NODE_DELEGATOR = [
    {"name": "getAssetBalance",       "type": "function", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "getEffectivePodShares", "type": "function", "stateMutability": "view",
     "inputs": [],                    "outputs": [{"name": "ethStaked", "type": "uint256"}]},
    {"name": "getAssetUnstaking",     "type": "function", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"name": "amount", "type": "uint256"}]},
    {"name": "elOperatorDelegatedTo", "type": "function", "stateMutability": "view",
     "inputs": [],                    "outputs": [{"type": "address"}]},
]

ABI_WITHDRAWAL_MANAGER = [
    {"name": "assetsCommitted", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "getAaveBalance",  "type": "function", "stateMutability": "view",
     "inputs": [],              "outputs": [{"type": "uint256"}]},
]

ABI_LRT_ORACLE = [
    {"name": "getAssetPrice", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "rsETHPrice",    "type": "function", "stateMutability": "view",
     "inputs": [],            "outputs": [{"type": "uint256"}]},
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt(amount: int, width: int = 14) -> str:
    return f"{amount / 1e18:>{width}.4f}"


def get_symbol(w3: Web3, asset: str, block: int | str) -> str:
    if asset.lower() == ETH_TOKEN.lower():
        return "ETH"
    try:
        return w3.eth.contract(address=asset, abi=ABI_ERC20).functions.symbol().call(block_identifier=block)
    except Exception:
        return asset[:8]


def token_balance(w3: Web3, token: str, holder: str, block: int | str) -> int:
    if token.lower() == ETH_TOKEN.lower():
        return w3.eth.get_balance(holder, block_identifier=block)
    return w3.eth.contract(address=token, abi=ABI_ERC20).functions.balanceOf(holder).call(block_identifier=block)


def safe_call(fn, *args, block, default=0):
    try:
        return fn(*args).call(block_identifier=block)
    except Exception:
        return default


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="rsETH collateral balance sheet")
    parser.add_argument("--rpc",   default=os.getenv("RPC_URL"), help="Ethereum RPC URL")
    parser.add_argument("--block", default="latest",             help="Block number or 'latest'")
    parser.add_argument("--rps",   default=5, type=int,          help="Max RPC calls per second")
    args = parser.parse_args()

    if not args.rpc:
        sys.exit("ERROR: provide RPC URL via --rpc or RPC_URL env var")

    rl = RateLimiter(max_per_second=args.rps)
    w3 = Web3(RateLimitedHTTPProvider(args.rpc, rate_limiter=rl))
    if not w3.is_connected():
        sys.exit("ERROR: could not connect to RPC")

    block = int(args.block) if args.block != "latest" else "latest"
    current_block = w3.eth.block_number
    print(f"Connected. Current block: {current_block}")
    print(f"Querying at: {block}\n")

    config      = w3.eth.contract(address=LRT_CONFIG,        abi=ABI_LRT_CONFIG)
    deposit_pool= w3.eth.contract(address=LRT_DEPOSIT_POOL,  abi=ABI_LRT_DEPOSIT_POOL)
    wm          = w3.eth.contract(address=WITHDRAWAL_MANAGER,abi=ABI_WITHDRAWAL_MANAGER)
    oracle      = w3.eth.contract(address=LRT_ORACLE,         abi=ABI_LRT_ORACLE)
    rseth       = w3.eth.contract(address=RSETH,              abi=ABI_ERC20)

    # Assets: supported LSTs + ETH (deduplicated)
    supported = config.functions.getSupportedAssetList().call(block_identifier=block)
    seen = {a.lower() for a in supported}
    if ETH_TOKEN.lower() not in seen:
        supported = supported + [ETH_TOKEN]
    assets = [Web3.to_checksum_address(a) for a in supported]
    symbols = [get_symbol(w3, a, block) for a in assets]

    # NodeDelegators: read live from LRTDepositPool (not hardcoded)
    ndc_addrs = deposit_pool.functions.getNodeDelegatorQueue().call(block_identifier=block)
    node_delegators = []
    for i, addr in enumerate(ndc_addrs):
        addr = Web3.to_checksum_address(addr)
        ndc = w3.eth.contract(address=addr, abi=ABI_NODE_DELEGATOR)
        operator = safe_call(ndc.functions.elOperatorDelegatedTo, block=block)
        operator_label = f"{operator[:8]}..." if operator and operator != "0x" + "0" * 40 else "unassigned"
        node_delegators.append((addr, f"NDC-{i} ({operator_label})"))
    print(f"NodeDelegators found: {len(node_delegators)}")
    for addr, label in node_delegators:
        print(f"  {label}: {addr}")
    print()

    # Asset prices from LRTOracle (ETH = 1e18 by definition, no oracle needed)
    prices = {}
    for asset in assets:
        if asset.lower() == ETH_TOKEN.lower():
            prices[asset] = 10**18
        else:
            prices[asset] = safe_call(oracle.functions.getAssetPrice, asset, block=block, default=10**18)

    # rsETH price and total supply (liabilities)
    rseth_price  = safe_call(oracle.functions.rsETHPrice, block=block, default=10**18)
    rseth_supply = rseth.functions.totalSupply().call(block_identifier=block)

    # ── Collect data ──────────────────────────────────────────────────────────
    # rows: list of (label, {asset_addr: amount})
    rows = []

    # NodeDelegators
    for ndc_addr, ndc_label in node_delegators:
        ndc = w3.eth.contract(address=ndc_addr, abi=ABI_NODE_DELEGATOR)
        direct, el_active, el_queue = {}, {}, {}

        for asset in assets:
            # Direct balance in contract
            direct[asset] = token_balance(w3, asset, ndc_addr, block)

            if asset.lower() == ETH_TOKEN.lower():
                # EigenLayer active: EigenPod (verified + unverified)
                el_active[asset] = safe_call(ndc.functions.getEffectivePodShares, block=block)
            else:
                # EigenLayer active: strategy shares converted to underlying
                el_active[asset] = safe_call(ndc.functions.getAssetBalance, asset, block=block)

            # EigenLayer withdrawal queue (exiting EigenLayer, not yet in vault)
            el_queue[asset] = safe_call(ndc.functions.getAssetUnstaking, asset, block=block)

        rows.append((f"{ndc_label} direct",    direct))
        rows.append((f"{ndc_label} EL active", el_active))
        rows.append((f"{ndc_label} EL queue",  el_queue))

    # LRTDepositPool
    pool_balances = {a: token_balance(w3, a, LRT_DEPOSIT_POOL, block) for a in assets}
    rows.append(("DepositPool", pool_balances))

    # LRTUnstakingVault
    vault_balances = {a: token_balance(w3, a, LRT_UNSTAKING_VAULT, block) for a in assets}
    rows.append(("UnstakingVault", vault_balances))

    # LRTWithdrawalManager — locked (assetsCommitted, still in vault)
    wm_locked = {a: safe_call(wm.functions.assetsCommitted, a, block=block) for a in assets}
    rows.append(("WM locked", wm_locked))

    # LRTWithdrawalManager — unlocked (owed to users, in contract)
    wm_unlocked = {a: token_balance(w3, a, WITHDRAWAL_MANAGER, block) for a in assets}
    rows.append(("WM unlocked", wm_unlocked))

    # LRTWithdrawalManager — Aave (ETH parked as aWETH)
    aave_balance = safe_call(wm.functions.getAaveBalance, block=block)
    wm_aave = {a: (aave_balance if a.lower() == ETH_TOKEN.lower() else 0) for a in assets}
    rows.append(("WM Aave (ETH)", wm_aave))

    # ── Print matrix ──────────────────────────────────────────────────────────
    COL_W   = 14
    LABEL_W = 26

    eth_col  = "ETH equiv"
    header = f"{'Location':<{LABEL_W}}" + "".join(f"{s:>{COL_W}}" for s in symbols) + f"{eth_col:>{COL_W}}"
    sep    = "-" * len(header)

    print(sep)
    print(header)
    print(sep)

    col_totals     = {a: 0 for a in assets}
    grand_eth_total = 0

    def row_eth_equiv(balances: dict) -> int:
        return sum(int(balances.get(a, 0) * prices[a] / 1e18) for a in assets)

    prev_ndc = None
    for label, balances in rows:
        ndc_prefix = label.split(" ")[0] if "NDC" in label else None
        if ndc_prefix != prev_ndc and prev_ndc is not None:
            print()
        prev_ndc = ndc_prefix

        eth_equiv = row_eth_equiv(balances)
        row_str = (f"{label:<{LABEL_W}}"
                   + "".join(fmt(balances.get(a, 0), COL_W) for a in assets)
                   + fmt(eth_equiv, COL_W))
        print(row_str)

        for a in assets:
            col_totals[a] += balances.get(a, 0)
        grand_eth_total += eth_equiv

    print(sep)
    totals_str = (f"{'TOTAL COLLATERAL':<{LABEL_W}}"
                  + "".join(fmt(col_totals[a], COL_W) for a in assets)
                  + fmt(grand_eth_total, COL_W))
    print(totals_str)
    print(sep)

    # ── Collateralization ratio ───────────────────────────────────────────────
    rseth_supply_eth = int(rseth_supply * rseth_price / 1e18)
    ratio = grand_eth_total / rseth_supply_eth if rseth_supply_eth > 0 else 0

    print(f"\n{'rsETH total supply':<{LABEL_W}}{fmt(rseth_supply, COL_W)}")
    print(f"{'rsETH price (oracle)':<{LABEL_W}}{rseth_price / 1e18:>{COL_W}.6f}")
    print(f"{'rsETH supply (ETH)':<{LABEL_W}}{fmt(rseth_supply_eth, COL_W)}")
    print(f"\n{'Collateralization ratio':<{LABEL_W}}{ratio:>{COL_W}.4f}x")
    print(f"\nAsset prices used (ETH, from LRTOracle):")
    for asset, sym in zip(assets, symbols):
        print(f"  {sym:<10} {prices[asset] / 1e18:.6f}")


if __name__ == "__main__":
    main()
