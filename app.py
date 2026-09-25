from flask import Flask, jsonify, Response
import requests
import subprocess
import tempfile
import os
import re
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


@app.route("/image")
def image():
    try:
        r = requests.get(
            SOURCE_URL,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()

        return Response(
            r.content,
            content_type="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"
            }
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def fetch_image():
    r = requests.get(
        SOURCE_URL,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    r.raise_for_status()
    return r.content


def run_tesseract(image, psm=6):
    with tempfile.TemporaryDirectory() as tmp:
        processed = os.path.join(tmp, "processed.png")

        img = image.convert("L")

        max_width = 1400
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize(
                (max_width, max(1, int(img.height * ratio))),
                Image.Resampling.LANCZOS
            )

        img = ImageEnhance.Contrast(img).enhance(2.0)
        img = img.filter(ImageFilter.SHARPEN)
        img.save(processed, format="PNG", optimize=True)

        result = subprocess.run(
            [
                "tesseract",
                processed,
                "stdout",
                "-l",
                "deu+eng",
                "--psm",
                str(psm)
            ],
            capture_output=True,
            text=True,
            timeout=90
        )

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "Tesseract failed"
            )

        return result.stdout


def read_scoreboard():
    data = fetch_image()

    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "source.png")
        with open(source, "wb") as f:
            f.write(data)

        img = Image.open(source).convert("RGB")

        # Hauptbereich: Spieler und Mannschaftsnamen
        full_text = run_tesseract(img, 6)

        # Unterer Bereich separat lesen. Dadurch werden Ergebnis/Punkte/Wurf
        # zuverlässiger erkannt als bei einem einzigen OCR-Lauf.
        h = img.height
        bottom = img.crop((0, int(h * 0.72), img.width, h))
        bottom_text = run_tesseract(bottom, 6)

        return full_text, bottom_text


def clean_text(text):
    text = text.replace("\u2014", "-")
    text = text.replace("\u2013", "-")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def parse_player_segment(segment):
    segment = clean_text(segment)
    nums = re.findall(r"(?<!\w)(\d{2,3}|\d+\.\d)(?!\w)", segment)

    # Erwartet: Satz1 Satz2 Satz3 Satz4 Total SaP
    if len(nums) < 6:
        return None

    try:
        sets = [int(x) for x in nums[-6:-2]]
        total = int(nums[-2])
        sap = float(nums[-1])
    except ValueError:
        return None

    # Name ist alles vor dem ersten Satzwert.
    first_num = re.search(r"(?<!\w)" + re.escape(nums[-6]) + r"(?!\w)", segment)
    if not first_num:
        return None

    name = segment[:first_num.start()].strip(" -|@}9")
    name = re.sub(r"\s+", " ", name).strip()

    if not name or len(name) < 3:
        return None

    return {
        "name": name,
        "sets": sets,
        "total": total,
        "sap": sap
    }


def split_player_line(line):
    """
    Eine OCR-Zeile enthält normalerweise Heim- und Gastspieler.
    Wir suchen zwei Gruppen aus je 6 Zahlen.
    """
    line = clean_text(line)

    matches = list(re.finditer(
        r"(\d{2,3})\s+(\d{2,3})\s+(\d{2,3})\s+(\d{2,3})\s+(\d{3})\s+(\d+\.\d)",
        line
    ))

    if len(matches) < 2:
        return None, None

    left_end = matches[0].end()
    right_start = matches[1].start()

    left = line[:left_end]
    right_prefix = line[left_end:right_start]

    # OCR-Artefakte zwischen beiden Spielern entfernen.
    right_prefix = re.sub(r"^[^A-Za-zÄÖÜäöüß]+", "", right_prefix).strip()
    right = right_prefix + " " + matches[1].group(0)

    return parse_player_segment(left), parse_player_segment(right)


def parse_team_names(text):
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    home = "KSV Weissach 1"
    away = "HKO Young Stars"

    if lines:
        top = lines[0]

        # Bekannte OCR-Fehler korrigieren.
        top = re.sub(r"(?i)^[@\s]*sv\s+weissach", "KSV Weissach", top)
        top = re.sub(r"(?i)ho\s+youg\s+stars", "HKO Young Stars", top)
        top = re.sub(r"(?i)hko\s+youg\s+stars", "HKO Young Stars", top)

        m = re.search(
            r"(KSV\s+Weissach\s*1?)\s+[@| ]+\s*(HKO?\s+Young\s+Stars)",
            top,
            re.I
        )
        if m:
            home = re.sub(r"\s+", " ", m.group(1)).strip()
            away = re.sub(r"\s+", " ", m.group(2)).strip()

    return home, away


def parse_players(text):
    home_players = []
    away_players = []

    for line in text.splitlines():
        if not re.search(r"\d{2,3}\s+\d{2,3}\s+\d{2,3}\s+\d{2,3}", line):
            continue

        home, away = split_player_line(line)

        if home:
            home_players.append(home)
        if away:
            away_players.append(away)

    return home_players[:4], away_players[:4]


def parse_bottom_numbers(text):
    """
    Liest nach Möglichkeit:
    Heim-Wurf, Heim-Ergebnis, Punkte Heim/Gast, Gast-Ergebnis, Gast-Wurf.
    Falls OCR einzelne Werte nicht erkennt, bleiben sie None.
    """
    cleaned = clean_text(text)
    result = {
        "home_throws": None,
        "home_total": None,
        "home_points": None,
        "away_points": None,
        "away_total": None,
        "away_throws": None,
        "difference": None
    }

    # Punkte, z.B. 4.0 : 2.0
    pm = re.search(r"(\d+(?:[.,]\d)?)\s*[:\-]\s*(\d+(?:[.,]\d)?)", cleaned)
    if pm:
        result["home_points"] = float(pm.group(1).replace(",", "."))
        result["away_points"] = float(pm.group(2).replace(",", "."))

    # Alle 3-4 stelligen Zahlen im unteren Bereich als Fallback.
    ints = [int(x) for x in re.findall(r"(?<![\d.])(\d{3,4})(?![\d.])", cleaned)]

    # Plausible Gesamtholz: typischerweise > 1000.
    totals = [x for x in ints if 1000 <= x <= 9999]
    if len(totals) >= 2:
        result["home_total"] = totals[0]
        result["away_total"] = totals[-1]

    # Wurfwerte typischerweise 0000-999, OCR verliert evtl. führende Null.
    throws = [x for x in ints if 0 <= x < 1000]
    if len(throws) >= 2:
        result["home_throws"] = throws[0]
        result["away_throws"] = throws[-1]

    if result["home_total"] is not None and result["away_total"] is not None:
        result["difference"] = result["home_total"] - result["away_total"]

    return result


def build_live_data():
    raw, bottom_raw = read_scoreboard()
    home_team, away_team = parse_team_names(raw)
    home_players, away_players = parse_players(raw)
    bottom = parse_bottom_numbers(bottom_raw)

    # Wenn die Gesamtergebnisse unten nicht erkannt werden, Summe der
    # erkannten Spieler verwenden. Das ist bei vollständigen 4 Spielern
    # zuverlässig.
    if bottom["home_total"] is None and len(home_players) == 4:
        bottom["home_total"] = sum(p["total"] for p in home_players)

    if bottom["away_total"] is None and len(away_players) == 4:
        bottom["away_total"] = sum(p["total"] for p in away_players)

    if bottom["home_total"] is not None and bottom["away_total"] is not None:
        bottom["difference"] = bottom["home_total"] - bottom["away_total"]

    # Falls Mannschaftspunkte nicht erkannt werden, aus SaP nicht erfinden.
    # Die Werte bleiben dann None.
    return {
        "success": True,
        "home": {
            "team": home_team,
            "players": home_players,
            "total": bottom["home_total"],
            "throws": bottom["home_throws"]
        },
        "away": {
            "team": away_team,
            "players": away_players,
            "total": bottom["away_total"],
            "throws": bottom["away_throws"]
        },
        "points": {
            "home": bottom["home_points"],
            "away": bottom["away_points"]
        },
        "difference": bottom["difference"],
        "players_detected": len(home_players) + len(away_players),
        "raw": raw,
        "bottom_raw": bottom_raw
    }


@app.route("/ocr")
def ocr():
    try:
        return jsonify(build_live_data())
    except subprocess.TimeoutExpired:
        return jsonify({
            "success": False,
            "error": "OCR timeout"
        }), 504
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/live")
def live():
    try:
        data = build_live_data()

        # Kompakter Endpoint für die Website.
        data.pop("raw", None)
        data.pop("bottom_raw", None)

        return jsonify(data)
    except subprocess.TimeoutExpired:
        return jsonify({
            "success": False,
            "error": "OCR timeout"
        }), 504
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
