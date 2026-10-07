"""Minimal Solana JSON-RPC client over httpx. Only the calls the resolver needs."""

from __future__ import annotations

from typing import Any

import httpx


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SolanaRpc:
    def __init__(self, url: str, client: httpx.AsyncClient, commitment: str = "confirmed"):
        if not url:
            raise RpcError("SOLANA_RPC_URL is not configured", retryable=False)
        self.url = url
        self.client = client
        self.commitment = commitment
        self._id = 0

    async def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        try:
            r = await self.client.post(self.url, json=body)
        except httpx.HTTPError as e:
            raise RpcError(f"rpc transport error: {type(e).__name__}: {e}") from e
        if r.status_code == 429:
            raise RpcError("rpc rate limited (429)")
        if r.status_code >= 500:
            raise RpcError(f"rpc http {r.status_code}")
        if r.status_code != 200:
            raise RpcError(f"rpc http {r.status_code}: {r.text[:200]}", retryable=False)
        try:
            data = r.json()
        except ValueError as e:  # e.g. an HTML error page from a proxy with status 200
            raise RpcError(f"rpc returned non-JSON: {r.text[:120]!r}") from e
        if not isinstance(data, dict):
            raise RpcError(f"rpc returned unexpected JSON: {str(data)[:120]}")
        if "error" in data:
            err = data["error"] or {}
            code = err.get("code")
            # -32601 method not found (e.g. no DAS on this provider): not retryable
            raise RpcError(
                f"rpc error {code}: {err.get('message')}", code=code, retryable=code != -32601
            )
        return data.get("result")

    async def get_account_info(self, pubkey: str, encoding: str = "jsonParsed") -> dict | None:
        res = await self.call(
            "getAccountInfo", [pubkey, {"encoding": encoding, "commitment": self.commitment}]
        )
        return (res or {}).get("value")

    async def get_multiple_accounts(self, pubkeys: list[str], encoding: str = "base64") -> list:
        res = await self.call(
            "getMultipleAccounts", [pubkeys, {"encoding": encoding, "commitment": self.commitment}]
        )
        value = (res or {}).get("value") or [None] * len(pubkeys)
        if len(value) != len(pubkeys):
            raise RpcError(f"getMultipleAccounts returned {len(value)} of {len(pubkeys)} accounts")
        return value

    async def get_asset(self, mint: str) -> dict | None:
        """DAS getAsset (Helius and some others). Returns None when unsupported."""
        try:
            return await self.call("getAsset", [{"id": mint}])
        except RpcError as e:
            if e.code == -32601 or not e.retryable:
                return None
            raise

    async def get_signatures(
        self, address: str, before: str | None = None, limit: int = 1000
    ) -> list[dict]:
        opts: dict[str, Any] = {"limit": limit, "commitment": self.commitment}
        if before:
            opts["before"] = before
        return await self.call("getSignaturesForAddress", [address, opts]) or []

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "commitment": self.commitment,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
