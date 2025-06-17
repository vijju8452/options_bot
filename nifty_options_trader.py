import pandas as pd
import numpy as np
import datetime
from zoneinfo import ZoneInfo

import time
import json
import http.client
from angel_login import smartapi_login
from zoneinfo import ZoneInfo
import os
import requests

# ---------------------------- CONFIGURATION ----------------------------
SIMULATION_MODE = True
SIMULATION_START = "2025-06-13"
SIMULATION_END = "2025-06-13"
INDEXTOKEN = 99926000  # NIFTY
TICKSTEP = 50
LOT = 75
TOKEN_CACHE = {}
OPEN_TRADE = None
TRADE_LOG = []
HEADERS = ""
SCRIPTMASTER = None
STARTING_CAPITAL = 10000
capital = STARTING_CAPITAL
pending_profits = 0



# ---------------------------- UTILITY FUNCTIONS ----------------------------
def round_to_tick(price, tick_size=0.05):
    return round(round(price / tick_size) * tick_size, 2)
def get_symbol_token(symbol):
    
    if SCRIPTMASTER is None:
        print("[❌] SCRIPTMASTER is not loaded.")
        return None

    row = SCRIPTMASTER[SCRIPTMASTER["symbol"] == symbol.upper()]
    if row.empty:
        print(f"[❌] Token not found for {symbol}")
        return None
    return str(row.iloc[0]["token"])



def fetch_historical_data(symbol_token, interval="ONE_MINUTE", exchange="NSE", scan_time=None):
    if scan_time is None:
        scan_time = datetime.datetime.now()
    
    from_date = (scan_time - datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    to_date = scan_time.strftime("%Y-%m-%d %H:%M")

    payload = json.dumps({
        "exchange": exchange,
        "symboltoken": symbol_token,
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date
    })
    
    conn = http.client.HTTPSConnection("apiconnect.angelone.in")
    time.sleep(0.5)
    conn.request("POST", "/rest/secure/angelbroking/historical/v1/getCandleData", payload, HEADERS)
    res = conn.getresponse()
    data = json.loads(res.read().decode("utf-8"))

    if data.get("status"):
        df = pd.DataFrame(data["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df[df["timestamp"] <= scan_time].dropna()
    return None

# ---------------------------- INDICATORS ----------------------------
def calculate_rsi(data, period=14):
    delta = data["close"].diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(window=period).mean()
    avg_loss = pd.Series(loss).rolling(window=period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_adx(data, period=14):
    df = data.copy()
    df["TR"] = np.maximum(df["high"] - df["low"], np.maximum(abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1))))
    df["+DM"] = np.where((df["high"] - df["high"].shift(1)) > (df["low"].shift(1) - df["low"]), df["high"] - df["high"].shift(1), 0)
    df["-DM"] = np.where((df["low"].shift(1) - df["low"]) > (df["high"] - df["high"].shift(1)), df["low"].shift(1) - df["low"], 0)
    df["ATR"] = df["TR"].rolling(window=period).mean()
    df["+DI"] = (df["+DM"].rolling(window=period).mean() / df["ATR"]) * 100
    df["-DI"] = (df["-DM"].rolling(window=period).mean() / df["ATR"]) * 100
    df["DX"] = (abs(df["+DI"] - df["-DI"]) / (df["+DI"] + df["-DI"])) * 100
    df["ADX"] = df["DX"].rolling(window=period).mean()
    return df["ADX"]

def calculate_atr(data, period=14):
    data["high-low"] = data["high"] - data["low"]
    data["high-prev_close"] = abs(data["high"] - data["close"].shift(1))
    data["low-prev_close"] = abs(data["low"] - data["close"].shift(1))
    data["true_range"] = data[["high-low", "high-prev_close", "low-prev_close"]].max(axis=1)
    data["ATR"] = data["true_range"].rolling(window=period).mean()
    return round(data["ATR"].iloc[-1], 2)

# ---------------------------- STRATEGY: SPOT BREAKOUT ----------------------------
def check_spot_breakout(scan_time):
    spot_data = fetch_historical_data(INDEXTOKEN, interval="FIFTEEN_MINUTE", exchange="NSE", scan_time=scan_time)
    if spot_data is None or len(spot_data) < 3:
        return None
    prev_high = spot_data.iloc[-3]["high"]
    prev_low = spot_data.iloc[-3]["low"]
    current_close = spot_data.iloc[-1]["close"]

    if current_close > prev_high:
        return "CE"
    elif current_close < prev_low:
        return "PE"
    return None
def get_next_expiry(scan_time):
    """
    Returns next weekly expiry date in Angel format (e.g., 20JUN for June 20th).
    Assumes expiry is on Thursday, skips holidays.
    """
    weekday = scan_time.weekday()  # Monday=0 ... Sunday=6
    days_until_thursday = (3 - weekday) % 7  # 3 = Thursday
    expiry_date = scan_time + datetime.timedelta(days=days_until_thursday)
    return expiry_date.strftime('%d%b').upper()  # Format like 20JUN

def simulate_trade_entry(trend, spot_price, scan_time):
    global OPEN_TRADE, capital

    atm_strike = round(spot_price / TICKSTEP) * TICKSTEP
    expiry_str = get_next_expiry(scan_time)

    max_checks = 10
    checks = 0

    while checks < max_checks:
        option_symbol = f"NIFTY{expiry_str}25{atm_strike}{trend}"
        token = get_symbol_token(option_symbol)
        if token is None:
            print(f"[❌] Token not found for {option_symbol}, skipping...")
            return

        option_data = fetch_historical_data(token, interval="FIVE_MINUTE", exchange="NFO", scan_time=scan_time)
        if option_data is None or option_data.empty:
            print(f"[❌] No data for {option_symbol}, trying next strike...")
            return

        ltp = option_data.iloc[-1]["close"]
        cost = ltp * LOT

        if cost <= capital:
            atr = calculate_atr(option_data)
            sl = ltp - (1.0 * atr)
            tp = ltp + (3.0 * atr)

            OPEN_TRADE = {
                "entry_time": scan_time,
                "symbol": option_symbol,
                "entry_price": ltp,
                "sl": sl,
                "tp": tp,
                "token": token,
                "direction": trend,
                "tsl": sl,  # initial trailing SL
                "max_price": ltp,  # start max/min tracker
                "atr": atr  # save ATR if needed for dynamic TSL
            }

            print(f"[✅] Entered Trade: {option_symbol} at {ltp}, SL={sl}, TP={tp}")
            return
        else:
            print(f"[💸] ₹{cost:.2f} exceeds capital ₹{capital:.2f}. Checking next strike...")
            atm_strike += TICKSTEP if trend == "CE" else -TICKSTEP
            checks += 1

    print("[❌] Could not find affordable option within 10 checks.")



def monitor_trade_exit(scan_time):
    global OPEN_TRADE, TRADE_LOG, capital, pending_profits

    if not OPEN_TRADE:
        return None

    option_data = fetch_historical_data(OPEN_TRADE["token"], interval="ONE_MINUTE", exchange="NFO", scan_time=scan_time)
    if option_data is None or option_data.empty:
        return None

    price = option_data.iloc[-1]["close"]
    entry = OPEN_TRADE["entry_price"]
    direction = OPEN_TRADE["direction"]
    qty = LOT

    if price <= OPEN_TRADE["sl"]:
        result = "SL"
    elif price >= OPEN_TRADE["tp"]:
        result = "TP"
    elif (scan_time - OPEN_TRADE["entry_time"]).seconds > 60 * 60:
        result = "TIMEOUT"
    else:
        pnl = (price - entry) * qty
        return round(pnl, 2)

    pnl = (price - entry) * qty

    if pnl < 0:
        capital -= abs(pnl)
        print(f"[💸 LOSS] -₹{abs(pnl):.2f} | Capital now: ₹{capital:.2f}")
    else:
        needed_to_recover = 10000 - capital
        if pnl <= needed_to_recover:
            capital += pnl
            print(f"[↩️ RECOVERING LOSS] +₹{pnl:.2f} → Capital restored to ₹{capital:.2f}")
        else:
            capital = 10000
            leftover = pnl - needed_to_recover
            pending_profits += leftover
            print(f"[💹 PROFIT] Recovered loss, ₹{leftover:.2f} pending | Capital: ₹{capital:.2f}")

    print(f"[🔁] Exit: {OPEN_TRADE['symbol']} | Price: {price} | Reason: {result}")

    OPEN_TRADE["exit_price"] = price
    OPEN_TRADE["exit_time"] = scan_time
    OPEN_TRADE["exit_reason"] = result
    OPEN_TRADE["pnl"] = pnl
    TRADE_LOG.append(OPEN_TRADE)
    OPEN_TRADE = None
    return 0




# ---------------------------- SIMULATION LOOP ----------------------------
def run_simulation(start_date, end_date):
    global capital, pending_profits  # <-- required to modify globals

    all_dates = pd.date_range(start=start_date, end=end_date, freq='B')

    for date in all_dates:
        current_time = datetime.datetime.strptime(f"{date.date()} 09:45", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        end_time = datetime.datetime.strptime(f"{date.date()} 15:10", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Kolkata"))

        while current_time <= end_time:
            print(f"[🔁] Simulating {current_time}")

            if OPEN_TRADE:
                pnl = monitor_trade_exit(current_time)
                if pnl is not None:
                    print(f"[📈] Unrealized PnL as of {current_time}: ₹{pnl}")
            else:
                trend = check_spot_breakout(current_time)
                if trend:
                    spot_data = fetch_historical_data(INDEXTOKEN, "ONE_MINUTE", "NSE", current_time)
                    if spot_data is not None and not spot_data.empty:
                        spot_price = spot_data.iloc[-1]["close"]
                        simulate_trade_entry(trend, spot_price, current_time)

            current_time += datetime.timedelta(minutes=1)

        # ✅ Day-end capital update block (inside for-loop)
        print(f"[🏦 EOD] Adding ₹{pending_profits:.2f} to capital.")
        capital += pending_profits
        pending_profits = 0
        print(f"[📊] Capital after {date.date()}: ₹{capital:.2f}")



def load_scripmaster_daily(sim_time=None):
    
    # Ensure 'data' folder exists
    os.makedirs("data", exist_ok=True)

    # Construct file path inside 'data' directory
    today_str = (sim_time or datetime.datetime.now()).strftime("%Y-%m-%d")

    
    filename = os.path.join("data", f"OpenAPIScripMaster_{today_str}.json")

    # Load or download the ScripMaster file
    if os.path.exists(filename):
        print(f"📂 Using cached ScripMaster: {filename}")
        with open(filename, "r") as f:
            data = json.load(f)
    else:
        print(f"⬇️ Downloading fresh ScripMaster for {today_str}...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        response = requests.get(url)
        data = response.json()
        with open(filename, "w") as f:
            json.dump(data, f)
        print(f"✅ Saved ScripMaster to {filename}")

    return pd.DataFrame(data)
# ---------------------------- MAIN ----------------------------
if __name__ == "__main__":
    HEADERS = smartapi_login()
     
    
    if SIMULATION_MODE:
        if SCRIPTMASTER is None:
            SCRIPTMASTER = load_scripmaster_daily()

        run_simulation(SIMULATION_START, SIMULATION_END)
        
        
        print("\n--- TRADE SUMMARY ---")
        for trade in TRADE_LOG:
            print(trade)
    else:
        print("[⚠️] Live trading not supported in this script.")