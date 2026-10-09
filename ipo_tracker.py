"""
Mainboard IPO Day 1-3 tracker (NSE) -> dashboard page docs/index.html. Runs unattended.

PIPELINE
 1. DISCOVER  recent listings from NSE's public EQUITY_L.csv, then filter strictly to genuine
              Mainboard IPOs (see reject_reason + history check below).
 2. TRACK     Day 1/2/3 = first three trading days from the listing date (Yahoo daily candles).
 3. NOTIFY    ONE notification per stock, on Day 3, only if it passed every check.
 4. PUBLISH   docs/index.html (state kept in state.json, filter diagnostics in docs/excluded.json)

RULES
  Day 1 : close > open (green). Red -> stop, no notification.
  Day 2 : low >= Day 1 low AND close >= Day 1 low. Breach -> stop.
  Day 3 : same check. If it also passes -> notification:
          "Stock did not breach the low of opening day in last 3 days."
  Day 4+: stop tracking.
"""
import csv, io, json, os, re, datetime as dt
import requests, yfinance as yf

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HDRS = {"User-Agent": "Mozilla/5.0"}
EQUITY_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
SME_URL = "https://archives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"  # best effort
MAINBOARD_SERIES = {"EQ", "BE"}
LOOKBACK_DAYS = 14
KEEP_DAYS = 45                      # how long finished stocks stay in state.json (notifications are kept forever)
STATE_FILE, PAGE_FILE, EXCL_FILE = "state.json", "docs/index.html", "docs/excluded.json"
SEED_FILE, EXTRA_FILE = "exclude.txt", "ipos.csv"

MSG_FINAL = "Stock did not breach the low of opening day in last 3 days."

RIGHTS_SYMBOL = re.compile(r"-(RE\d*|PP\d*|P\d+|W\d+|N\d+)$", re.I)
RIGHTS_NAME = re.compile(r"(-\s*(RE|PP)\s*$)|rights\s*entitlement|partly\s*paid", re.I)


def now_ist():
    return dt.datetime.now(IST)


def empty_state():
    return {"stocks": {}, "notifications": [], "excluded": {}, "sme_seen": {"symbols": [], "isins": []}}


# ---------- network (kept separate so tests can replace them) ----------
def fetch_equity_csv():
    r = requests.get(EQUITY_URL, headers=HDRS, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_sme_csv():
    r = requests.get(SME_URL, headers=HDRS, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_history(symbol):
    """All daily candles Yahoo has for SYMBOL.NS, oldest first. [] if none."""
    df = yf.Ticker(f"{symbol}.NS").history(period="max", interval="1d", auto_adjust=False)
    return [{"date": ts.strftime("%Y-%m-%d"), "open": round(float(r.Open), 2),
             "low": round(float(r.Low), 2), "close": round(float(r.Close), 2)} for ts, r in df.iterrows()]


# ---------- 1. discover + filter ----------
def parse_rows(text):
    for row in csv.DictReader(io.StringIO(text)):
        yield {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}


def read_seed():
    out = set()
    if os.path.exists(SEED_FILE):
        for line in open(SEED_FILE):
            line = line.split("#")[0].strip().upper()
            if line:
                out.add(line)
    return out


def reject_reason(row, sym, name, listed, today, seed, sme):
    if sym in seed:
        return "On exclusion list (SME upgrade / not a Mainboard IPO)"
    if listed > today:
        return "Listing date is in the future"
    if RIGHTS_SYMBOL.search(sym) or RIGHTS_NAME.search(name):
        return "Rights entitlement / partly-paid security"
    try:
        if float(row.get("PAID UP VALUE", "")) != float(row.get("FACE VALUE", "")):
            return "Partly paid (paid-up value differs from face value)"
    except ValueError:
        pass
    if sym in set(sme["symbols"]) or (row.get("ISIN NUMBER") and row["ISIN NUMBER"] in set(sme["isins"])):
        return "Was listed on the SME board earlier (SME upgrade)"
    return None


def discover(state):
    """Return (candidates, rule_excluded)."""
    today = now_ist().date()
    cutoff = today - dt.timedelta(days=LOOKBACK_DAYS)
    seed, sme = read_seed(), state["sme_seen"]
    cands, excluded = {}, {}
    for row in parse_rows(fetch_equity_csv()):
        if row.get("SERIES") not in MAINBOARD_SERIES:
            continue
        try:
            listed = dt.datetime.strptime(row["DATE OF LISTING"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        if listed < cutoff:
            continue
        sym, name = row["SYMBOL"], row.get("NAME OF COMPANY", "")
        why = reject_reason(row, sym, name, listed, today, seed, sme)
        rec = {"name": name, "listing_date": listed.isoformat(), "isin": row.get("ISIN NUMBER", "")}
        if why:
            excluded[sym] = {**rec, "reason": why}
        else:
            cands[sym] = rec
    if os.path.exists(EXTRA_FILE):  # optional manual safety net
        for row in csv.DictReader(open(EXTRA_FILE)):
            if row.get("symbol") and row.get("listing_date"):
                cands[row["symbol"].strip().upper()] = {"name": "", "listing_date": row["listing_date"].strip(), "isin": ""}
    return cands, excluded


def update_sme_registry(state):
    """Remember every symbol/ISIN seen on the SME board, so a later move to Mainboard is recognised."""
    try:
        for row in parse_rows(fetch_sme_csv()):
            if row.get("SYMBOL") and row["SYMBOL"] not in state["sme_seen"]["symbols"]:
                state["sme_seen"]["symbols"].append(row["SYMBOL"])
            isin = row.get("ISIN NUMBER")
            if isin and isin not in state["sme_seen"]["isins"]:
                state["sme_seen"]["isins"].append(isin)
        return "ok"
    except Exception as e:
        return f"unavailable ({type(e).__name__})"


# ---------- 2. track ----------
def data_due(listing_date):
    """Is Day 1's final candle expected to exist by now?"""
    n = now_ist()
    d = n.date().isoformat()
    return listing_date < d or (listing_date == d and n.hour * 60 + n.minute >= 15 * 60 + 45)


def evaluate(st, candles):
    """Apply Day 1-3 rules. Returns True when the stock has just passed all three days."""
    if st["status"] != "tracking":
        return False
    if "1" not in st["days"]:
        c = candles[0]
        green = c["close"] > c["open"]
        st["days"]["1"] = {**c, "result": "green" if green else "red"}
        if not green:
            st.update(status="stopped", reason="Day 1 closed red")
            return False
    d1 = st["days"]["1"]
    for k in (2, 3):
        if len(candles) >= k and str(k) not in st["days"]:
            c = candles[k - 1]
            held = c["low"] >= d1["low"] and c["close"] >= d1["low"]
            st["days"][str(k)] = {**c, "result": "held" if held else "breached"}
            if not held:
                st.update(status="stopped", reason=f"Day 1 low breached on Day {k}")
                return False
    if "3" in st["days"]:
        st["status"] = "done"
        return True
    return False


def track(sym, st, history):
    """Returns 'excluded', 'passed' or None."""
    if not history:
        st["pending"] = data_due(st["listing_date"])
        return None
    if any(c["date"] < st["listing_date"] for c in history):
        return "excluded"  # traded before listing date -> SME upgrade / relisting, not an IPO
    now = now_ist()
    cs = [c for c in history if c["date"] >= st["listing_date"]]
    if cs and cs[-1]["date"] == now.date().isoformat() and now.hour * 60 + now.minute < 15 * 60 + 45:
        cs = cs[:-1]  # today's candle not final yet
    if not cs or cs[0]["date"] != st["listing_date"]:
        st["pending"] = data_due(st["listing_date"])  # Day 1 candle not available yet
        return None
    st["pending"] = False
    return "passed" if evaluate(st, cs[:3]) else None


# ---------- 3/4. notify + publish ----------
def notify(state, sym, st):
    nid = f"{sym}-{st['listing_date']}"
    if any(n["id"] == nid for n in state["notifications"]):
        return
    state["notifications"].append({
        "id": nid, "symbol": sym, "name": st["name"], "listing_date": st["listing_date"],
        "msg": MSG_FINAL, "candle_date": st["days"]["3"]["date"],
        "raised_at": now_ist().isoformat(timespec="minutes")})


def render(state, run):
    here = os.path.dirname(os.path.abspath(__file__))
    tpl = open(os.path.join(here, "template.html"), encoding="utf-8").read()
    payload = json.dumps({"run": run, "stocks": state["stocks"], "notifications": state["notifications"]},
                         separators=(",", ":")).replace("</", "<\\/")
    os.makedirs(os.path.dirname(PAGE_FILE), exist_ok=True)
    open(PAGE_FILE, "w", encoding="utf-8").write(tpl.replace("/*__DATA__*/null", payload))


def main():
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    for k, v in empty_state().items():
        state.setdefault(k, v)
    run = {"at": now_ist().isoformat(timespec="minutes"), "ok": True, "note": "", "pending": []}
    sme_status = update_sme_registry(state)
    rule_excluded = {}
    try:
        cands, rule_excluded = discover(state)
    except Exception as e:  # NSE file unreachable: keep tracking what we already know
        cands, run["ok"], run["note"] = {}, False, f"Could not fetch NSE listings file: {e}"
    for sym, info in cands.items():
        if sym not in state["excluded"]:
            state["stocks"].setdefault(sym, {**info, "status": "tracking", "reason": "", "days": {}, "pending": False})
    for sym in rule_excluded:                      # clean out anything the stricter rules now reject
        state["stocks"].pop(sym, None)
    keep_from = (now_ist().date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["stocks"] = {s: v for s, v in state["stocks"].items() if v["listing_date"] >= keep_from}

    for sym, st in list(state["stocks"].items()):
        if st["status"] != "tracking":
            continue
        try:
            outcome = track(sym, st, fetch_history(sym))
        except Exception as e:
            run["ok"], run["note"] = False, (run["note"] + f" Price fetch failed for {sym}: {e}").strip()
            continue
        if outcome == "excluded":
            state["excluded"][sym] = {"name": st["name"], "listing_date": st["listing_date"],
                                       "reason": "Traded before its listing date (SME upgrade / relisting)"}
            del state["stocks"][sym]
        elif outcome == "passed":
            notify(state, sym, st)
    run["pending"] = sorted(s for s, v in state["stocks"].items() if v.get("pending"))

    json.dump(state, open(STATE_FILE, "w"), indent=1)
    os.makedirs(os.path.dirname(EXCL_FILE), exist_ok=True)
    json.dump({"updated": run["at"], "sme_registry": sme_status,
               "excluded": {**rule_excluded, **state["excluded"]}}, open(EXCL_FILE, "w"), indent=1)
    render(state, run)
    print("done:", len(state["stocks"]), "tracked;", len(state["notifications"]), "notifications;", run)


if __name__ == "__main__":
    main()
