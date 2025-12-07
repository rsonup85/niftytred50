# app.py
import os
import json
from flask import Flask, jsonify, render_template_string, send_file

app = Flask(__name__)

HTML = """PUT YOUR ENTIRE HTML HERE"""
# Replace the placeholder above with the full HTML from your original code (between triple quotes).
# Make sure the JS fetch URLs point to '/status', '/start', '/stop' (they already do in your HTML).

DATA_FILE = "current_data.json"
RUN_FLAG = "running.flag"

def read_current_data():
    default = {
        "time": "--",
        "signal": "Waiting…",
        "atm": "--",
        "pcr": "--",
        "ce_votes": 0,
        "pe_votes": 0,
        "reasons": ["No data yet"]
    }
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return default

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/status")
def status():
    data = read_current_data()
    return jsonify(data)

@app.route("/start")
def start():
    # create running flag so worker will run (worker always running on Render, but checks flag)
    try:
        with open(RUN_FLAG, "w") as f:
            f.write("1")
        return jsonify({"status":"started"})
    except Exception as e:
        return jsonify({"status":"error", "msg": str(e)}), 500

@app.route("/stop")
def stop():
    try:
        with open(RUN_FLAG, "w") as f:
            f.write("0")
        return jsonify({"status":"stopped"})
    except Exception as e:
        return jsonify({"status":"error", "msg": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
