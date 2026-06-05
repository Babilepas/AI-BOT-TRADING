"""
trading_bot.py — Bitget USDT-M Perpetual Futures bot (EMA-9 / EMA-21 crossover)
Leverage  : x5, isolated margin, one-way mode
Symbol    : HYPE/USDT:USDT   (futures notation required for swap markets)
Timeframe : 5 m
Position  : size is based on USDT_AMOUNT * LEVERAGE notional value

Requirements:
    pip install ccxt pandas ta python-dotenv

.env keys expected:
    BITGET_API_KEY, BITGET_SECRET, BITGET_PASSPHRASE
"""

import logging
import math
import os
import time

import ccxt
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────

load_dotenv()

API_KEY    = os.getenv("BITGET_API_KEY")
SECRET     = os.getenv("BITGET_SECRET")
PASSPHRASE = os.getenv("BITGET_PASSPHRASE")

# Futures symbol uses the  BASE/QUOTE:SETTLE  notation in ccxt
SYMBOL     = "HYPE/USDT:USDT"
TIMEFRAME  = "5m"
LEVERAGE   = 5
MARGIN_MODE = "isolated"   # "isolated" or "cross"
USDT_AMOUNT = 3           # collateral per trade (leverage applied on top)

POLL_INTERVAL_SEC  = 300   # 5 minutes — matches candle timeframe
ERROR_SLEEP_SEC    = 60

# ── exchange ──────────────────────────────────────────────────────────────────

exchange = ccxt.bitget({
    "apiKey":   API_KEY,
    "secret":   SECRET,
    "password": PASSPHRASE,
    "enableRateLimit": True,
    "options": {
        # Route all calls to the swap (perpetual futures) endpoint
        "defaultType": "swap",
        # Bitget V2 API — keep ccxt's default; listed here for explicitness
        "createMarketBuyOrderRequiresPrice": False,
    },
})


def initialise_exchange() -> None:
    """Load markets and configure leverage + margin mode once at startup."""
    log.info("Loading markets …")
    exchange.load_markets()

    # One-way mode must be set in your Bitget account settings (web/app).
    # Programmatically we still set_position_mode to make sure ccxt aligns.
    try:
        # hedged=False → one-way mode
        exchange.set_position_mode(hedged=False, symbol=SYMBOL)
        log.info("Position mode: one-way")
    except ccxt.BaseError as exc:
        # Bitget raises if you try to switch while a position is open;
        # if it's already correct, that's fine too.
        log.warning("set_position_mode: %s (continuing)", exc)

    try:
        exchange.set_margin_mode(MARGIN_MODE, SYMBOL)
        log.info("Margin mode: %s", MARGIN_MODE)
    except ccxt.BaseError as exc:
        log.warning("set_margin_mode: %s (continuing)", exc)

    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
        log.info("Leverage: x%d", LEVERAGE)
    except ccxt.BaseError as exc:
        log.warning("set_leverage: %s (continuing)", exc)


# ── signal ────────────────────────────────────────────────────────────────────

def get_signal() -> str:
    """
    Fetch the last 100 candles and return 'BUY', 'SELL', or 'HOLD'
    based on EMA-9 / EMA-21 crossover on the closing price.
    """
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)

    if not ohlcv:
        log.warning("fetch_ohlcv returned empty data")
        return "HOLD"

    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    close = df["close"]
    ema_fast = EMAIndicator(close, window=9).ema_indicator()
    ema_slow = EMAIndicator(close, window=21).ema_indicator()

    last_fast = ema_fast.iloc[-1]
    last_slow = ema_slow.iloc[-1]

    log.info("EMA-9 = %.6f  |  EMA-21 = %.6f", last_fast, last_slow)

    if last_fast > last_slow:
        return "BUY"
    if last_fast < last_slow:
        return "SELL"
    return "HOLD"


# ── position helpers ──────────────────────────────────────────────────────────

def get_open_position() -> dict | None:
    """
    Return the current open position for SYMBOL, or None if flat.
    Handles both list and dict responses from fetch_positions.
    """
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            contracts = float(pos.get("contracts") or 0)
            if contracts > 0:
                return pos
    except ccxt.BaseError as exc:
        log.warning("fetch_positions failed: %s", exc)
    return None


# ── trade amount ──────────────────────────────────────────────────────────────

def get_trade_amount(symbol: str, usdt_collateral: float) -> float:
    """
    Convert a USDT collateral amount into a contract quantity, respecting
    exchange precision rules.

    notional = usdt_collateral * LEVERAGE
    contracts = notional / current_price  (then floored to step size)
    """
    ticker = exchange.fetch_ticker(symbol)
    price  = ticker["last"]
    if not price or price <= 0:
        raise ValueError(f"Invalid price from ticker: {price}")

    market     = exchange.market(symbol)
    precision  = market.get("precision", {}).get("amount")
    min_amount = market.get("limits", {}).get("amount", {}).get("min")
    min_cost   = market.get("limits", {}).get("cost",   {}).get("min")

    notional = usdt_collateral * LEVERAGE
    amount   = notional / price

    # Floor to exchange step size
    if precision is not None:
        if isinstance(precision, int):
            step = 1 / (10 ** precision)
        else:
            # precision already expressed as a float step (e.g. 0.001)
            step = precision if precision > 0 else 1
        amount = math.floor(amount / step) * step

    # Min-cost check (on raw collateral, not notional)
    if min_cost is not None and usdt_collateral < min_cost:
        raise ValueError(
            f"Collateral {usdt_collateral} USDT is below the minimum cost "
            f"{min_cost} for {symbol}."
        )

    # Min-amount check
    if min_amount is not None and amount < min_amount:
        required_collateral = (min_amount * price) / LEVERAGE
        if required_collateral <= usdt_collateral:
            amount = min_amount
        else:
            raise ValueError(
                f"Collateral {usdt_collateral} USDT (x{LEVERAGE} → {notional:.2f} "
                f"USDT notional) is not enough to buy the minimum amount "
                f"{min_amount} for {symbol} at price {price:.4f}."
            )

    if amount <= 0:
        raise ValueError(
            f"Calculated trade amount is zero or negative ({amount}). "
            f"Increase USDT_AMOUNT or reduce LEVERAGE."
        )

    return amount


# ── order placement ───────────────────────────────────────────────────────────

def close_position(position: dict) -> None:
    """
    Close an existing position with a reduce-only market order.
    side = opposite of the current position side.
    """
    pos_side = position.get("side", "").lower()   # 'long' or 'short'
    contracts = float(position.get("contracts", 0))

    if contracts <= 0:
        return

    close_side = "sell" if pos_side == "long" else "buy"
    log.info(
        "Closing %s position: %s %.6f contracts",
        pos_side.upper(), close_side.upper(), contracts,
    )

    try:
        order = exchange.create_market_order(
            SYMBOL,
            close_side,
            contracts,
            params={
                "reduceOnly": True,
                "marginMode": MARGIN_MODE,
            },
        )
        log.info("Close order filled: %s", order.get("id"))
    except ccxt.BaseError as exc:
        log.error("Failed to close position: %s", exc)
        raise


def open_position(side: str) -> None:
    """
    Open a new position (BUY = long, SELL = short).
    Applies leverage via USDT_AMOUNT * LEVERAGE notional.
    """
    side = side.upper()
    try:
        amount = get_trade_amount(SYMBOL, USDT_AMOUNT)
    except ValueError as exc:
        log.error("Order skipped — %s", exc)
        return

    log.info(
        "Opening %s | %.6f contracts | x%d leverage | ~%.2f USDT notional",
        side, amount, LEVERAGE, amount * exchange.fetch_ticker(SYMBOL)["last"],
    )

    try:
        order = exchange.create_market_order(
            SYMBOL,
            side.lower(),
            amount,
            params={"marginMode": MARGIN_MODE},
        )
        log.info("%s order placed — id: %s", side, order.get("id"))
    except ccxt.BaseError as exc:
        log.error("Error placing %s order: %s", side, exc)
        raise


def place_order(signal: str) -> None:
    """
    Main order logic:
    - If flat: open in signal direction.
    - If already in the same direction: do nothing (hold).
    - If in the opposite direction: close first, then open in new direction.
    """
    signal = signal.upper()
    if signal not in ("BUY", "SELL"):
        return

    position = get_open_position()

    if position is None:
        # No open position — open fresh
        open_position(signal)
        return

    pos_side = position.get("side", "").lower()  # 'long' or 'short'
    signal_side = "long" if signal == "BUY" else "short"

    if pos_side == signal_side:
        log.info("Already %s — holding, no new order.", pos_side.upper())
        return

    # Flip: close current then open opposite
    log.info("Flipping from %s → %s", pos_side.upper(), signal_side.upper())
    close_position(position)
    time.sleep(1)   # brief pause to let the close settle
    open_position(signal)


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    initialise_exchange()

    log.info(
        "Bot started — symbol=%s  tf=%s  leverage=x%d  collateral=%s USDT  margin=%s",
        SYMBOL, TIMEFRAME, LEVERAGE, USDT_AMOUNT, MARGIN_MODE,
    )

    while True:
        try:
            signal = get_signal()
            log.info("Signal: %s", signal)

            if signal in ("BUY", "SELL"):
                place_order(signal)
            else:
                log.info("HOLD — no action.")

            time.sleep(POLL_INTERVAL_SEC)

        except ccxt.AuthenticationError as exc:
            log.critical("Authentication failed — check your API keys: %s", exc)
            break                          # fatal: no point retrying

        except ccxt.InsufficientFunds as exc:
            log.error("Insufficient funds: %s", exc)
            time.sleep(ERROR_SLEEP_SEC)

        except ccxt.NetworkError as exc:
            log.warning("Network error (will retry): %s", exc)
            time.sleep(ERROR_SLEEP_SEC)

        except ccxt.BaseError as exc:
            log.error("Exchange error: %s", exc)
            time.sleep(ERROR_SLEEP_SEC)

        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    main()
