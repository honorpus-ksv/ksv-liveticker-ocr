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
            timeout=10,
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
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


def read_scoreboard():
    r = requests.get(
        SOURCE_URL,
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    r.raise_for_status()

    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "source.png")
        processed = os.path.join(tmp, "processed.png")

        with open(source, "wb") as f:
            f.write(r.content)

        img = Image.open(source).convert("L")

        bbox = img.getbbox()

        if bbox:
            img = img.crop(bbox)

        max_width = 1200

        if img.width > max_width:
            ratio = max_width / img.width

            img = img.resize(
                (
                    max_width,
                    int(img.height * ratio)
                ),
                Image.Resampling.LANCZOS
            )

        img = ImageEnhance.Contrast(img).enhance(2.0)
        img = img.filter(ImageFilter.SHARPEN)

        img.save(processed, format="PNG")

        result = subprocess.run(
            [
                "tesseract",
                processed,
                "stdout",
                "-l",
                "deu+eng",
                "--psm",
                "6"
            ],
            capture_output=True,
            text=True,
            timeout=90
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr)

        return result.stdout


@app.route("/ocr")
def ocr():
    try:
        text = read_scoreboard()

        return jsonify({
            "success": True,
            "text": text
        })

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


def parse_players(text):
    pattern = re.compile(r"([A-Za-zÄÖÜäöüß\- ]+?)\s+(\d{2,3})\s+(\d{2,3})\s+(\d{2,3})\s+(\d{2,3})\s+(\d{3})\s+(\d(?:[\.,]\d))")

    matches = pattern.findall(text)

    players = []

    for match in matches:
        name = match[0].strip()

        if len(name) < 2:
            continue

        players.append({
            "name": name,
            "sets": [
                int(match[1]),
                int(match[2]),
                int(match[3]),
                int(match[4])
            ],
            "total": int(match[5]),
            "sap": float(match[6].replace(",", "."))
        })

    return players


def detect_teams(text):
    home_team = "KSV Weissach 1"
    away_team = "HKO Youg Stars"

    home_match = re.search(
        r"KSV\s+Weissach\s*\d*",
        text,
        re.IGNORECASE
    )

    if home_match:
        home_team = home_match.group(0).strip()

    away_match = re.search(
        r"HKO\s+\w+\s+Stars",
        text,
        re.IGNORECASE
    )

    if away_match:
        away_team = away_match.group(0).strip()

    return home_team, away_team


def calculate_total(players):
    return sum(
        player["total"]
        for player in players
    )


@app.route("/live")
def live():
    try:
        text = read_scoreboard()

        players = parse_players(text)

        home_team, away_team = detect_teams(text)

        home_players = players[0::2]
        away_players = players[1::2]

        home_total = calculate_total(home_players)
        away_total = calculate_total(away_players)

        return jsonify({
            "success": True,

            "home": {
                "team": home_team,
                "players": home_players,
                "total": home_total
            },

            "away": {
                "team": away_team,
                "players": away_players,
                "total": away_total
            },

            "difference": home_total - away_total,

            "players_detected": len(players),

            "raw": text
        })

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

    app.run(
        host="0.0.0.0",
        port=port
    )
