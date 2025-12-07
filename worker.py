# worker.py
import requests
import json
import time
import threading
from datetime import datetime
import pandas as pd
import yfinance as yf
import os
import traceback

# ========== CONFIG (same as yours) ==========
SYMBOL = "NIFTY"
OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}
LOOP_SECONDS = 5
YF_TICKER = "^NSEI"
ATM_PCR_UPPER = 1.2
ATM_PCR_LOWER = 0.8

DATA_FILE = "current_data.json"
LOG_FILE = "signals_log.csv"
RUN_FLAG = "running.flag"

# create default log if missing
if not os.path.exists(LOG_FILE):
    pd.DataFrame(columns=["Time", "Signal", "ATM", "PCR", "CE_Votes", "PE_Votes"]).to_csv(LOG_FILE, index=False)

def safe_write_json(path, obj):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        try:
            with open(path, "w") as f:
                json.dump(obj, f)
        except:
            pass

def log_to_csv(time_str, signal, atm, pcr, ce_votes, pe_votes):
    try:
        df = pd.DataFrame([[time_str, signal, atm, pcr, ce_votes, pe_votes]],
                          columns=["Time", "Signal", "ATM", "PCR", "CE_Votes", "PE_Votes"])
        df.to_csv(LOG_FILE, mode='a', header=False, index=False)
    except Exception:
        pass

def get_nse_session():
    s = requests.Session()
    try:
        s.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
        time.sleep(0.3)
    except:
        pass
    return s

def safe_json_load(resp):
    try:
        return resp.json()
    except:
        text = resp.text
        idx = text.rfind("}")
        if idx != -1:
            try:
                return json.loads(text[: idx + 1])
            except:
                return None
        return None

def fetch_option_chain(session):
    try:
        r = session.get(OPTION_CHAIN_URL, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return safe_json_load(r)
    except Exception:
        return None

def flatten_option_chain(json_data):
    if not json_data:
        return pd.DataFrame()

    rec = json_data.get("records", {})
    underlying = rec.get("underlyingValue", None)
    data = rec.get("data", [])
    rows = []

    for item in data:
        strike = item.get("strikePrice")
        ce = item.get("CE")
        pe = item.get("PE")

        if ce:
            rows.append({
                "side": "CE",
                "strike": strike,
                "oi": float(ce.get("openInterest", 0)),
                "coi": float(ce.get("changeinOpenInterest", 0)),
                "vol": float(ce.get("totalTradedVolume", 0)),
                "underlying": underlying
            })
        if pe:
            rows.append({
                "side": "PE",
                "strike": strike,
                "oi": float(pe.get("openInterest", 0)),
                "coi": float(pe.get("changeinOpenInterest", 0)),
                "vol": float(pe.get("totalTradedVolume", 0)),
                "underlying": underlying
            })

    return pd.DataFrame(rows)

def market_trend_last5():
    try:
        df = yf.download(YF_TICKER, period="1d", interval="1m", progress=False)
        if df.empty:
            return "NEUTRAL"

        closes = df["Close"].values
        if len(closes) < 5:
            return "NEUTRAL"

        last5 = closes[-5:]
        if all(last5[i] > last5[i - 1] for i in range(1, 5)):
            return "UP"
        if all(last5[i] < last5[i - 1] for i in range(1, 5)):
            return "DOWN"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def compute_signal(df):
    if df.empty:
        return "NO DATA", ["Option chain empty"], 0, 0.0, (0, 0)

    underlying = df["underlying"].dropna().unique()
    if len(underlying) == 0:
        return "NO DATA", ["Underlying missing"], 0, 0.0, (0, 0)

    underlying_val = underlying[0]
    atm = int(round(underlying_val / 50) * 50)

    atm_rows = df[df["strike"] == atm]
    if atm_rows.empty:
        return "NO DATA", [f"ATM {atm} missing"], atm, 0.0, (0, 0)

    ce = atm_rows[atm_rows["side"] == "CE"].iloc[0]
    pe = atm_rows[atm_rows["side"] == "PE"].iloc[0]

    reasons = [
        f"Underlying = {underlying_val}",
        f"ATM Strike = {atm}"
    ]

    # Volume fight
    if pe["vol"] > ce["vol"]:
        vol_side = "PE"
        reasons.append("PE Volume > CE Volume")
    elif ce["vol"] > pe["vol"]:
        vol_side = "CE"
        reasons.append("CE Volume > PE Volume")
    else:
        vol_side = "NEUTRAL"
        reasons.append("Volumes equal")

    # OI fight
    if pe["oi"] > ce["oi"]:
        oi_side = "PE"
        reasons.append("PE OI > CE OI")
    elif ce["oi"] > pe["oi"]:
        oi_side = "CE"
        reasons.append("CE OI > PE OI")
    else:
        oi_side = "NEUTRAL"
        reasons.append("OI equal")

    # COI fight
    if pe["coi"] > ce["coi"]:
        coi_side = "PE"
        reasons.append("PE COI > CE COI")
    elif ce["coi"] > pe["coi"]:
        coi_side = "CE"
        reasons.append("CE COI > PE COI")
    else:
        coi_side = "NEUTRAL"
        reasons.append("COI equal")

    # PCR
    pcr = round(pe["oi"] / ce["oi"], 2) if ce["oi"] else 1.0
    reasons.append(f"PCR = {pcr}")

    if pcr > ATM_PCR_UPPER:
        pcr_side = "CE"
    elif pcr < ATM_PCR_LOWER:
        pcr_side = "PE"
    else:
        pcr_side = "NEUTRAL"

    trend = market_trend_last5()
    reasons.append(f"Trend = {trend}")

    votes = [vol_side, oi_side, coi_side, pcr_side]
    if trend == "UP":
        votes.append("CE")
    if trend == "DOWN":
        votes.append("PE")

    ce_votes = votes.count("CE")
    pe_votes = votes.count("PE")

    reasons.append(f"Votes → CE:{ce_votes}, PE:{pe_votes}")

    if ce_votes > pe_votes and ce_votes >= 2:
        return "BUY CE", reasons, atm, pcr, (ce_votes, pe_votes)
    if pe_votes > ce_votes and pe_votes >= 2:
        return "BUY PE", reasons, atm, pcr, (ce_votes, pe_votes)

    return "NO TRADE", reasons, atm, pcr, (ce_votes, pe_votes)

def worker_loop():
    session = get_nse_session()
    while True:
        try:
            # Check running flag; if not present or 0, just write stopped state and sleep
            running_state = "1"
            try:
                if os.path.exists(RUN_FLAG):
                    with open(RUN_FLAG, "r") as f:
                        running_state = f.read().strip()
                else:
                    # if no flag, default to stopped until user hits START
                    running_state = "0"
            except:
                running_state = "0"

            if running_state != "1":
                # write a stopped state for UI
                now = datetime.now().strftime("%H:%M:%S")
                current_data = {
                    "time": now,
                    "signal": "Stopped",
                    "atm": "--",
                    "pcr": "--",
                    "ce_votes": 0,
                    "pe_votes": 0,
                    "reasons": ["Bot is stopped"]
                }
                safe_write_json(DATA_FILE, current_data)
                time.sleep(2)
                continue

            json_data = fetch_option_chain(session)
            if json_data is None:
                session = get_nse_session()
                now = datetime.now().strftime("%H:%M:%S")
                current_data = {
                    "time": now,
                    "signal": "NO DATA",
                    "atm": "--",
                    "pcr": "--",
                    "ce_votes": 0,
                    "pe_votes": 0,
                    "reasons": ["Cannot fetch NSE data"]
                }
                safe_write_json(DATA_FILE, current_data)
                time.sleep(LOOP_SECONDS)
                continue

            df = flatten_option_chain(json_data)
            signal, reasons, atm, pcr, votes = compute_signal(df)
            ce_votes, pe_votes = votes
            now = datetime.now().strftime("%H:%M:%S")

            current_data = {
                "time": now,
                "signal": signal,
                "atm": atm,
                "pcr": pcr,
                "ce_votes": ce_votes,
                "pe_votes": pe_votes,
                "reasons": reasons
            }

            safe_write_json(DATA_FILE, current_data)
            log_to_csv(now, signal, atm, pcr, ce_votes, pe_votes)

            time.sleep(LOOP_SECONDS)
        except Exception as e:
            # write error to json for visibility, continue
            try:
                now = datetime.now().strftime("%H:%M:%S")
                safe_write_json(DATA_FILE, {
                    "time": now,
                    "signal": "ERROR",
                    "atm": "--",
                    "pcr": "--",
                    "ce_votes": 0,
                    "pe_votes": 0,
                    "reasons": [str(e), traceback.format_exc().splitlines()[-1]]
                })
            except:
                pass
            time.sleep(5)

if __name__ == "__main__":
    # worker process directly runs loop
    worker_loop()
