"""
Web3 Resource Adapter — Gas-Aware Budget Monitoring.

Provides real-time gas-price awareness for supported chains (Sui, Monad,
Solana).  The Governor consults this adapter before approving on-chain
operations to ensure gas costs won't breach the agent's budget threshold.

Design:
    - Each chain has a dedicated ``_fetch_*`` method that hits the chain's
      JSON-RPC endpoint.
    - Results are cached briefly (configurable) to avoid hammering nodes.
    - If the node is unreachable, the adapter returns a *fail-closed* result
      (i.e., ``is_gas_affordable`` returns False) to prevent unbudgeted spend.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

logger = logging.getLogger("aegis.adapters.web3")


class Chain(str, Enum):
    SUI = "sui"
    MONAD = "monad"
    SOLANA = "solana"


@dataclass
class GasQuote:
    """Snapshot of gas conditions on a specific chain."""
    chain: Chain
    gas_price_native: float  # Price in the chain's native unit
    gas_price_usd: float     # Estimated USD equivalent
    timestamp: float = field(default_factory=time.time)
    is_stale: bool = False


@dataclass
class Web3Adapter:
    """
    Gas-price oracle and budget gatekeeper for on-chain operations.

    Parameters
    ----------
    rpc_urls : dict[Chain, str]
        JSON-RPC endpoints for each supported chain.
    cache_ttl : float
        Seconds before a cached gas quote is considered stale.
    http_timeout : float
        Seconds to wait for an RPC response.
    """

    rpc_urls: dict[Chain, str] = field(default_factory=lambda: {
        Chain.SUI: "https://fullnode.mainnet.sui.io:443",
        Chain.MONAD: "https://rpc.monad.xyz",
        Chain.SOLANA: "https://api.mainnet-beta.solana.com",
    })
    cache_ttl: float = 15.0
    http_timeout: float = 5.0

    _cache: dict[Chain, GasQuote] = field(init=False, default_factory=dict)

    # Rough USD-per-native-unit estimates (operators should feed real prices).
    _native_usd: dict[Chain, float] = field(default_factory=lambda: {
        Chain.SUI: 1.10,
        Chain.MONAD: 0.50,
        Chain.SOLANA: 145.0,
    })

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_gas_affordable(
        self,
        chain: Chain,
        budget_threshold_usd: float,
        estimated_gas_units: int = 1,
    ) -> bool:
        """
        Return True only if the current gas cost is within the agent's
        budget threshold.  Fail-closed: returns False on any error.

        Parameters
        ----------
        chain : Chain
            Target blockchain.
        budget_threshold_usd : float
            Maximum acceptable gas cost in USD for this operation.
        estimated_gas_units : int
            Number of gas units the operation is expected to consume.
        """
        quote = await self.get_gas_quote(chain)
        if quote is None or quote.is_stale:
            logger.warning(
                "Gas quote unavailable or stale for %s — fail-closed.", chain.value
            )
            return False

        total_usd = quote.gas_price_usd * estimated_gas_units
        affordable = total_usd <= budget_threshold_usd
        if not affordable:
            logger.info(
                "Gas NOT affordable on %s: $%.6f × %d units = $%.6f > threshold $%.4f",
                chain.value, quote.gas_price_usd, estimated_gas_units,
                total_usd, budget_threshold_usd,
            )
        return affordable

    async def get_gas_quote(self, chain: Chain) -> Optional[GasQuote]:
        """
        Fetch the current gas price for *chain*, using cache if fresh.
        """
        cached = self._cache.get(chain)
        if cached and (time.time() - cached.timestamp) < self.cache_ttl:
            return cached

        try:
            quote = await self._fetch_gas(chain)
            self._cache[chain] = quote
            return quote
        except Exception as exc:
            logger.error("Failed to fetch gas for %s: %s", chain.value, exc)
            # Return stale cache if available.
            if cached:
                cached_stale = GasQuote(
                    chain=cached.chain,
                    gas_price_native=cached.gas_price_native,
                    gas_price_usd=cached.gas_price_usd,
                    timestamp=cached.timestamp,
                    is_stale=True,
                )
                return cached_stale
            return None

    # ------------------------------------------------------------------
    # Chain-specific RPC calls
    # ------------------------------------------------------------------

    async def _fetch_gas(self, chain: Chain) -> GasQuote:
        """Dispatch to the appropriate chain fetcher."""
        fetchers = {
            Chain.SUI: self._fetch_sui_gas,
            Chain.MONAD: self._fetch_monad_gas,
            Chain.SOLANA: self._fetch_solana_gas,
        }
        return await fetchers[chain]()

    async def _fetch_sui_gas(self) -> GasQuote:
        """Query Sui's ``suix_getReferenceGasPrice`` RPC method."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "suix_getReferenceGasPrice",
            "params": [],
        }
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.post(self.rpc_urls[Chain.SUI], json=payload)
            resp.raise_for_status()
            data = resp.json()

        # Sui returns gas price in MIST (1 SUI = 1e9 MIST).
        mist_price = int(data["result"])
        sui_price = mist_price / 1e9
        usd_price = sui_price * self._native_usd[Chain.SUI]
        return GasQuote(chain=Chain.SUI, gas_price_native=sui_price, gas_price_usd=usd_price)

    async def _fetch_monad_gas(self) -> GasQuote:
        """Query Monad's EVM-compatible ``eth_gasPrice`` RPC method."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_gasPrice",
            "params": [],
        }
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.post(self.rpc_urls[Chain.MONAD], json=payload)
            resp.raise_for_status()
            data = resp.json()

        # Result is hex-encoded wei.  Convert to native (1 MON = 1e18 wei).
        wei = int(data["result"], 16)
        native_price = wei / 1e18
        usd_price = native_price * self._native_usd[Chain.MONAD]
        return GasQuote(chain=Chain.MONAD, gas_price_native=native_price, gas_price_usd=usd_price)

    async def _fetch_solana_gas(self) -> GasQuote:
        """Query Solana's ``getRecentPrioritizationFees`` for fee estimates."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getRecentPrioritizationFees",
            "params": [],
        }
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.post(self.rpc_urls[Chain.SOLANA], json=payload)
            resp.raise_for_status()
            data = resp.json()

        # Average the recent priority fees (in micro-lamports per CU).
        fees = data.get("result", [])
        if not fees:
            avg_fee = 5000  # Fallback: base fee in lamports
        else:
            avg_fee = sum(f["prioritizationFee"] for f in fees) / len(fees)

        # Convert lamports to SOL (1 SOL = 1e9 lamports).
        sol_price = avg_fee / 1e9
        usd_price = sol_price * self._native_usd[Chain.SOLANA]
        return GasQuote(chain=Chain.SOLANA, gas_price_native=sol_price, gas_price_usd=usd_price)
