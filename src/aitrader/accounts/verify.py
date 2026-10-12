"""Check a user's exchange key before it is stored.

Real-account keys are accepted only when Bybit itself reports (``/v5/user/query-api``) that the
key can trade spot, is not read-only, **cannot withdraw** or transfer, and is bound to trusted
IPs. Contracts / Derivatives / Options permissions are allowed (the agent sells USDT perpetuals
short); ``futures`` in the result says whether the key can trade them. Keys that fail are never
stored.
"""

from __future__ import annotations

from ..config import ExchangeKeys, Settings

SUPPORTED = ("bybit",)


class KeyRejected(ValueError):
    pass


class KeyUnverified(KeyRejected):
    """The check couldn't run (network, exchange down, region block): the key may be fine."""


_AUTH_ERRORS = ("AuthenticationError", "PermissionDenied", "AccountNotEnabled", "AccountSuspended")


def _is_auth_error(exc: Exception) -> bool:
    return any(c.__name__ in _AUTH_ERRORS for c in type(exc).__mro__)


async def verify_keys(exchange: str, api_key: str, secret: str, settings: Settings, client=None) -> dict:
    """Return the key's permissions, or raise KeyRejected with a reason a user can act on."""
    from ..exchanges.ccxt_adapter import ADAPTERS

    if exchange not in SUPPORTED:
        raise KeyRejected(f"{exchange} keys aren't supported; the agent uses Bybit only")
    api_key, secret = (api_key or "").strip(), (secret or "").strip()
    if len(api_key) < 16 or len(secret) < 16 or any(c.isspace() for c in api_key + secret):
        raise KeyRejected("that doesn't look like an API key and secret; copy both again")
    adapter = ADAPTERS[exchange](settings, keys=ExchangeKeys(api_key, secret), client=client)
    try:
        try:
            bal = await adapter.balance()
        except Exception as exc:
            if _is_auth_error(exc):  # wrong key, wrong IP, or a key from Bybit's demo section
                raise KeyRejected(f"Bybit refused the key ({type(exc).__name__}). Check that you "
                                  "copied a real-account key and its secret in full") from None
            # never echo the exception text: ccxt puts the signed request URL in it
            raise KeyUnverified(f"couldn't reach Bybit to check the key ({type(exc).__name__})") from None
        perms = {"balance_assets": len([a for a, v in bal.free.items() if v])}
        try:
            r = (await adapter.client.private_get_v5_user_query_api()).get("result") or {}
        except Exception as exc:
            raise KeyRejected(f"couldn't read the key's permissions ({type(exc).__name__})") from None
        p = r.get("permissions") or {}
        wallet = [str(x) for x in p.get("Wallet") or []]
        ips = [str(x) for x in r.get("ips") or []]
        perms.update(read_only=str(r.get("readOnly")) == "1", spot=list(p.get("Spot") or []), wallet=wallet,
                     derivatives=bool(p.get("ContractTrade") or p.get("Derivatives") or p.get("Options")),
                     futures=bool(p.get("ContractTrade") or p.get("Derivatives")),
                     ip_restricted=bool(ips) and "*" not in ips)
        if "Withdraw" in wallet:
            raise KeyRejected("this key can WITHDRAW funds. On Bybit (API Management) make a new key "
                              "without the Withdrawal permission")
        if wallet:
            raise KeyRejected("this key can transfer funds (Assets permissions); make a key with only "
                              "SPOT and Contracts trade permissions")
        if perms["read_only"] or "SpotTrade" not in perms["spot"]:
            raise KeyRejected("this key can't trade: choose 'Read-Write' and tick SPOT Trade")
        if not perms["ip_restricted"]:
            raise KeyRejected("bind this key to this server's IP on Bybit (Only IPs with permissions "
                              "granted are allowed to access the OpenAPI)")
        return perms
    finally:
        await adapter.close()

