from flask import Flask, jsonify, Response
import requests, subprocess, tempfile, os, re
from PIL import Image, ImageEnhance, ImageFilter

app = Flask(__name__)
SOURCE_URL = "http://ksv-weissach.host4free.de/Kegelbahn/Index.png"

@app.route("/")
def home():
    return jsonify({
        "service": "KSV Weissach Liveticker OCR",
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

def read_scoreboard():
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
    home="KSV Weissach 1"
    away="HKO Young Stars"
    lines=[clean(x) for x in raw.splitlines() if clean(x)]
    if lines:
        top=lines[0]
        top=re.sub(r"(?i)^[@\s]*sv\s+weissach","KSV Weissach",top)
        top=re.sub(r"(?i)h?o\s+youg\s+stars","HKO Young Stars",top)
        top=re.sub(r"(?i)hko\s+youg\s+stars","HKO Young Stars",top)
        m=re.search(r"(KSV\s+Weissach\s*\d*)\s+[@| ]+\s*(HKO\s+Young\s+Stars)",top,re.I)
        if m:
            home=clean(m.group(1))
            away=clean(m.group(2))
    return home,away

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

def replacement_markers(raw):
    """
    Erfasst alles, was OCR im Spielerfeld als Ersatz/Einwechslung erkennt.
    Da verschiedene Anzeigetafeln Einwechslungen unterschiedlich darstellen,
    werden die Originalzeilen zusätzlich unverändert geliefert.
    """
    lines=[clean(x) for x in raw.splitlines() if clean(x)]
    keys=("ersatz","wechsel","einwechsl","auswechsl","eingewechselt")
    return [x for x in lines if any(k in x.lower() for k in keys)]

def parse_bottom(text):
    c=clean(text)
    out={"home_throws":None,"home_total":None,"home_points":None,
         "away_points":None,"away_total":None,"away_throws":None,"difference":None}

    pm=re.search(r"(\d+(?:[.,]\d)?)\s*[:\-]\s*(\d+(?:[.,]\d)?)",c)
    if pm:
        out["home_points"]=float(pm.group(1).replace(",","."))
        out["away_points"]=float(pm.group(2).replace(",","."))

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

def build():
    raw,bottom_raw=read_scoreboard()
    ht,at=team_names(raw)
    hp,ap=parse_players(raw)
    b=parse_bottom(bottom_raw)
    markers=replacement_markers(raw)

    # Nur als Fallback summieren. Bei Ein-/Auswechslungen oder Ersatz
    # haben die offiziellen Gesamtwerte aus dem unteren Feld Vorrang.
    if b["home_total"] is None and hp and not markers:
        b["home_total"]=sum(p["total"] for p in hp)
    if b["away_total"] is None and ap and not markers:
        b["away_total"]=sum(p["total"] for p in ap)
    if b["home_total"] is not None and b["away_total"] is not None:
        b["difference"]=b["home_total"]-b["away_total"]

    return {
        "success":True,
        "home":{"team":ht,"players":hp,"player_count":len(hp),
                "throws":b["home_throws"],"total":b["home_total"]},
        "away":{"team":at,"players":ap,"player_count":len(ap),
                "throws":b["away_throws"],"total":b["away_total"]},
        "points":{"home":b["home_points"],"away":b["away_points"]},
        "difference":b["difference"],
        "players_detected":len(hp)+len(ap),
        "replacement_detected":bool(markers),
        "replacement_lines":markers,
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
    try:
        d=build()
        d.pop("raw",None)
        d.pop("bottom_raw",None)
        return jsonify(d)
    except subprocess.TimeoutExpired:
        return jsonify({"success":False,"error":"OCR timeout"}),504
    except Exception as e:
        return jsonify({"success":False,"error":str(e)}),500

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
