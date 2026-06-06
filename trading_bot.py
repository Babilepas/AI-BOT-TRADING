"""
trading_bot.py — Bitget USDT-M Perpetual Futures bot
Strategy  : RSI-7 with trend confirmation (EMA-100) for 1-minute scalping
             • BUY  when RSI crosses UP through 20 (oversold recovery) AND price > EMA-100
             • SELL when RSI crosses DOWN through 80 (overbought rejection) AND price < EMA-100
Risk mgmt : SL = 0.5% from entry  |  TP = 1% from entry  (RR 1:2)
Leverage  : x5, isolated margin, one-way mode
Symbol    : HYPE/USDT:USDT
Timeframe : 1 m

Requirements:
    pip install ccxt pandas ta python-dotenv

.env keys:
    BITGET_API_KEY, BITGET_SECRET, BITGET_PASSPHRASE
"""

import logging
import math
import os
import time
import csv
from datetime import datetime
from pathlib import Path

import ccxt
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

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

SYMBOL       = "HYPE/USDT:USDT"   # futures notation (BASE/QUOTE:SETTLE)
TIMEFRAME    = "1m"
LEVERAGE     = 3
MARGIN_MODE  = "isolated"

# Trade sizing — uses a fixed % of your available futures wallet balance.
# With a $3 account, TRADE_BALANCE_PCT = 1.0 means use 100% of available balance.
# Lower this (e.g. 0.5) to only risk 50% per trade and keep a buffer.
TRADE_BALANCE_PCT = 1.0            # fraction of available USDT balance to use (0.0 – 1.0)
USDT_AMOUNT_FALLBACK = 3.0        # fallback if balance fetch fails ($3 account)

# RSI strategy parameters
RSI_PERIOD    = 7
RSI_OVERSOLD  = 20                 # cross UP  → BUY
RSI_OVERBOUGHT = 80                # cross DOWN → SELL
EMA_TREND_PERIOD = 100             # trend filter: only buy above, only sell below

# Risk management — applied to price at entry
SL_PCT = 0.005  # 0.5 % stop-loss
TP_PCT = 0.01   # 1 % take-profit    → RR 1:2

# Volatility guard — prevents trading during extreme spikes
ATR_PERIOD = 14                    # Average True Range period
ATR_THRESHOLD = 0.02               # skip trades if ATR > 2% of price (adjust as needed)

POLL_INTERVAL_SEC = 60             # 1 min — matches candle timeframe
ERROR_SLEEP_SEC   = 60
CANDLE_LIMIT      = 250            # needs ≥ EMA_TREND_PERIOD + buffer

# Journal file for recording closed trades (profit / loss)
JOURNAL_PATH = Path.cwd() / "trade_journal.csv"
# ── exchange ──────────────────────────────────────────────────────────────────

exchange = ccxt.bitget({
    "apiKey":   API_KEY,
    "secret":   SECRET,
    "password": PASSPHRASE,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",                        # route to perpetual futures
        "createMarketBuyOrderRequiresPrice": False,
    },
})


def initialise_exchange() -> None:
    """Load markets and configure leverage + margin mode once at startup."""
    log.info("Loading markets …")
    exchange.load_markets()

    try:
        exchange.set_position_mode(hedged=False, symbol=SYMBOL)
        log.info("Position mode: one-way")
    except ccxt.BaseError as exc:
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


# ── balance ───────────────────────────────────────────────────────────────────

def get_available_balance() -> float:
    """
    Fetch the available (free) USDT balance from the futures wallet.
    Applies TRADE_BALANCE_PCT so you only risk a set fraction per trade.
    Falls back to USDT_AMOUNT_FALLBACK if the API call fails.
    """
    try:
        balance = exchange.fetch_balance(params={"type": "swap"})
        free_usdt = float(balance.get("USDT", {}).get("free") or 0)

        if free_usdt <= 0:
            log.warning("Free USDT balance is %.4f — using fallback %.2f", free_usdt, USDT_AMOUNT_FALLBACK)
            return USDT_AMOUNT_FALLBACK

        trade_amount = round(free_usdt * TRADE_BALANCE_PCT, 4)
        log.info(
            "Wallet: %.4f USDT free | using %.0f%% → %.4f USDT for this trade",
            free_usdt, TRADE_BALANCE_PCT * 100, trade_amount,
        )
        return trade_amount

    except ccxt.BaseError as exc:
        log.warning("fetch_balance failed (%s) — using fallback %.2f USDT", exc, USDT_AMOUNT_FALLBACK)
        return USDT_AMOUNT_FALLBACK


# ── signal ────────────────────────────────────────────────────────────────────

def get_volatility(symbol: str) -> float:
    """
    Calculate Average True Range (ATR) as a percentage of current price.
    Returns ATR / price to compare against ATR_THRESHOLD.
    High volatility may indicate risky conditions for scalping.
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=ATR_PERIOD + 5)
        if len(ohlcv) < ATR_PERIOD:
            return 0.0
        
        high  = [x[2] for x in ohlcv]
        low   = [x[3] for x in ohlcv]
        close = [x[4] for x in ohlcv]
        
        # Calculate True Range
        tr_values = []
        for i in range(1, len(ohlcv)):
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i] - close[i-1])
            )
            tr_values.append(tr)
        
        # Average True Range
        atr = sum(tr_values[-ATR_PERIOD:]) / min(ATR_PERIOD, len(tr_values))
        atr_pct = atr / close[-1] if close[-1] > 0 else 0.0
        
        log.info("ATR: %.4f USDT (%.2f%% of price)", atr, atr_pct * 100)
        return atr_pct
    
    except Exception as exc:
        log.warning("get_volatility failed: %s", exc)
        return 0.0


def get_signal() -> tuple[str, float]:
    """
    RSI-7 crossover strategy with EMA-100 trend filter + volatility guard.

    Returns (signal, last_close) where signal ∈ {"BUY", "SELL", "HOLD"}.

    Entry rules:
      BUY  — RSI crosses ABOVE 20 (prev < 20, current >= 20) AND close > EMA-100 AND ATR is safe
      SELL — RSI crosses BELOW 80 (prev > 80, current <= 80) AND close < EMA-100 AND ATR is safe
    """
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)

    if len(ohlcv) < EMA_TREND_PERIOD + 10:
        log.warning("Not enough candles (%d) for EMA-%d — HOLD", len(ohlcv), EMA_TREND_PERIOD)
        return "HOLD", 0.0

    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    close = df["close"]

    rsi      = RSIIndicator(close, window=RSI_PERIOD).rsi()
    ema_trend = EMAIndicator(close, window=EMA_TREND_PERIOD).ema_indicator()

    last_close    = close.iloc[-1]
    prev_rsi      = rsi.iloc[-2]
    curr_rsi      = rsi.iloc[-1]
    trend_ema     = ema_trend.iloc[-1]

    log.info(
        "RSI prev=%.2f curr=%.2f | EMA-%d=%.4f | close=%.4f",
        prev_rsi, curr_rsi, EMA_TREND_PERIOD, trend_ema, last_close,
    )

    # Check volatility — safety guard for scalping
    volatility = get_volatility(SYMBOL)
    if volatility > ATR_THRESHOLD:
        log.warning(
            "Volatility too high (ATR %.2f%% > threshold %.2f%%) — HOLD",
            volatility * 100, ATR_THRESHOLD * 100
        )
        return "HOLD", last_close

    # RSI cross UP through oversold + price above trend EMA → BUY
    if prev_rsi < RSI_OVERSOLD and curr_rsi >= RSI_OVERSOLD and last_close > trend_ema:
        return "BUY", last_close

    # RSI cross DOWN through overbought + price below trend EMA → SELL
    if prev_rsi > RSI_OVERBOUGHT and curr_rsi <= RSI_OVERBOUGHT and last_close < trend_ema:
        return "SELL", last_close

    return "HOLD", last_close


# ── position helpers ──────────────────────────────────────────────────────────

def get_open_position() -> dict | None:
    """Return the current open position for SYMBOL, or None if flat."""
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if float(pos.get("contracts") or 0) > 0:
                return pos
    except ccxt.BaseError as exc:
        log.warning("fetch_positions failed: %s", exc)
    return None


# ── trade amount ──────────────────────────────────────────────────────────────

def get_trade_amount(symbol: str, usdt_collateral: float) -> float:
    """
    Convert USDT collateral → contract quantity, applying leverage.
    notional = usdt_collateral * LEVERAGE
    contracts = notional / price  (floored to step size)
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

    log.info(
        "Sizing: collateral=%.4f USDT × x%d = %.4f notional / price=%.4f → %.6f contracts (raw)",
        usdt_collateral, LEVERAGE, notional, price, amount,
    )

    # Floor to exchange step size
    if precision is not None:
        step = (1 / (10 ** precision)) if isinstance(precision, int) else (precision if precision > 0 else 1)
        amount = math.floor(amount / step) * step

    # ── min_cost check ────────────────────────────────────────────────────────
    # min_cost is the minimum NOTIONAL value (not collateral).
    # With leverage, our notional = collateral × LEVERAGE, so $3 × x5 = $15
    # which comfortably clears a $5 min_cost requirement.
    if min_cost is not None and notional < min_cost:
        raise ValueError(
            f"Notional {notional:.2f} USDT (collateral {usdt_collateral} × x{LEVERAGE}) "
            f"is below the exchange minimum notional {min_cost} USDT for {symbol}. "
            f"You need at least {min_cost / LEVERAGE:.2f} USDT in collateral."
        )

    # ── min_amount check ──────────────────────────────────────────────────────
    if min_amount is not None and amount < min_amount:
        required_collateral = (min_amount * price) / LEVERAGE
        if required_collateral <= usdt_collateral:
            amount = min_amount
            log.info("Amount bumped to min_amount: %.6f", min_amount)
        else:
            raise ValueError(
                f"Collateral {usdt_collateral:.4f} USDT (x{LEVERAGE} → {notional:.2f} notional) "
                f"is insufficient for min amount {min_amount} at price {price:.4f}. "
                f"Need at least {required_collateral:.4f} USDT collateral."
            )

    if amount <= 0:
        raise ValueError(f"Calculated trade amount is zero or negative ({amount}).")

    return amount


# ── SL / TP helpers ───────────────────────────────────────────────────────────

def calc_sl_tp(side: str, entry_price: float) -> tuple[float, float]:
    """
    Calculate stop-loss and take-profit prices.
    SL = 1% adverse  |  TP = 2% favourable  → RR 1:2

    Returns (sl_price, tp_price) rounded to 6 decimal places.
    """
    if side.upper() == "BUY":
        sl_price = round(entry_price * (1 - SL_PCT), 6)
        tp_price = round(entry_price * (1 + TP_PCT), 6)
    else:  # SELL / short
        sl_price = round(entry_price * (1 + SL_PCT), 6)
        tp_price = round(entry_price * (1 - TP_PCT), 6)
    return sl_price, tp_price


def place_tpsl_orders(side: str, contracts: float, entry_price: float) -> None:
    """
    Place separate TP and SL plan orders via Bitget's TPSL endpoint.
    Uses the Bitget V2 REST API directly because ccxt's unified
    create_order merges both into one call which can be fragile.

    planType:
      pos_profit → closes the WHOLE position when TP hit
      pos_loss   → closes the WHOLE position when SL hit
    """
    sl_price, tp_price = calc_sl_tp(side, entry_price)
    hold_side = "long" if side.upper() == "BUY" else "short"

    log.info(
        "Setting SL=%.6f  TP=%.6f  (entry=%.6f, RR 1:2)",
        sl_price, tp_price, entry_price,
    )

    base_params = {
        "marginCoin":  "USDT",
        "productType": "usdt-futures",
        "symbol":      SYMBOL.replace("/", "").replace(":USDT", "").upper(),  # e.g. HYPEUSDT
        "triggerType": "mark_price",
        "executePrice": "0",                  # market execution on trigger
        "holdSide":    hold_side,
        "size":        str(contracts),
    }

    for plan_type, trigger in [("pos_profit", tp_price), ("pos_loss", sl_price)]:
        params = {**base_params, "planType": plan_type, "triggerPrice": str(trigger)}
        try:
            resp = exchange.private_post_api_v2_mix_order_place_tpsl_order(params)
            label = "TP" if plan_type == "pos_profit" else "SL"
            log.info("%s order placed: %s", label, resp.get("data", {}).get("orderId", "?"))
        except ccxt.BaseError as exc:
            label = "TP" if plan_type == "pos_profit" else "SL"
            log.error("Failed to place %s order: %s", label, exc)


# ── order placement ───────────────────────────────────────────────────────────

def close_position(position: dict) -> None:
    """Close an existing position with a reduce-only market order."""
    pos_side  = position.get("side", "").lower()
    contracts = float(position.get("contracts", 0))

    if contracts <= 0:
        return

    close_side = "sell" if pos_side == "long" else "buy"
    log.info("Closing %s → %s %.6f contracts", pos_side.upper(), close_side.upper(), contracts)

    try:
        order = exchange.create_market_order(
            SYMBOL, close_side, contracts,
            params={"reduceOnly": True, "marginMode": MARGIN_MODE},
        )
        log.info("Close order filled: %s", order.get("id"))
        # Try to determine close price from order, fallback to ticker
        close_price = None
        for key in ("average", "avgPrice", "filledPrice"):
            close_price = order.get(key) or close_price
        if not close_price:
            try:
                close_price = float(exchange.fetch_ticker(SYMBOL)["last"])
            except Exception:
                close_price = 0.0

        # Determine entry price from position info (be generous with possible keys)
        entry_price = None
        for k in ("entryPrice", "entry_price", "avgEntryPrice", "avg_entry_price"):
            entry_price = (position.get(k) or (position.get("info") or {}).get(k)) or entry_price
        try:
            entry_price = float(entry_price) if entry_price is not None else 0.0
        except Exception:
            entry_price = 0.0

        # Compute simple profit metric (percentage) based on entry/close prices
        pct = 0.0
        profit = False
        if entry_price > 0 and close_price > 0:
            if pos_side == "long":
                pct = (close_price - entry_price) / entry_price
            else:
                pct = (entry_price - close_price) / entry_price
            profit = pct > 0

        # Append to CSV journal
        try:
            header = [
                "timestamp", "symbol", "side", "entry_price", "close_price",
                "contracts", "pnl_pct", "profit", "order_id"
            ]
            row = [
                datetime.utcnow().isoformat(), SYMBOL, pos_side.upper(),
                f"{entry_price:.8f}", f"{close_price:.8f}", f"{contracts:.8f}",
                f"{pct:.8f}", str(bool(profit)), order.get("id")
            ]
            write_header = not JOURNAL_PATH.exists()
            with JOURNAL_PATH.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if write_header:
                    writer.writerow(header)
                writer.writerow(row)
            log.info("Trade journal updated: %s", JOURNAL_PATH)
        except Exception as exc:
            log.warning("Failed to write trade journal: %s", exc)
    except ccxt.BaseError as exc:
        log.error("Failed to close position: %s", exc)
        raise


def open_position(side: str, entry_price: float) -> None:
    """
    Open a new position (BUY=long / SELL=short) and immediately
    attach SL (1 %) and TP (2 %) plan orders.
    Trade size is derived from the live futures wallet balance.
    """
    side = side.upper()

    usdt_collateral = get_available_balance()

    try:
        amount = get_trade_amount(SYMBOL, usdt_collateral)
    except ValueError as exc:
        log.error("Order skipped — %s", exc)
        return

    log.info(
        "Opening %s | collateral=%.4f USDT | %.6f contracts | x%d leverage | entry≈%.4f",
        side, usdt_collateral, amount, LEVERAGE, entry_price,
    )

    try:
        order = exchange.create_market_order(
            SYMBOL, side.lower(), amount,
            params={"marginMode": MARGIN_MODE},
        )
        log.info("%s order placed — id: %s", side, order.get("id"))
    except ccxt.BaseError as exc:
        log.error("Error placing %s order: %s", side, exc)
        raise

    # Give the exchange a moment to register the position, then attach SL/TP
    time.sleep(1)
    place_tpsl_orders(side, amount, entry_price)


def place_order(signal: str, entry_price: float) -> None:
    """
    Main order routing:
    • Flat            → open in signal direction + SL/TP (if volatility is safe)
    • Same direction  → hold (avoid stacking)
    • Opposite        → close existing, open opposite + SL/TP (if volatility is safe)
    """
    signal = signal.upper()
    if signal not in ("BUY", "SELL"):
        return
    
    # Double-check volatility before executing any trade
    volatility = get_volatility(SYMBOL)
    if volatility > ATR_THRESHOLD:
        log.warning(
            "Trade rejected — volatility too high (ATR %.2f%% > threshold %.2f%%)",
            volatility * 100, ATR_THRESHOLD * 100
        )
        return

    position = get_open_position()

    if position is None:
        open_position(signal, entry_price)
        return

    pos_side    = position.get("side", "").lower()   # 'long' or 'short'
    signal_side = "long" if signal == "BUY" else "short"

    if pos_side == signal_side:
        log.info("Already %s — holding.", pos_side.upper())
        return

    log.info("Flipping %s → %s", pos_side.upper(), signal_side.upper())
    close_position(position)
    time.sleep(1)
    open_position(signal, entry_price)


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    initialise_exchange()
    log.info(
        "Bot started — %s | %s | x%d | trade=%.0f%% of balance | SL %.0f%% | TP %.0f%% | RR 1:%.0f",
        SYMBOL, TIMEFRAME, LEVERAGE,
        TRADE_BALANCE_PCT * 100, SL_PCT * 100, TP_PCT * 100, TP_PCT / SL_PCT,
    )

    while True:
        try:
            signal, last_price = get_signal()
            log.info("Signal: %s  (last_close=%.4f)", signal, last_price)

            if signal in ("BUY", "SELL"):
                place_order(signal, last_price)
            else:
                log.info("HOLD — no action.")

            time.sleep(POLL_INTERVAL_SEC)

        except ccxt.AuthenticationError as exc:
            log.critical("Authentication failed — check API keys: %s", exc)
            break

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
