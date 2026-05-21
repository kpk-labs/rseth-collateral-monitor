"""
Purpose: Query LRTWithdrawalManager to report all pending withdrawal amounts per asset.
         Works even when the contract is paused (only read calls).

Inputs:
    - RPC_URL env var (or --rpc CLI arg): Ethereum mainnet JSON-RPC endpoint
    - Optional --block: block number to query at (defaults to latest)
    - Optional --rps: max RPC calls per second (default 2)

Outputs:
    Per asset:
    - Locked requests: not yet unlocked by unlockQueue (assets still in LRTUnstakingVault)
    - Unlocked requests: unlocked but user hasn't called completeWithdrawal yet (assets in this contract)
    - Totals in ETH-denominated units (18 decimals)

Logic:
    1. Load supported assets from LRTConfig.getSupportedAssetList() + ETH sentinel.
    2. For each asset, read nextUnusedNonce, nextLockedNonce, unlockedWithdrawalsCount, assetsCommitted.
    3. Unlocked-but-not-completed amount = contract's balance of that asset.
       Rationale: unlockQueue redeems exactly the payout amounts from the vault into this contract,
       and completeWithdrawal transfers them out to users. So balance == total owed at all times.
       For ETH: contract ETH balance + Aave aWETH balance (ETH may have been auto-deposited).
    4. Report locked (assetsCommitted) + unlocked (contract balance) per asset.

Assumptions:
    - All LST assets use 18 decimals.
    - ETH_TOKEN sentinel = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE.
    - Contract holds no surplus LST balance beyond what is owed to users (sweepRemainingAssets
      would have cleared any excess). Small rounding differences (<1 wei) are ignored.

Known limitations:
    - Contract ETH balance may include dust from failed transfers or direct sends, overstating
      the unlocked amount slightly. Use unlockedWithdrawalsCount as a sanity check on the count.
"""

import argparse
import os
import sys
import time
from collections import deque
from web3 import Web3
from web3 import HTTPProvider

# ── Rate limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding-window rate limiter: enforces at most `max_per_second` calls per second."""

    def __init__(self, max_per_second: int):
        self.max_per_second = max_per_second
        self._timestamps: deque[float] = deque()

    def tick(self) -> None:
        now = time.monotonic()
        # Drop timestamps older than 1 second
        while self._timestamps and self._timestamps[0] <= now - 1.0:
            self._timestamps.popleft()
        # If at the limit, sleep until the oldest call falls outside the 1-second window
        if len(self._timestamps) >= self.max_per_second:
            sleep_for = self._timestamps[0] + 1.0 - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._timestamps and self._timestamps[0] <= now - 1.0:
                self._timestamps.popleft()
        self._timestamps.append(time.monotonic())


class RateLimitedHTTPProvider(HTTPProvider):
    """HTTPProvider that enforces a per-second call limit on every make_request."""

    def __init__(self, *args, rate_limiter: RateLimiter, **kwargs):
        super().__init__(*args, **kwargs)
        self._rate_limiter = rate_limiter

    def make_request(self, method, params):
        self._rate_limiter.tick()
        return super().make_request(method, params)


# ── Contract addresses (mainnet) ─────────────────────────────────────────────

WITHDRAWAL_MANAGER = Web3.to_checksum_address("0x62De59c08eB5dAE4b7E6F7a8cAd3006d6965ec16")
LRT_CONFIG         = Web3.to_checksum_address("0x947Cb49334e6571ccBFEF1f1f1178d8469D65ec7")
ETH_TOKEN          = Web3.to_checksum_address("0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE")

# ── Minimal ABIs ─────────────────────────────────────────────────────────────

ABI_WITHDRAWAL_MANAGER = [
    {"name": "paused",                   "type": "function", "stateMutability": "view",
     "inputs": [],                       "outputs": [{"type": "bool"}]},
    {"name": "nextUnusedNonce",          "type": "function", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "nextLockedNonce",          "type": "function", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "unlockedWithdrawalsCount", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "assetsCommitted",          "type": "function", "stateMutability": "view",
     "inputs": [{"name": "asset", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "withdrawalRequests",       "type": "function", "stateMutability": "view",
     "inputs": [{"name": "requestId", "type": "bytes32"}],
     "outputs": [
         {"name": "rsETHUnstaked",       "type": "uint256"},
         {"name": "expectedAssetAmount", "type": "uint256"},
         {"name": "withdrawalStartBlock","type": "uint256"},
     ]},
    {"name": "isAaveIntegrationEnabled", "type": "function", "stateMutability": "view",
     "inputs": [],                       "outputs": [{"type": "bool"}]},
    {"name": "totalETHDepositedToAave",  "type": "function", "stateMutability": "view",
     "inputs": [],                       "outputs": [{"type": "uint256"}]},
    {"name": "getAaveBalance",           "type": "function", "stateMutability": "view",
     "inputs": [],                       "outputs": [{"type": "uint256"}]},
]

ABI_LRT_CONFIG = [
    {"name": "getSupportedAssetList", "type": "function", "stateMutability": "view",
     "inputs": [],                    "outputs": [{"type": "address[]"}]},
]

ABI_ERC20 = [
    {"name": "symbol",   "type": "function", "stateMutability": "view",
     "inputs": [],       "outputs": [{"type": "string"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [],       "outputs": [{"type": "uint8"}]},
    {"name": "balanceOf","type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_request_id(asset: str, nonce: int) -> bytes:
    return Web3.solidity_keccak(["address", "uint256"], [asset, nonce])


def fmt(amount: int, decimals: int = 18) -> str:
    return f"{amount / 10**decimals:.6f}"


def asset_symbol(w3: Web3, asset: str) -> str:
    if asset.lower() == ETH_TOKEN.lower():
        return "ETH"
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(asset), abi=ABI_ERC20)
        return token.functions.symbol().call()
    except Exception:
        return asset[:10] + "..."


def asset_decimals(w3: Web3, asset: str) -> int:
    if asset.lower() == ETH_TOKEN.lower():
        return 18
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(asset), abi=ABI_ERC20)
        return token.functions.decimals().call()
    except Exception:
        return 18


def get_unlocked_balance(w3: Web3, asset: str, block: int | str) -> int:
    """
    Returns the amount owed to users with unlocked-but-not-completed withdrawals.

    After unlockQueue, the contract holds exactly the payout amounts redeemed from the vault.
    Each completeWithdrawal transfers that amount out, so the contract balance equals
    the total still owed. For ETH, also includes the Aave aWETH balance.
    """
    if asset.lower() == ETH_TOKEN.lower():
        return w3.eth.get_balance(WITHDRAWAL_MANAGER, block_identifier=block)
    token = w3.eth.contract(address=Web3.to_checksum_address(asset), abi=ABI_ERC20)
    return token.functions.balanceOf(WITHDRAWAL_MANAGER).call(block_identifier=block)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Query LRTWithdrawalManager pending withdrawals")
    parser.add_argument("--rpc",   default=os.getenv("RPC_URL"), help="Ethereum RPC URL")
    parser.add_argument("--block", default="latest",             help="Block number or 'latest'")
    parser.add_argument("--rps",   default=2, type=int,          help="Max RPC calls per second (default 2)")
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

    wm     = w3.eth.contract(address=WITHDRAWAL_MANAGER, abi=ABI_WITHDRAWAL_MANAGER)
    config = w3.eth.contract(address=LRT_CONFIG,         abi=ABI_LRT_CONFIG)

    # Pause state
    is_paused = wm.functions.paused().call(block_identifier=block)
    print(f"Contract paused: {is_paused}\n")

    # Supported assets (from LRTConfig) + ETH sentinel (deduplicated)
    supported = config.functions.getSupportedAssetList().call(block_identifier=block)
    seen = {a.lower() for a in supported}
    if ETH_TOKEN.lower() not in seen:
        supported = supported + [ETH_TOKEN]
    assets = supported

    # Aave state (ETH only)
    aave_enabled       = wm.functions.isAaveIntegrationEnabled().call(block_identifier=block)
    aave_balance       = wm.functions.getAaveBalance().call(block_identifier=block)
    total_eth_in_aave  = wm.functions.totalETHDepositedToAave().call(block_identifier=block)

    grand_total_locked   = {}  # asset -> wei
    grand_total_unlocked = {}  # asset -> wei

    print("=" * 70)
    for asset in assets:
        symbol = asset_symbol(w3, asset)
        dec    = asset_decimals(w3, asset)

        next_unused  = wm.functions.nextUnusedNonce(asset).call(block_identifier=block)
        next_locked  = wm.functions.nextLockedNonce(asset).call(block_identifier=block)
        unlocked_cnt = wm.functions.unlockedWithdrawalsCount(asset).call(block_identifier=block)
        committed    = wm.functions.assetsCommitted(asset).call(block_identifier=block)

        locked_req_count = next_unused - next_locked

        # Unlocked amount = contract balance for that asset (exact, O(1) calls).
        # For ETH, the contract balance already includes what's NOT in Aave;
        # add aave_balance separately to get the full picture.
        contract_balance = get_unlocked_balance(w3, asset, block)

        if asset.lower() == ETH_TOKEN.lower():
            unlocked_amount = contract_balance + aave_balance
        else:
            unlocked_amount = contract_balance

        grand_total_locked[symbol]   = committed
        grand_total_unlocked[symbol] = unlocked_amount

        print(f"\nAsset: {symbol} ({asset})")
        print(f"  Total requests ever:            {next_unused}")
        print(f"  ── Locked (pending unlockQueue)")
        print(f"     Requests:                    {locked_req_count}")
        print(f"     Assets committed (in vault): {fmt(committed, dec)} {symbol}")
        print(f"  ── Unlocked (pending completeWithdrawal)")
        print(f"     Count:                       {unlocked_cnt}")
        print(f"     Assets owed to users:        {fmt(unlocked_amount, dec)} {symbol}")
        print(f"  ── TOTAL pending to users:      {fmt(committed + unlocked_amount, dec)} {symbol}")

        if asset.lower() == ETH_TOKEN.lower():
            print(f"\n  Aave integration enabled:     {aave_enabled}")
            if aave_enabled:
                print(f"  Aave aWETH balance:           {fmt(aave_balance)} ETH")
                print(f"  Aave principal deposited:     {fmt(total_eth_in_aave)} ETH")
                accrued = aave_balance - total_eth_in_aave if aave_balance > total_eth_in_aave else 0
                print(f"  Aave accrued interest:        {fmt(accrued)} ETH")
            print(f"  Contract ETH balance:         {fmt(contract_balance)} ETH")

        print()

    print("=" * 70)
    print("SUMMARY")
    for symbol in grand_total_locked:
        total = grand_total_locked[symbol] + grand_total_unlocked[symbol]
        print(f"  {symbol:8s}  locked={fmt(grand_total_locked[symbol])}  unlocked={fmt(grand_total_unlocked[symbol])}  total={fmt(total)}")


if __name__ == "__main__":
    main()
