"""
Mainboard IPO Day 1-3 tracker (NSE) -> writes a dashboard page: docs/index.html
Runs once daily after market close. No manual input needed.

1. DISCOVER  new NSE listings from NSE's public EQUITY_L.csv (series EQ/BE = Mainboard;
             SME stocks sit in other series, so they never appear).
2. TRACK     Day 1/2/3 = first three trading days from the listing date (weekends and
             holidays are skipped automatically because we use real price history).
3. PUBLISH   alerts + status table into docs/index.html (state kept in state.json).

Rules
  Day 1 : close > open (green)           -> alert. Red -> stop tracking, no alert.
  Day 2 : low >= Day 1 low AND close >= Day 1 low -> alert. Breach -> stop.
  Day 3 : same check as Day 2.            Day 4+: stop tracking.
"""
import csv, io, json, os, datetime as dt
import requests, yfinance as yf

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EQUITY_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
MAINBOARD_SERIES = {"EQ", "BE"}
LOOKBACK_DAYS = 14          # how far back to look for new listings
KEEP_DAYS = 45              # how long to keep a stock in state.json
STATE_FILE, PAGE_FILE, EXTRA_FILE = "state.json", "docs/index.html", "ipos.csv"

MSG_DAY1 = "Stock closed in green on Day 1."
MSG_DAY23 = "Stock did not breach the low of Day 1"


def now_ist():
    return dt.datetime.now(IST)


# ---------- 1. discover ----------
def discover():
    """Return {symbol: {"name":..., "listing_date": "YYYY-MM-DD"}} for recent mainboard listings."""
    found, cutoff = {}, now_ist().date() - dt.timedelta(days=LOOKBACK_DAYS)
    r = requests.get(EQUITY_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    for row in csv.DictReader(io.StringIO(r.text)):
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
        if row.get("SERIES") not in MAINBOARD_SERIES:
            continue
        try:
            d = dt.datetime.strptime(row["DATE OF LISTING"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        if d >= cutoff:
            found[row["SYMBOL"]] = {"name": row.get("NAME OF COMPANY", ""), "listing_date": d.isoformat()}
    return found


def read_extras():
    """Optional safety net: add rows to ipos.csv (symbol,listing_date) if auto-discovery ever misses one."""
    out = {}
    if os.path.exists(EXTRA_FILE):
        with open(EXTRA_FILE) as f:
            for row in csv.DictReader(f):
                if row.get("symbol") and row.get("listing_date"):
                    out[row["symbol"].strip().upper()] = {"name": "", "listing_date": row["listing_date"].strip()}
    return out


# ---------- 2. track ----------
def get_candles(symbol, listing_date):
    """First 3 trading-day candles from the listing date. Today's candle is ignored until after close."""
    df = yf.Ticker(f"{symbol}.NS").history(start=listing_date, interval="1d", auto_adjust=False)
    now, out = now_ist(), []
    for ts, r in df.iterrows():
        d = ts.strftime("%Y-%m-%d")
        if d < listing_date:
            continue
        if d == now.date().isoformat() and now.hour * 60 + now.minute < 15 * 60 + 45:
            continue  # market still open / bar not final
        out.append({"date": d, "open": round(float(r.Open), 2), "low": round(float(r.Low), 2),
                    "close": round(float(r.Close), 2)})
    return out[:3]


def add_alert(st, day, msg):
    st["alerts"].append({"day": day, "msg": msg, "candle_date": st["days"][str(day)]["date"],
                         "raised_at": now_ist().isoformat(timespec="minutes")})


def evaluate(st, candles):
    if st["status"] != "tracking" or not candles:
        return
    if "1" not in st["days"]:
        c = candles[0]
        green = c["close"] > c["open"]
        st["days"]["1"] = {**c, "result": "green" if green else "red"}
        if not green:
            st.update(status="stopped", reason="Day 1 closed red")
            return
        add_alert(st, 1, MSG_DAY1)
    d1 = st["days"]["1"]
    for k in (2, 3):
        if len(candles) >= k and str(k) not in st["days"]:
            c = candles[k - 1]
            held = c["low"] >= d1["low"] and c["close"] >= d1["low"]
            st["days"][str(k)] = {**c, "result": "held" if held else "breached"}
            if not held:
                st.update(status="stopped", reason=f"Day 1 low breached on Day {k}")
                return
            add_alert(st, k, MSG_DAY23)
    if "3" in st["days"]:
        st["status"] = "done"


# ---------- 3. publish ----------
def render(state, run_info):
    with open(os.path.join(os.path.dirname(__file__), "template.html"), encoding="utf-8") as f:
        tpl = f.read()
    payload = json.dumps({"run": run_info, "stocks": state}, separators=(",", ":")).replace("</", "<\\/")
    os.makedirs(os.path.dirname(PAGE_FILE), exist_ok=True)
    with open(PAGE_FILE, "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", payload))


def main():
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    run = {"at": now_ist().isoformat(timespec="minutes"), "ok": True, "note": ""}
    try:
        found = {**discover(), **read_extras()}
    except Exception as e:  # NSE file unreachable: keep tracking what we already know
        found, run["ok"], run["note"] = {}, False, f"Could not fetch NSE listings file: {e}"
    for sym, info in found.items():
        state.setdefault(sym, {**info, "status": "tracking", "reason": "", "days": {}, "alerts": []})
    keep_from = (now_ist().date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    state = {s: v for s, v in state.items() if v["listing_date"] >= keep_from}
    for sym, st in state.items():
        if st["status"] != "tracking":
            continue
        try:
            evaluate(st, get_candles(sym, st["listing_date"]))
        except Exception as e:
            run["ok"], run["note"] = False, (run["note"] + f" Price fetch failed for {sym}: {e}").strip()
    json.dump(state, open(STATE_FILE, "w"), indent=1)
    render(state, run)
    print("done:", len(state), "stocks;", run)


if __name__ == "__main__":
    main()
