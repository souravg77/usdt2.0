"""Check a user's exchange key before it is stored.

Real-account keys are accepted only when Binance itself reports that the key can trade spot,
**cannot withdraw**, cannot do margin or futures transfers, and is restricted to trusted IPs.
Testnet keys only have to work (testnet money can't be withdrawn). Keys that fail are never
stored.
"""

from __future__ import annotations

from ..config import ExchangeKeys, Settings

SUPPORTED = ("binance",)  # HTX gives no reliable way to read a key's withdrawal permission


class KeyRejected(ValueError):
    pass


async def verify_keys(exchange: str, testnet: bool, api_key: str, secret: str,
                      settings: Settings, client=None) -> dict:
    """Return the key's permissions, or raise KeyRejected with a reason a user can act on."""
    from ..exchanges.ccxt_adapter import ADAPTERS

    if exchange not in SUPPORTED:
        raise KeyRejected(f"{exchange} keys aren't supported yet; use Binance")
    api_key, secret = (api_key or "").strip(), (secret or "").strip()
    if len(api_key) < 16 or len(secret) < 16 or any(c.isspace() for c in api_key + secret):
        raise KeyRejected("that doesn't look like an API key and secret; copy both again")
    adapter = ADAPTERS[exchange](settings, keys=ExchangeKeys(api_key, secret), testnet=testnet, client=client)
    try:
        try:
            bal = await adapter.balance()
        except Exception as exc:  # wrong key, wrong IP, or not a testnet key
            raise KeyRejected(f"the exchange refused the key ({type(exc).__name__}). Check that you "
                              f"copied a {'testnet' if testnet else 'real-account'} key and secret") from None
        perms = {"balance_assets": len([a for a, v in bal.free.items() if v])}
        if testnet:
            return perms
        try:
            r = await adapter.client.sapi_get_account_apirestrictions()
        except Exception as exc:
            raise KeyRejected(f"couldn't read the key's permissions ({type(exc).__name__})") from None
        flags = {k: _true(r.get(k)) for k in ("enableWithdrawals", "enableSpotAndMarginTrading",
                                              "enableMargin", "enableFutures", "ipRestrict",
                                              "enableInternalTransfer", "permitsUniversalTransfer")}
        perms.update(flags)
        if flags["enableWithdrawals"]:
            raise KeyRejected("this key can WITHDRAW funds. Turn off 'Enable Withdrawals' on Binance "
                              "(API Management, Edit restrictions), or make a new key without it")
        if flags["enableInternalTransfer"] or flags["permitsUniversalTransfer"]:
            raise KeyRejected("this key can transfer funds; turn off transfers on Binance")
        if flags["enableMargin"] or flags["enableFutures"]:
            raise KeyRejected("turn off margin and futures for this key; the agent trades spot only")
        if not flags["enableSpotAndMarginTrading"]:
            raise KeyRejected("turn on 'Enable Spot & Margin Trading' for this key")
        if not flags["ipRestrict"]:
            raise KeyRejected("restrict this key to trusted IPs on Binance and add the server's IP")
        return perms
    finally:
        await adapter.close()


def _true(v) -> bool:
    return v is True or str(v).lower() == "true"
