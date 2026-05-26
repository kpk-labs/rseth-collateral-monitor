# rseth-collateral-monitor

On-chain monitoring scripts for rsETH (Kelp DAO) collateralization on **Ethereum mainnet**.

> **Scope**: Ethereum mainnet only. Extending to multichain rsETH supply is tracked in [issue #2](https://github.com/kpk-labs/rseth-collateral-monitor/issues/2).

---

## What it monitors

rsETH is a liquid restaking token (LRT). It functions like a liability on a balance sheet — the protocol issues rsETH against underlying collateral (stETH, ETHx, ETH) held across several contracts and delegated to EigenLayer operators via NodeDelegators.

These scripts map that collateral and report:
- Where each unit of underlying asset is sitting (NodeDelegators, DepositPool, UnstakingVault, WithdrawalManager, Aave)
- How much of each asset is active in EigenLayer vs. in transit vs. locked as pending withdrawals
- The collateralization ratio: total collateral in ETH / rsETH supply in ETH
- Pending withdrawal requests and their claim status

---

## Scripts

### `query_balance_sheet.py`

Builds the full collateral balance sheet as a matrix of location × asset.

**Rows**: per NodeDelegator (direct balance / EigenLayer active / EigenLayer withdrawal queue), LRTDepositPool, LRTUnstakingVault, LRTWithdrawalManager locked, WM unlocked, WM Aave.

**Columns**: one per supported LST asset + ETH + ETH equivalent total.

**Footer**: collateralization ratio using prices from LRTOracle.

NodeDelegators and supported assets are read live from on-chain — no hardcoded lists that go stale.

### `query_pending_withdrawals.py`

Reports all pending withdrawal amounts per asset:
- Locked requests: not yet unlocked by `unlockQueue` (assets still in LRTUnstakingVault)
- Unlocked requests: unlocked but user hasn't called `completeWithdrawal` yet

Works even when the contract is paused (read-only calls).

---

## Setup

**Requirements**: Python 3.10+ and an Ethereum mainnet RPC endpoint.

```bash
pip install -r requirements.txt
```

Set your RPC URL:

```bash
export RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
```

---

## Usage

```bash
# Full collateral balance sheet + collateralization ratio
python query_balance_sheet.py

# Pending withdrawal amounts per asset
python query_pending_withdrawals.py

# Query at a specific block
python query_balance_sheet.py --block 21000000

# Adjust RPC rate limit (default 5 calls/sec)
python query_balance_sheet.py --rps 3
```

---

## Contract addresses (Ethereum mainnet)

| Contract | Address |
|----------|---------|
| LRTConfig | `0x947Cb49334e6571ccBFEF1f1f1178d8469D65ec7` |
| LRTDepositPool | `0x036676389e48133B63a802f8635AD39E752D375D` |
| LRTUnstakingVault | `0xc66830E2667bc740c0BED9A71F18B14B8c8184bA` |
| LRTWithdrawalManager | `0x62De59c08eB5dAE4b7E6F7a8cAd3006d6965ec16` |
| LRTOracle | `0x349A73444b1a310BAe67ef67973022020d70020d` |
| rsETH | `0xA1290d69c65A6Fe4DF752f95823fae25cB99e5A7` |

NodeDelegator addresses are fetched live from `LRTDepositPool.getNodeDelegatorQueue()`.
