# HYPE/USDT Futures Trading Bot — Bitget

Automated perpetual futures bot running on Bitget with a **RSI-14 + EMA-200** strategy, x5 leverage, and fixed **1% SL / 2% TP** on every trade.

---

## Strategy

| | Rule |
|---|---|
| **Indicator** | RSI-14 with EMA-200 trend filter |
| **BUY signal** | RSI crosses UP through 30 (oversold exit) AND price is above EMA-200 |
| **SELL signal** | RSI crosses DOWN through 70 (overbought exit) AND price is below EMA-200 |
| **Timeframe** | 5 minutes |
| **Leverage** | x5 isolated margin |
| **Stop-Loss** | 1% from entry |
| **Take-Profit** | 2% from entry |
| **Risk/Reward** | 1:2 |

---

## Requirements

- Python 3.11+
- Bitget account with Futures enabled
- API key with **Futures trading** permission

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Mac / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install ccxt pandas ta python-dotenv
```

### 4. Set up your API keys

```bash
cp .env.example .env
```

Open `.env` and fill in your Bitget credentials:

```env
BITGET_API_KEY=your_api_key_here
BITGET_SECRET=your_secret_here
BITGET_PASSPHRASE=your_passphrase_here
```

> **How to get your Bitget API keys:**
> 1. Log in to Bitget → Profile → API Management
> 2. Click **Create API**
> 3. Enable **Futures Trading** permission
> 4. Save your Key, Secret, and Passphrase

### 5. Set Bitget to One-Way Mode (required)

> In your Bitget app or website:
> **Futures → Settings → Position Mode → One-Way Mode**

This must be done before running the bot or orders will be rejected.

### 6. Run the bot

```bash
python trading_bot.py
```

---

## Configuration

All settings are at the top of `trading_bot.py`:

```python
SYMBOL       = "HYPE/USDT:USDT"   # trading pair
TIMEFRAME    = "5m"                # candle timeframe
LEVERAGE     = 5                   # futures leverage
MARGIN_MODE  = "isolated"          # isolated or cross
USDT_AMOUNT  = 20                  # collateral per trade (USDT)

RSI_PERIOD       = 14              # RSI window
RSI_OVERSOLD     = 30              # BUY threshold
RSI_OVERBOUGHT   = 70              # SELL threshold
EMA_TREND_PERIOD = 200             # trend filter period

SL_PCT = 0.01                      # stop-loss  1%
TP_PCT = 0.02                      # take-profit 2%
```

---

## Running Tests

```bash
pip install pytest
pytest test_trading_bot.py -v
```

---

## Project Structure

```
├── trading_bot.py        # main bot
├── test_trading_bot.py   # unit tests
├── .env                  # your real API keys (never commit this)
├── .env.example          # key template (safe to commit)
├── .gitignore            # keeps .env out of git
└── README.md             # this file
```

---

## How the Bot Works

```
Every 5 minutes:
  1. Fetch last 250 candles from Bitget
  2. Calculate RSI-14 and EMA-200
  3. Check for RSI crossover signal
     ├── BUY  → open long  + set SL/TP
     ├── SELL → open short + set SL/TP
     └── HOLD → do nothing
  4. If already in a position:
     ├── Same direction → hold, skip
     └── Opposite       → close first, then open new
```

---

## Risk Warning

> This bot trades real money with leverage. Leveraged futures trading carries significant risk of loss. Always test with a small amount first. Never trade more than you can afford to lose. Past performance of any strategy does not guarantee future results.

---

## License

MIT
