import requests
import json
import time
import threading
from datetime import datetime
import pandas as pd
import yfinance as yf
import os
from flask import Flask, jsonify, render_template_string

# Try to import winsound for beep on Windows; fail silently on other OS.
try:
    import winsound
    _HAS_WINSOUND = True
except:
    _HAS_WINSOUND = False

# ---------------- CONFIG ----------------
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
running = False
current_data = {
    "time": "",
    "signal": "Waiting…",
    "atm": "",
    "pcr": "",
    "ce_votes": 0,
    "pe_votes": 0,
    "reasons": []
}
# ----------------------------------------

# ========== LOGGING ==========
LOG_FILE = "signals_log.csv"
if not os.path.exists(LOG_FILE):
    pd.DataFrame(columns=["Time", "Signal", "ATM", "PCR", "CE_Votes", "PE_Votes"]).to_csv(LOG_FILE, index=False)


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
    except:
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


# ---------------- BOT LOOP ----------------
def bot_loop():
    global running, current_data
    session = get_nse_session()

    while running:
        try:
            json_data = fetch_option_chain(session)
            if json_data is None:
                session = get_nse_session()
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

            log_to_csv(now, signal, atm, pcr, ce_votes, pe_votes)

            time.sleep(LOOP_SECONDS)

        except:
            session = get_nse_session()
            time.sleep(2)


# ---------------- FLASK APP ----------------
app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">

<title>NIFTY OPTION BUYING SIGNAL</title>

<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');

  body {
    margin: 0;
    padding: 0;
    font-family: 'Inter', sans-serif;
    background: #eef1f5;
  }

  .container {
    padding: 14px;
    max-width: 460px;
    margin: auto;
  }

  .box {
    background: white;
    border-radius: 16px;
    padding: 22px;
    box-shadow: 0 4px 25px rgba(0,0,0,0.08);
    transition: 0.3s;
  }

  h3 {
    text-align: center;
    margin: 0 0 18px 0;
    font-size: 1.5rem;
    color: #222;
    font-weight: 700;
  }

  .row {
    margin: 10px 0;
    font-size: 1.1rem;
    color: #444;
  }

  .label {
    font-weight: 600;
    color: #333;
  }

  #signal {
    font-weight: 700;
    font-size: 1.35rem;
    padding: 6px 12px;
    border-radius: 8px;
    display: inline-block;
  }

  #votes {
    font-weight: 600;
    font-size: 1.05rem;
  }

  h4 {
    margin-top: 18px;
    font-size: 1.2rem;
    font-weight: 600;
  }

  ul {
    padding-left: 20px;
    max-height: 160px;
    overflow-y: auto;
    font-size: 1rem;
    color: #444;
  }

  /* Mobile bounce buttons */
  button {
    width: 48%;
    padding: 15px 5px;
    margin-top: 16px;
    font-size: 1.1rem;
    font-weight: 600;
    border-radius: 10px;
    border: none;
    color: white;
    cursor: pointer;
    transition: transform .1s ease-in-out;
  }

  button:active {
    transform: scale(0.96);
  }

  .start-btn {
    background: #079b47;
    box-shadow: 0 3px 12px rgba(7,155,71,0.4);
  }

  .stop-btn {
    background: #d83434;
    box-shadow: 0 3px 12px rgba(216,52,52,0.4);
  }

  .btn-row {
    display: flex;
    justify-content: space-between;
  }

  /* Dynamic Backgrounds */
  .signal-ce {
    background-color: #d5f5d8 !important;
  }

  .signal-pe {
    background-color: #ffd4d4 !important;
  }

  .signal-none {
    background-color: #f0f0f0 !important;
  }

  /* Responsive */
  @media(max-width: 550px){
    .box {
      padding: 18px;
      border-radius: 14px;
    }
    h3 {
      font-size: 1.35rem;
    }
    button {
      font-size: 1rem;
      padding: 13px 5px;
    }
  }
</style>

<script>
  function startBot() {
      fetch('/start');
  }

  function stopBot() {
      fetch('/stop');
  }

  setInterval(() => {
      fetch('/status')
          .then(r => r.json())
          .then(data => {
              document.getElementById("last").innerText = data.time;
              document.getElementById("signal").innerText = data.signal;
              document.getElementById("atm").innerText = data.atm;
              document.getElementById("pcr").innerText = data.pcr;
              document.getElementById("votes").innerText =
                  "CE=" + data.ce_votes + " | PE=" + data.pe_votes;

              let rBox = document.getElementById("reasons");
              rBox.innerHTML = "";
              data.reasons.forEach(r => rBox.innerHTML += "<li>" + r + "</li>");

              const box = document.querySelector(".box");
              if (data.signal === "BUY CE") box.className = "box signal-ce";
              else if (data.signal === "BUY PE") box.className = "box signal-pe";
              else if (data.signal === "NO TRADE") box.className = "box signal-none";
              else box.className = "box";
          });
  }, 1000);
</script>

</head>
<body>

<div class="container">
  <div class="box">

    <h3>NIFTY OPTION BUYING SIGNAL</h3>

    <div class="row"><span class="label">Last Update:</span> <span id="last">--</span></div>
    <div class="row"><span class="label">Signal:</span> <span id="signal">Waiting…</span></div>

    <div class="row">
      <span class="label">ATM:</span> <span id="atm">--</span> |
      <span class="label">PCR:</span> <span id="pcr">--</span>
    </div>

    <div class="row"><span class="label">Votes:</span> <span id="votes">CE=0 | PE=0</span></div>

    <h4>Reasons:</h4>
    <ul id="reasons"></ul>

    <div class="btn-row">
      <button class="start-btn" onclick="startBot()">START</button>
      <button class="stop-btn" onclick="stopBot()">STOP</button>
    </div>

  </div>
</div>

</body>
</html>
"""




@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/start")
def start():
    global running
    if not running:
        running = True
        threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/stop")
def stop():
    global running
    running = False
    return jsonify({"status": "stopped"})


@app.route("/status")
def status():
    return jsonify(current_data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
