from flask import Flask, jsonify, Response, request
import requests, subprocess, tempfile, os, re, threading, time, hashlib
from PIL import Image, ImageEnhance, ImageFilter
from pathlib import Path

app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    allowed = {
        "https://test.ksv-info.de",
        "http://test.ksv-info.de",
        "https://ksv-info.de",
        "https://www.ksv-info.de"
    }
    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

SOURCE_URL = "http://ksv-weissach.host4free.de/Kegelbahn/Index.png"


CACHE_LOCK = threading.Lock()
CACHE_DATA = None
CACHE_UPDATED = None
OCR_RUNNING = False
CACHE_MAX_AGE = 5
LAST_IMAGE_HASH = None
LAST_CHECKED = None

def compact_data(d):
    d = dict(d)
    d.pop("raw", None)
    d.pop("bottom_raw", None)
    d["cache_updated"] = CACHE_UPDATED
    return d

def refresh_cache():
    global CACHE_DATA, CACHE_UPDATED, OCR_RUNNING, LAST_IMAGE_HASH, LAST_CHECKED
    with CACHE_LOCK:
        if OCR_RUNNING:
            return
        OCR_RUNNING = True
    try:
        image_data = fetch_image()
        image_hash = hashlib.sha256(image_data).hexdigest()
        now = int(time.time())

        with CACHE_LOCK:
            unchanged = CACHE_DATA is not None and LAST_IMAGE_HASH == image_hash
            LAST_CHECKED = now

        if unchanged:
            # Bild identisch: keine OCR. Der vorhandene Spielstand bleibt gültig.
            with CACHE_LOCK:
                CACHE_UPDATED = now
            return

        # Nur ein tatsächlich neues Bild wird per OCR ausgewertet.
        data = build(image_data)
        with CACHE_LOCK:
            CACHE_DATA = data
            CACHE_UPDATED = int(time.time())
            LAST_IMAGE_HASH = image_hash
    except Exception:
        # Letzten gültigen Spielstand behalten.
        pass
    finally:
        with CACHE_LOCK:
            OCR_RUNNING = False
def ensure_refresh():
    now = int(time.time())
    with CACHE_LOCK:
        stale = CACHE_UPDATED is None or (now - CACHE_UPDATED) >= CACHE_MAX_AGE
        running = OCR_RUNNING
    if stale and not running:
        threading.Thread(target=refresh_cache, daemon=True).start()

@app.route("/")
def home():
    return jsonify({
        "service": "KSV Weissach Liveticker OCR",
        "version": "3.1-change-detection",
        "status": "online",
        "image": "/image",
        "ocr": "/ocr",
        "live": "/live"
    })

def fetch_image():
    r = requests.get(SOURCE_URL, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
    r.raise_for_status()
    return r.content

@app.route("/image")
def image():
    try:
        data = fetch_image()
        return Response(data, content_type="image/png",
            headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)}),500

def tesseract(img, psm=6):
    with tempfile.TemporaryDirectory() as tmp:
        fn=os.path.join(tmp,"ocr.png")
        x=img.convert("L")
        if x.width>1600:
            ratio=1600/x.width
            x=x.resize((1600,max(1,int(x.height*ratio))),Image.Resampling.LANCZOS)
        x=ImageEnhance.Contrast(x).enhance(2.0)
        x=x.filter(ImageFilter.SHARPEN)
        x.save(fn,"PNG",optimize=True)
        r=subprocess.run(
            ["tesseract",fn,"stdout","-l","deu+eng","--psm",str(psm)],
            capture_output=True,text=True,timeout=90)
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or "Tesseract failed")
        return r.stdout

def read_scoreboard(data=None):
    if data is None:
        data=fetch_image()
    with tempfile.TemporaryDirectory() as tmp:
        fn=os.path.join(tmp,"source.png")
        Path(fn).write_bytes(data)
        img=Image.open(fn).convert("RGB")
        raw=tesseract(img,6)
        h=img.height
        bottom=tesseract(img.crop((0,int(h*.70),img.width,h)),6)
        return raw,bottom

def clean(s):
    s=s.replace("\u2014","-").replace("\u2013","-")
    return re.sub(r"[ \t]+"," ",s).strip()

PLAYER_RE=re.compile(
    r"(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s+(\d{1,4})\s+(\d+(?:[.,]\d)?)"
)

def make_player(name,m):
    name=clean(name).strip(" -|@}9")
    name=re.sub(r"^[^A-Za-zÄÖÜäöüß]+","",name).strip()
    if len(name)<2:
        return None
    vals=[int(m.group(i)) for i in range(1,6)]
    return {
        "name":name,
        "sets":vals[:4],
        "total":vals[4],
        "sap":float(m.group(6).replace(",", ".")),
        "substitute":False
    }

def parse_player_line(line):
    line=clean(line)
    ms=list(PLAYER_RE.finditer(line))
    if len(ms)<2:
        return None,None
    left=make_player(line[:ms[0].start()],ms[0])
    middle=line[ms[0].end():ms[1].start()]
    middle=re.sub(r"^[^A-Za-zÄÖÜäöüß]+","",middle)
    right=make_player(middle,ms[1])
    return left,right

def team_names(raw):
    lines = [re.sub(r"\s+", " ", x).strip() for x in (raw or "").splitlines() if x.strip()]
    for line in lines[:15]:
        low = line.lower()
        if any(k in low for k in ("satz 1", "total", "sap", "ergebnis", "wurf", "punkte", "name")):
            continue
        # Typical header OCR: "@ KSV Weissach 1 @ HKO Young Stars"
        parts = [p.strip(" @|:-") for p in re.split(r"\s*[©@|]\s*|\s{3,}", line)
                 if p.strip(" @|:-")]
        parts = [p for p in parts if len(p) >= 3 and re.search(r"[A-Za-zÄÖÜäöüß]", p)]
        if len(parts) >= 2:
            return parts[0], parts[-1]
    return "Heimmannschaft", "Gastmannschaft"

def parse_players(raw):
    home=[]; away=[]
    for line in raw.splitlines():
        if len(PLAYER_RE.findall(clean(line)))<2:
            continue
        h,a=parse_player_line(line)
        if h: home.append(h)
        if a: away.append(a)

    # Keine feste Mannschaftsgröße mehr: bis zu 8 sichtbare Spieler je Seite.
    return home[:8],away[:8]

def substitution_info(raw):
    """
    Zusätzliche, rein informative Wechsel-Erkennung.
    Verändert weder Spielerlisten noch Mannschaftsnamen oder Ergebnisse.
    """
    lines = [clean(x) for x in (raw or "").splitlines() if clean(x)]
    events = []
    keywords = ("wechsel", "einwechsl", "auswechsl", "eingewechselt", "ausgewechselt")
    for line in lines:
        low = line.lower()
        if any(k in low for k in keywords):
            events.append({"type": "wechsel", "raw": line})
        elif "ersatz" in low:
            # Reine Tabellen-/Platzhalterzeilen wie "Ersatz Ersatz" nicht als echten Wechsel melden.
            stripped = re.sub(r"(?i)\bersatz\b", " ", line)
            stripped = re.sub(r"[^A-Za-zÄÖÜäöüß0-9]+", "", stripped)
            if stripped:
                events.append({"type": "ersatz", "raw": line})
    return {
        "detected": bool(events),
        "count": len(events),
        "events": events
    }

def replacement_markers(raw):
    """
    Erfasst alles, was OCR im Spielerfeld als Ersatz/Einwechslung erkennt.
    Da verschiedene Anzeigetafeln Einwechslungen unterschiedlich darstellen,
    werden die Originalzeilen zusätzlich unverändert geliefert.
    """
    lines=[clean(x) for x in raw.splitlines() if clean(x)]
    found=[]
    for line in lines:
        low=line.lower()
        if any(k in low for k in ("wechsel","einwechsl","auswechsl","eingewechselt")):
            found.append(line); continue
        if "ersatz" in low:
            rest=re.sub(r"(?i)\bersatz\b"," ",line)
            rest=re.sub(r"[^A-Za-zÄÖÜäöüß0-9]+","",rest)
            if rest: found.append(line)
    return found

def parse_bottom(text):
    c=clean(text)
    out={"home_throws":None,"home_total":None,"home_points":None,
         "away_points":None,"away_total":None,"away_throws":None,"difference":None}

    pm=re.search(r"(\d+(?:[.,]\d)?)\s*[:\-]\s*(\d+(?:[.,]\d)?)",c)
    if pm:
        hp=float(pm.group(1).replace(",",".")); ap=float(pm.group(2).replace(",","."))
        if hp > 8 and hp / 10 <= 8: hp /= 10
        if ap > 8 and ap / 10 <= 8: ap /= 10
        out["home_points"]=hp; out["away_points"]=ap

    ints=[int(x) for x in re.findall(r"(?<![\d.])(\d{3,4})(?![\d.])",c)]
    totals=[x for x in ints if 1000<=x<=9999]
    throws=[x for x in ints if 0<=x<1000]

    if len(totals)>=2:
        out["home_total"],out["away_total"]=totals[0],totals[-1]
    if len(throws)>=2:
        out["home_throws"],out["away_throws"]=throws[0],throws[-1]
    if out["home_total"] is not None and out["away_total"] is not None:
        out["difference"]=out["home_total"]-out["away_total"]
    return out

def build(image_data=None):
    raw,bottom_raw=read_scoreboard(image_data)
    ht,at=team_names(raw)
    hp,ap=parse_players(raw)
    b=parse_bottom(bottom_raw)
    markers=replacement_markers(raw)
    subinfo=substitution_info(raw)

    # Nur als Fallback summieren. Bei Ein-/Auswechslungen oder Ersatz
    # haben die offiziellen Gesamtwerte aus dem unteren Feld Vorrang.
    if b["home_total"] is None and hp and not markers:
        b["home_total"]=sum(p["total"] for p in hp)
    if b["away_total"] is None and ap and not markers:
        b["away_total"]=sum(p["total"] for p in ap)
    if b["home_total"] is not None and b["away_total"] is not None:
        b["difference"]=b["home_total"]-b["away_total"]

    header_raw = next(
        (clean(line) for line in raw.splitlines()
         if "©" in line or ("KSV" in line and len(line) < 120)),
        ""
    )

    return {
        "success":True,
        "header_raw":header_raw,
        "home":{"team":ht,"players":hp,"player_count":len(hp),
                "throws":b["home_throws"],"total":b["home_total"]},
        "away":{"team":at,"players":ap,"player_count":len(ap),
                "throws":b["away_throws"],"total":b["away_total"]},
        "points":{"home":b["home_points"],"away":b["away_points"]},
        "difference":b["difference"],
        "players_detected":len(hp)+len(ap),
        "replacement_detected":bool(markers),
        "replacement_lines":markers,
        "substitution_info":subinfo,
        "raw":raw,
        "bottom_raw":bottom_raw
    }

@app.route("/ocr")
def ocr():
    try:
        return jsonify(build())
    except subprocess.TimeoutExpired:
        return jsonify({"success":False,"error":"OCR timeout"}),504
    except Exception as e:
        return jsonify({"success":False,"error":str(e)}),500

@app.route("/live")
def live():
    global CACHE_DATA
    ensure_refresh()

    with CACHE_LOCK:
        cached = CACHE_DATA
        updated = CACHE_UPDATED
        running = OCR_RUNNING

    if cached is None:
        # First request after a Render cold start: start OCR and return quickly.
        return jsonify({
            "success": False,
            "warming_up": True,
            "ocr_running": running,
            "error": "OCR wird initialisiert. Bitte in wenigen Sekunden erneut abrufen."
        }), 202

    d = dict(cached)
    d.pop("raw", None)
    d.pop("bottom_raw", None)
    d["cache_updated"] = updated
    d["ocr_running"] = running
    d["cache_age_seconds"] = max(0, int(time.time()) - updated) if updated else None
    d["image_checked"] = LAST_CHECKED
    d["change_detection"] = True
    return jsonify(d)


def delayed_start():
    time.sleep(1)
    refresh_cache()

threading.Thread(target=delayed_start, daemon=True).start()

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
