import html as html_lib
import functools
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone

import gnupg
import numpy as np
import requests
import ring
from base91 import decode, encode
from decouple import config
from django.conf import settings
from django.utils import timezone

from api.errors import new_error
from api.mempool import _mempool_fees_with_hard_timeout
from api.models import Robot

logger = logging.getLogger("api.utils")

TOR_PROXY = config("TOR_PROXY", default="127.0.0.1:9050")
USE_TOR = config("USE_TOR", cast=bool, default=True)
LNVENDOR = config("LNVENDOR", cast=str, default="LND")


def get_session():
    session = requests.session()
    # Tor uses the 9050 port as the default socks port
    if USE_TOR:
        session.proxies = {
            "http": "socks5h://" + TOR_PROXY,
            "https": "socks5h://" + TOR_PROXY,
        }
    return session


_FEDERATION_BUNDLED_PATH = os.path.join(os.path.dirname(__file__), "federation.json")


def _federation_doc_paths() -> list[str]:
    """Custom FEDERATION_JSON_PATH first, bundled copy as fallback — mirrors _load_federation_doc."""
    paths = [config("FEDERATION_JSON_PATH", default="", cast=str).strip()]
    paths.append(_FEDERATION_BUNDLED_PATH)
    return [p for p in paths if p]


@functools.lru_cache(maxsize=1)
def get_federation_short_alias() -> str:
    """
    Resolves this coordinator's federation routing `shortAlias` from its
    COORDINATOR_ALIAS env identity, which may differ from the `shortAlias`
    used in /order/<shortAlias>/<orderId>/ URLs (e.g. 'LibreBazaar' → 'bazaar').
    Matches (lowercased, space-stripped) against each entry's key,
    `identifier` and `longAlias` in the federation document.
    """
    alias = config("COORDINATOR_ALIAS", cast=str, default="").lower().replace(" ", "")
    if not alias:
        return alias

    for path in _federation_doc_paths():
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load federation document '{path}': {e}")
            continue

        for key, entry in doc.items():
            candidates = (
                key,
                str(entry.get("identifier", "")),
                str(entry.get("longAlias", "")),
            )
            if alias in (c.lower().replace(" ", "") for c in candidates):
                return key

    return alias


def bitcoind_rpc(method, params=None):
    """
    Makes a RPC call to bitcoin core daemon
    :param method: RPC method to call
    :param params: list of params required by the calling RPC method
    :return:
    """

    BITCOIND_RPCURL = config("BITCOIND_RPCURL")
    BITCOIND_RPCUSER = config("BITCOIND_RPCUSER")
    BITCOIND_RPCPASSWORD = config("BITCOIND_RPCPASSWORD")

    if params is None:
        params = []

    payload = json.dumps(
        {"jsonrpc": "2.0", "id": "robosats", "method": method, "params": params}
    )
    return requests.post(
        BITCOIND_RPCURL, auth=(BITCOIND_RPCUSER, BITCOIND_RPCPASSWORD), data=payload
    ).json()["result"]


def validate_onchain_address(address):
    """
    Validates an onchain address
    """

    try:
        validation = bitcoind_rpc("validateaddress", [address])
        if not validation["isvalid"]:
            return False, {"bad_address": "Invalid address"}
    except Exception as e:
        logger.error(e)
        return False, {
            "bad_address": "Unable to validate address, check bitcoind backend"
        }

    return True, None


mining_fee = {}


@ring.dict(mining_fee, expire=60)  # keeps in cache for 60 seconds
def get_minning_fee(priority: str, preliminary_amount: int) -> int:
    """
    priority: (str) 'suggested' | 'minimum'
    Fetches suggested and minimum mining fee rates from mempool.space
    uses LND/CLN fee estimator as fallback.

    mempool.space response object:
    {
        fastestFee: 1,
        halfHourFee: 1,
        hourFee: 1,
        economyFee: 1,
        minimumFee: 1
    }
    Where 'suggested' is 'fastestFee' and 'minimum' is 'economyFee'
    """

    from api.lightning.node import LNNode

    try:
        data = _mempool_fees_with_hard_timeout()

        if priority == "suggested":
            value = data.get("fastestFee")
        elif priority == "minimum":
            value = data.get("economyFee")
        else:
            raise Exception(
                "an error occurred",
                "unexpected value for mining fee priority",
                priority,
            )

    except Exception as e:
        logger.warning(
            "Falling back to LN fee estimator after mempool fetch failed (%s)",
            type(e).__name__,
        )
        # Fetch mining fee from LND/CLN instance
        if priority == "suggested":
            target_conf = config("SUGGESTED_TARGET_CONF", cast=int, default=2)
        if priority == "minimum":
            target_conf = config("MINIMUM_TARGET_CONF", cast=int, default=24)

        value = LNNode.estimate_fee(
            amount_sats=preliminary_amount,
            target_conf=target_conf,
        )["mining_fee_rate"]

    return value


def get_devfund_pubkey() -> str:
    """Returns the devfund Lightning node pubkey from the DEVFUND_PUBKEY env variable.
    Roblosats: without it, the Roblosats maintainer's node on the BLAKE2b Lightning
    network, from the devfund_pubkey.json shipped in the image (never fetched at run
    time)."""
    pubkey = config("DEVFUND_PUBKEY", cast=str, default="")
    if pubkey:
        return pubkey
    try:
        path = os.path.join(settings.BASE_DIR, "devfund_pubkey.json")
        with open(path) as f:
            return json.load(f).get(config("NETWORK", cast=str, default="mainnet"), "")
    except Exception:
        return ""


lnurlp_metadata_cache = {}


@ring.dict(lnurlp_metadata_cache, expire=43200)  # keeps in cache for 12 hours
def _fetch_lnurlp_metadata(lnurlp_url: str) -> dict:
    """
    GETs and returns the raw LNURL-pay metadata dict for *lnurlp_url*.
    Cached per URL for 12 hours so repeated donations to the same Lightning
    Address never hit the remote server more than once every 12 hours.
    Raises ValueError on any network or HTTP error.
    """
    session = get_session()
    try:
        resp = session.get(lnurlp_url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise ValueError(f"Failed to fetch LNURL-pay metadata from {lnurlp_url}: {e}")


def resolve_lightning_address(
    address: str, num_satoshis: int, comment: str = ""
) -> str:
    """
    Resolves a Lightning Address (user@domain) via LNURL-pay and returns a
    BOLT11 invoice for exactly *num_satoshis* sats.

    Raises ValueError with a descriptive message on any validation failure so
    the caller can log it and skip the payment without crashing.

    Steps:
      1. Validate address format  (user@domain)
      2. GET https://{domain}/.well-known/lnurlp/{user}  →  LNURL-pay metadata
         (cached per address for 12 hours via _fetch_lnurlp_metadata)
      3. Validate tag, minSendable / maxSendable bounds
      4. GET {callback}?amount={msat}[&comment=…]  →  bolt11 invoice
      5. Decode & verify the invoice amount matches num_satoshis exactly
    """
    from api.lightning.node import LNNode

    # --- 1. format check ---
    if "@" not in address or address.count("@") != 1:
        raise ValueError(f"Invalid Lightning Address format: {address!r}")
    user, domain = address.split("@", 1)
    if not user or not domain or "." not in domain:
        raise ValueError(f"Invalid Lightning Address format: {address!r}")

    # --- 2. fetch LNURL-pay metadata (cached 12 h) ---
    lnurlp_url = f"https://{domain}/.well-known/lnurlp/{user}"
    metadata = _fetch_lnurlp_metadata(lnurlp_url)

    # --- 3. validate metadata ---
    if metadata.get("tag") != "payRequest":
        raise ValueError(
            f"Unexpected LNURL-pay tag {metadata.get('tag')!r} for {address}"
        )
    callback = metadata.get("callback")
    if not callback:
        raise ValueError(f"No callback in LNURL-pay metadata for {address}")

    min_sendable_msat = metadata.get("minSendable", 0)
    max_sendable_msat = metadata.get("maxSendable", 0)
    amount_msat = num_satoshis * 1000

    if amount_msat < min_sendable_msat:
        raise ValueError(
            f"Amount {num_satoshis} sats ({amount_msat} msat) is below "
            f"minSendable {min_sendable_msat} msat for {address}"
        )
    if max_sendable_msat > 0 and amount_msat > max_sendable_msat:
        raise ValueError(
            f"Amount {num_satoshis} sats ({amount_msat} msat) exceeds "
            f"maxSendable {max_sendable_msat} msat for {address}"
        )

    # --- 4. request invoice ---
    params: dict = {"amount": amount_msat}
    comment_allowed = metadata.get("commentAllowed", 0)
    if comment and comment_allowed and len(comment) <= int(comment_allowed):
        params["comment"] = comment

    session = get_session()
    try:
        inv_resp = session.get(callback, params=params, timeout=10)
        inv_resp.raise_for_status()
        inv_data = inv_resp.json()
    except Exception as e:
        raise ValueError(
            f"Failed to fetch invoice from LNURL-pay callback for {address}: {e}"
        )

    invoice = inv_data.get("pr")
    if not invoice:
        raise ValueError(
            f"No invoice (pr) in LNURL-pay callback response for {address}"
        )

    # --- 5. verify invoice amount ---
    try:
        decoded = LNNode.decode_payreq(invoice)
        invoice_sats = int(decoded.num_satoshis)
    except Exception as e:
        raise ValueError(f"Failed to decode invoice from {address}: {e}")

    if invoice_sats != num_satoshis:
        raise ValueError(
            f"Invoice amount mismatch for {address}: "
            f"expected {num_satoshis} sats, got {invoice_sats} sats"
        )

    return invoice


market_cache = {}


def neoxa_rates(session, api_url, currencies):
    """
    Roblosats: the price of BTC on the BLAKE2b chain (BTCB2). Neoxa is where it trades, and
    only against USDC (taken as USD) and BTC. Every other fiat currency is that USD price
    times yadio's USD exchange rate for it.
    [api_url] is Neoxa's BTCB2_USDC ticker, straight (neoxa.exchange/api/exchange/ticker/
    BTCB2_USDC) or through a proxy ending in /neoxa-ticker (mempool.kilombino.com has one,
    reachable over Tor, where Neoxa's own site is not); the BTC pair is the same URL with
    BTCB2_BTC, or /neoxa-ticker-btc.
    """
    # Single venue, kept for coordinators configured before nonkyc_rates (22 oct 2026):
    # prefer MARKET_PRICE_APIS=<nonkyc ticker> + NEOXA_TICKER_URL. At least the quote is
    # the order-book midpoint with a sane spread, not the last trade (one small trade
    # moves that).
    print("MARKET_PRICE_APIS points at Neoxa alone: one venue; see nonkyc_rates")
    try:
        t = session.get(api_url).json()["ticker"]
        bid, ask = float(t["bestBid"]), float(t["bestAsk"])
        usd = (bid + ask) / 2
        if not (bid > 0 and ask >= bid) or (ask - bid) / usd > config(
            "BTCB2_MAX_SPREAD", cast=float, default=0.05
        ):
            raise Exception(f"Neoxa book unusable (bid {bid}, ask {ask})")
    except Exception as e:
        # If Neoxa is gone, the coordinator's own price (BTCB2_FALLBACK_USD) keeps the book
        # priced; users can always set explicit sats amounts too.
        usd = config("BTCB2_FALLBACK_USD", cast=float, default=0.0)
        if not usd > 0:
            raise e
        print(f"Neoxa price unavailable ({str(e)}); using BTCB2_FALLBACK_USD={usd}")
    fx = {}
    try:
        fx = session.get(
            config("FX_RATES_API", cast=str, default="https://api.yadio.io/exrates/USD")
        ).json()["USD"]
    except Exception as e:
        print(f"Could not fetch USD exchange rates: {str(e)}")
    btc = np.nan
    if "BTC" in currencies:
        try:
            t = session.get(
                api_url.replace("BTCB2_USDC", "BTCB2_BTC")
                if "BTCB2_USDC" in api_url
                else api_url + "-btc"
            ).json()["ticker"]
            bid, ask = float(t["bestBid"]), float(t["bestAsk"])
            if bid > 0 and ask >= bid:
                btc = (bid + ask) / 2
        except Exception:
            pass
    rates = []
    for currency in currencies:
        if currency == "USD":
            rates.append(usd)
        elif currency == "BTC":
            rates.append(btc)
        else:
            try:
                rates.append(usd * float(fx[currency]))
            except Exception:
                rates.append(np.nan)
    return rates


# When the BTCB2 price last had venue quorum (unix time), for BTCB2_SINGLE_VENUE_GRACE.
# In the Django cache (Redis, no expiry): survives restarts of the app; if Redis loses it,
# the grace period simply starts again.
BTCB2_QUORUM_KEY = "roblosats:btcb2_quorum_at"


def _get_quorum_at():
    try:
        from django.core.cache import cache

        v = cache.get(BTCB2_QUORUM_KEY)
        return float(v) if v is not None else None
    except Exception as e:
        print(f"Could not read {BTCB2_QUORUM_KEY}: {str(e)}")
        return None


def _set_quorum_at(t):
    try:
        from django.core.cache import cache

        cache.set(BTCB2_QUORUM_KEY, t, None)
    except Exception as e:
        print(f"Could not store {BTCB2_QUORUM_KEY}: {str(e)}")


def nonkyc_rates(session, api_url, currencies):
    """
    Roblosats: the price of BTCB2 from NonKYC (22 oct 2026, after an audit found that a
    single last-trade price from one exchange could be moved with one small trade).
    [api_url] is NonKYC's BTCB2_USDT ticker (api.nonkyc.io/api/v2/ticker/BTCB2_USDT), or
    a proxy ending in /nonkyc-ticker (mempool.kilombino.com has one: NonKYC blocks Tor
    exits); the BTC pair is the same URL with BTCB2_BTC, or /nonkyc-ticker-btc.

    Every quote is the MIDPOINT of the order book (bid/ask), never the last trade, and only
    if the spread is sane (BTCB2_MAX_SPREAD). NonKYC is one venue (the mean of its BTCB2/USDT
    and BTCB2/BTC x BTC/USD books); Neoxa (NEOXA_TICKER_URL) is the other. The price is the
    mean of the venues (they must agree within BTCB2_MAX_VENUE_DEVIATION). It never moves
    more than BTCB2_MAX_STEP from the last stored price per refresh. With fewer than BTCB2_MIN_VENUES (2) venues answering, or
    two that disagree, the last stored price is kept for BTCB2_SINGLE_VENUE_GRACE seconds
    (1 h) from the last refresh with quorum; after that the closest remaining venue is
    followed, still capped by BTCB2_MAX_STEP. No venue at all keeps the last stored price
    (BTCB2_FALLBACK_USD on first start). Other fiat currencies are that USD price times
    yadio's USD rate for them, as before.

    Trust: the default proxies (mempool.kilombino.com) are run by Roblosats' maintainer.
    A coordinator with clearnet access can point straight at the venues instead
    (MARKET_PRICE_APIS=https://api.nonkyc.io/api/v2/ticker/BTCB2_USDT,
    NEOXA_TICKER_URL=https://neoxa.exchange/api/exchange/ticker/BTCB2_USDC).
    """
    max_spread = config("BTCB2_MAX_SPREAD", cast=float, default=0.05)

    def mid(bid, ask):
        bid, ask = float(bid), float(ask)
        if not (bid > 0 and ask > 0 and ask >= bid):
            return None
        m = (bid + ask) / 2
        return m if (ask - bid) / m <= max_spread else None

    fx_all = {}
    try:
        fx_all = session.get(
            config("FX_RATES_API", cast=str, default="https://api.yadio.io/exrates/USD")
        ).json()
    except Exception as e:
        print(f"Could not fetch USD exchange rates: {str(e)}")
    fx = fx_all.get("USD", {})
    btc_usd = fx_all.get("BTC")

    nonkyc = []  # NonKYC's two books are one venue
    neoxa = None
    btc = np.nan
    try:
        t = session.get(api_url).json()
        q = mid(t["bid"], t["ask"])
        if q:
            nonkyc.append(q)
    except Exception as e:
        print(f"NonKYC BTCB2_USDT unavailable: {str(e)}")
    try:
        t = session.get(
            api_url.replace("BTCB2_USDT", "BTCB2_BTC")
            if "BTCB2_USDT" in api_url
            else api_url + "-btc"
        ).json()
        q = mid(t["bid"], t["ask"])
        if q:
            btc = q
            if btc_usd:
                nonkyc.append(q * float(btc_usd))
    except Exception as e:
        print(f"NonKYC BTCB2_BTC unavailable: {str(e)}")
    neoxa_url = config("NEOXA_TICKER_URL", cast=str, default="")
    if neoxa_url:
        try:
            t = session.get(neoxa_url).json()["ticker"]
            neoxa = mid(t["bestBid"], t["bestAsk"])
        except Exception as e:
            print(f"Neoxa unavailable: {str(e)}")

    venues = ([float(np.mean(nonkyc))] if nonkyc else []) + ([neoxa] if neoxa else [])
    last = None
    try:
        from api.models import Currency

        # currency_dict maps code -> label ({"1": "USD", ...}): look the code up by label.
        # (rl9-rl11 indexed it by "USD", a KeyError swallowed here, so `last` was always
        # None and neither BTCB2_MAX_STEP nor the closer-venue rule ever applied.)
        usd_code = next(int(k) for k, v in Currency.currency_dict.items() if v == "USD")
        last = float(Currency.objects.get(currency=usd_code).exchange_rate)
    except Exception as e:
        print(f"No stored BTCB2 USD price to compare with: {str(e)}")
    # Quorum: at least BTCB2_MIN_VENUES (2) distinct venues answering and, with two, agreeing
    # within BTCB2_MAX_VENUE_DEVIATION. Without quorum the last stored price is kept, but
    # only for BTCB2_SINGLE_VENUE_GRACE seconds (1 h) counted from the last refresh that had
    # quorum: after that the closest remaining venue is followed again, still capped by
    # BTCB2_MAX_STEP per refresh. (One venue alone could otherwise walk the price at will;
    # a frozen price forever is wrong too when a venue is gone for good.)
    min_venues = config("BTCB2_MIN_VENUES", cast=int, default=2)
    grace = config("BTCB2_SINGLE_VENUE_GRACE", cast=int, default=3600)
    max_dev = config("BTCB2_MAX_VENUE_DEVIATION", cast=float, default=0.10)
    disagree = len(venues) == 2 and abs(venues[0] - venues[1]) / min(venues) > max_dev
    if disagree:
        print(f"BTCB2 venues disagree (NonKYC {venues[0]:.2f}, Neoxa {venues[1]:.2f})")
    has_last = bool(last and last > 0)
    now = time.time()

    def closest():
        return min(venues, key=lambda v: abs(v - last)) if has_last else min(venues)

    if venues and len(venues) >= min_venues and not (disagree and min_venues >= 2):
        usd = closest() if disagree else float(np.mean(venues))
        _set_quorum_at(now)
    elif venues and not has_last:
        # First start: nothing stored yet, so one venue (or the lower one) is enough.
        usd = closest()
    elif venues:
        since = _get_quorum_at()
        if since is None:
            # Unknown (cache emptied): start the grace period now, the safe side.
            since = now
            _set_quorum_at(now)
        if now - since < grace:
            usd = last
            print(
                f"{len(venues)} BTCB2 venue(s) usable (BTCB2_MIN_VENUES={min_venues}); keeping "
                f"the last price {last:.2f} ({int(now - since)} s of {grace} s without quorum)"
            )
        else:
            usd = closest()
            print(
                f"No BTCB2 venue quorum for {int(now - since)} s (> {grace} s); following "
                f"{usd:.2f}, at most BTCB2_MAX_STEP per refresh"
            )
    elif has_last:
        usd = last
        print(f"No BTCB2 quote; keeping the last price {last:.2f}")
    else:
        usd = config("BTCB2_FALLBACK_USD", cast=float, default=0.0)
        if not usd > 0:
            raise Exception("no BTCB2 price from NonKYC or Neoxa")
        print(f"No BTCB2 quote; using BTCB2_FALLBACK_USD={usd}")
    # And never move more than BTCB2_MAX_STEP per refresh: a real move gets there in a few
    # minutes, a one-off spike or a thin-book trick doesn't.
    if has_last:
        step = config("BTCB2_MAX_STEP", cast=float, default=0.05)
        usd = min(max(usd, last * (1 - step)), last * (1 + step))

    rates = []
    for currency in currencies:
        if currency == "USD":
            rates.append(usd)
        elif currency == "BTC":
            rates.append(btc)
        else:
            try:
                rates.append(usd * float(fx[currency]))
            except Exception:
                rates.append(np.nan)
    return rates


@ring.dict(market_cache, expire=30)  # keeps in cache for 30 seconds
def get_exchange_rates(currencies):
    """
    Params: list of currency codes.
    Checks for exchange rates in several public APIs.
    Returns the median price list.
    """

    session = get_session()

    APIS = config("MARKET_PRICE_APIS", cast=lambda v: [s.strip() for s in v.split(",")])

    api_rates = []
    for api_url in APIS:
        try:  # If one API is unavailable pass
            if "neoxa" in api_url:
                api_rates.append(neoxa_rates(session, api_url, currencies))

            elif "nonkyc" in api_url:
                api_rates.append(nonkyc_rates(session, api_url, currencies))

            elif "blockchain.info" in api_url:
                blockchain_prices = session.get(api_url).json()
                blockchain_rates = []
                for currency in currencies:
                    # Do not include ARS from Blockchain.info . This pricing is estimated wrongly.
                    if currency == "ARS":
                        blockchain_rates.append(np.nan)
                    else:
                        try:  # If a currency is missing place a None
                            blockchain_rates.append(
                                float(blockchain_prices[currency]["last"])
                            )
                        except Exception:
                            blockchain_rates.append(np.nan)
                api_rates.append(blockchain_rates)

            elif "yadio.io" in api_url:
                yadio_prices = session.get(api_url).json()
                yadio_rates = []
                for currency in currencies:
                    try:
                        yadio_rates.append(float(yadio_prices["BTC"][currency]))
                    except Exception:
                        yadio_rates.append(np.nan)
                api_rates.append(yadio_rates)

            # Tor proxied requests to bitpay.com will fail. Skip if USE_TOR is enabled.
            elif "bitpay.com" in api_url and not USE_TOR:
                headers = {
                    "X-Accept-Version": "2.0.0",
                    "Content-type": "application/json",
                }
                bitpay_prices = session.get(api_url, headers=headers).json()
                bitpay_prices = {
                    item["code"]: item["rate"] for item in bitpay_prices["data"]
                }
                bitpay_rates = []
                for currency in currencies:
                    try:
                        bitpay_rates.append(float(bitpay_prices[currency]))
                    except Exception:
                        bitpay_rates.append(np.nan)
                api_rates.append(bitpay_rates)

            # Tor proxied requests to criptoya.com will fail. Skip if USE_TOR is enabled.
            elif "criptoya.com" in api_url and not USE_TOR:
                criptoya_supported_currencies = [
                    "ARS",
                    "COP",
                    "MXN",
                    "BRL",
                    "PEN",
                    "CLP",
                    "USD",
                    "VES",
                ]
                criptoya_rates = []
                for currency in currencies:
                    if currency in criptoya_supported_currencies:
                        criptoya_exchanges = session.get(f"{api_url}/{currency}").json()
                        exchange_medians = [
                            np.median([exchange["ask"], exchange["bid"]])
                            for exchange in criptoya_exchanges.values()
                            if exchange["ask"] > 0 and exchange["bid"] > 0
                        ]
                        criptoya_rates.append(round(np.median(exchange_medians), 2))
                    else:
                        criptoya_rates.append(np.nan)
                api_rates.append(criptoya_rates)

        except Exception as e:
            print(f"Could not fetch BTC prices from {api_url}: {str(e)}")
            pass

    if len(api_rates) == 0:
        return None  # Wops there is not API available!

    exchange_rates = np.array(api_rates)
    median_rates = np.nanmedian(exchange_rates, axis=0)

    return median_rates.tolist()


lnd_version_cache = {}


@ring.dict(lnd_version_cache, expire=3600)
def get_lnd_version():
    if LNVENDOR == "LND":
        try:
            from api.lightning.lnd import LNDNode

            return LNDNode.get_version()
        except Exception:
            return "Not installed"
    else:
        return "Not installed"


cln_version_cache = {}


@ring.dict(cln_version_cache, expire=3600)
def get_cln_version():
    if LNVENDOR == "CLN":
        try:
            from api.lightning.cln import CLNNode

            return CLNNode.get_version()
        except Exception:
            return "Not installed"
    else:
        return "Not installed"


robosats_commit_cache = {}


@ring.dict(robosats_commit_cache, expire=99999)
def get_robosats_commit():
    # .git folder is included in .dockerignore. The build workflow will drop the commit_sha file in root
    with open("commit_sha") as f:
        commit_hash = f.read()

    return commit_hash


def weighted_median(values, sample_weight=None, quantiles=0.5, values_sorted=False):
    """Very close to numpy.percentile, but it supports weights.
    NOTE: quantiles should be in [0, 1]!
    :param values: numpy.array with data
    :param quantiles: array-like with many quantiles needed. For weighted median 0.5
    :param sample_weight: array-like of the same length as `array`
    :param values_sorted: bool, if True, then will avoid sorting of
        initial array assuming array is already sorted
    :return: numpy.array with computed quantiles.
    """
    values = np.array(values)
    quantiles = np.array(quantiles)
    if sample_weight is None:
        sample_weight = np.ones(len(values))
    sample_weight = np.array(sample_weight)
    assert np.all(quantiles >= 0) and np.all(quantiles <= 1), (
        "quantiles should be in [0, 1]"
    )

    if not values_sorted:
        sorter = np.argsort(values)
        values = values[sorter]
        sample_weight = sample_weight[sorter]

    weighted_quantiles = np.cumsum(sample_weight) - 0.5 * sample_weight
    weighted_quantiles -= weighted_quantiles[0]
    weighted_quantiles /= weighted_quantiles[-1]

    return np.interp(quantiles, weighted_quantiles, values)


def compute_avg_premium(queryset):
    premiums = []
    volumes = []

    # We exclude BTC, as LN <-> BTC swap premiums should not be  mixed with FIAT.

    for tick in queryset.exclude(currency=1000):
        premiums.append(float(tick.premium))
        volumes.append(float(tick.volume))

    total_volume = sum(volumes)

    # weighted_median_premium is the weighted median of the premiums by volume
    if len(premiums) > 0 and len(volumes) > 0:
        weighted_median_premium = weighted_median(
            values=premiums, sample_weight=volumes, quantiles=0.5, values_sorted=False
        )
    else:
        weighted_median_premium = 0.0
    return weighted_median_premium, total_volume


_ARMOR_BODY_LINE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _is_well_formed_pgp_key(key_text: str) -> bool:
    lines = key_text.strip().splitlines()
    if len(lines) < 4:
        return False
    if not lines[0].startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
        return False
    if not lines[-1].startswith("-----END PGP PUBLIC KEY BLOCK-----"):
        return False
    try:
        blank_idx = next(
            i for i, line in enumerate(lines[1:-1], 1) if line.strip() == ""
        )
    except StopIteration:
        return False
    for line in lines[blank_idx + 1 : -1]:
        if line.startswith("="):
            continue
        if not _ARMOR_BODY_LINE.match(line):
            return False
    return True


def validate_pgp_keys(pub_key, enc_priv_key):
    """Validates PGP valid keys. Formats them in a way understandable by the frontend"""

    # Standardize format with linux linebreaks '\n'. Windows users submitting their own keys have '\r\n' breaking communication.
    enc_priv_key = enc_priv_key.replace("\r\n", "\n").replace("\\", "\n")
    pub_key = pub_key.replace("\r\n", "\n").replace("\\", "\n")

    if not _is_well_formed_pgp_key(pub_key):
        return (
            False,
            new_error(
                1034,
                {
                    "import_pub_result_stderr": "Invalid PGP key format",
                    "import_pub_result_returncode": "",
                    "import_pub_result_summary": "",
                    "import_pub_result_results": str([]),
                    "import_pub_result_imported": "0",
                },
            ),
            None,
            None,
        )

    if Robot.objects.filter(public_key=pub_key).exists():
        return (
            False,
            new_error(1055),
            None,
            None,
        )

    gpg = gnupg.GPG(gnupghome=config("GNUPG_DIR", default=None))

    # Try to import the public key
    import_pub_result = gpg.import_keys(pub_key)
    if not import_pub_result.imported == 1:
        # If a robot is deleted and it is rebuilt with the same pubKey, the key will not be imported again
        # so we assert that the import error is "Not actually changed"
        if (
            import_pub_result.results
            and "Not actually changed" not in import_pub_result.results[0]["text"]
        ):
            return (
                False,
                new_error(
                    1034,
                    {
                        "import_pub_result_stderr": str(import_pub_result.stderr),
                        "import_pub_result_returncode": str(
                            import_pub_result.returncode
                        ),
                        "import_pub_result_summary": str(import_pub_result.summary),
                        "import_pub_result_results": str(import_pub_result.results),
                        "import_pub_result_imported": str(import_pub_result.imported),
                    },
                ),
                None,
                None,
            )
    # Check key creation timestamp
    if import_pub_result.fingerprints:
        keys = gpg.list_keys(keys=import_pub_result.fingerprints[0])
        if keys:
            key_creation = datetime.fromtimestamp(
                int(keys[0]["date"]), tz=datetime_timezone.utc
            )
            if key_creation > timezone.now() - timedelta(hours=12):
                return (
                    False,
                    new_error(1056, {"key_creation_date": key_creation.isoformat()}),
                    None,
                    None,
                )

    # Exports the public key again for uniform formatting.
    if import_pub_result.fingerprints:
        pub_key = gpg.export_keys(import_pub_result.fingerprints[0])

    # Try to import the encrypted private key (without passphrase)
    import_priv_result = gpg.import_keys(enc_priv_key)
    if not import_priv_result.sec_imported == 1:
        if (
            import_priv_result.results
            and "Not actually changed" not in import_priv_result.results[0]["text"]
        ):
            return (
                False,
                new_error(
                    1034,
                    {
                        "import_priv_result_stderr": str(import_priv_result.stderr),
                        "import_priv_result_returncode": str(
                            import_priv_result.returncode
                        ),
                        "import_priv_result_summary": str(import_priv_result.summary),
                        "import_priv_result_results": str(import_priv_result.results),
                        "import_priv_result_imported": str(import_priv_result.imported),
                    },
                ),
                None,
                None,
            )

    return True, None, pub_key, enc_priv_key


def verify_signed_message(pub_key, signed_message):
    """
    Verifies a signed cleartext PGP message. Returns whether the signature
    is valid (was made by the given pub_key) and the content of the message.
    """
    gpg = gnupg.GPG(gnupghome=config("GNUPG_DIR", default=None))

    # import the public key
    import_result = gpg.import_keys(pub_key)

    # verify the signed message
    verified = gpg.verify(signed_message)

    if verified.valid and verified.fingerprint == import_result.fingerprints[0]:
        header = "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
        footer = "-----BEGIN PGP SIGNATURE-----"
        cleartext_message = signed_message.split(header)[1].split(footer)[0].strip()

        return True, cleartext_message
    else:
        return False, None


def base91_to_hex(base91_str: str) -> str:
    bytes_data = decode(base91_str)
    return bytes_data.hex()


def hex_to_base91(hex_str: str) -> str:
    hex_bytes = bytes.fromhex(hex_str)
    base91_str = encode(hex_bytes)
    return base91_str


def is_valid_token(token: str) -> bool:
    num_chars = len(token)

    if not 38 < num_chars < 41:
        return False

    charset = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&()*+,./:;<=>?@[]^_`{|}~"'
    return all(c in charset for c in token)


def location_country(lon: float, lat: float) -> str:
    """
    Returns the country code of a lon/lat location
    """

    from shapely.geometry import shape, Point
    from shapely.prepared import prep

    # Load the GeoJSON data from a local file
    with open("frontend/static/assets/geo/countries-coastline-10km.geo.json") as f:
        countries_geojeson = json.load(f)

    # Prepare the countries for reverse geocoding
    countries = {}
    for feature in countries_geojeson["features"]:
        geom = feature["geometry"]
        country_code = feature["properties"]["A3"]
        countries[country_code] = prep(shape(geom))

    point = Point(lon, lat)
    for country_code, geom in countries.items():
        if geom.contains(point):
            return country_code

    return "unknown"


def render_order_logs(raw):
    """
    Renders Order.logs (a JSON-encoded string) as a safe HTML table.

    Safety: every field is html.escape()'d before any formatting is applied.
    The two formatting rules (**bold** and Object(ID,NAME)) operate on
    already-escaped text, so user data can never inject HTML.
    """
    # Decode JSON. Old rows contain HTML (not JSON), so this will fail
    # gracefully and we show "No logs were recorded".
    try:
        entries = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        entries = []

    if not entries:
        return "<b>No logs were recorded</b>"

    objects = ["LNPayment", "Robot", "Order", "OnchainPayment", "MarketTick"]
    rows = []

    for entry in entries:
        timestamp = html_lib.escape(str(entry.get("timestamp", "")))
        level = html_lib.escape(str(entry.get("level", "")))
        event = html_lib.escape(str(entry.get("event", "")))

        # **bold** -> <b>bold</b>
        event = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", event, flags=re.DOTALL)

        # *italic* -> <i>italic</i>
        event = re.sub(r"\*(.+?)\*", r"<i>\1</i>", event, flags=re.DOTALL)

        # Object(ID,NAME) -> hyperlink
        for obj in objects:
            event = re.sub(
                rf"{obj}\(([0-9a-fA-F\-]+),\s*([^)]+)\)",
                rf'<b><a href="/coordinator/api/{obj.lower()}/\1">\2</a></b>',
                event,
                flags=re.DOTALL,
            )

        rows.append(f"<tr><td>{timestamp}</td><td>{level}</td><td>{event}</td></tr>")

    header = (
        "<thead><tr><b><th>Timestamp</th><th>Level</th><th>Event</th></b></tr></thead>"
    )
    return f'<table style="width:100%">{header}{"".join(rows)}</table>'
