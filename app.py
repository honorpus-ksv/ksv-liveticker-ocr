from flask import Flask, jsonify, Response
import requests
import subprocess
import tempfile
import os
import re
from PIL import Image, ImageEnhance, ImageFilter

app = Flask(__name__)

SOURCE_URL = "http://ksv-weissach.host4free.de/Kegelbahn/Index.png"


# ---------------------------------------------------------
# STARTSEITE
# ---------------------------------------------------------

@app.route("/")
def home():
    return jsonify({
        "service": "KSV Weissach Liveticker OCR",
        "status": "online",
        "image": "/image",
        "ocr": "/ocr",
        "live": "/live"
    })


# ---------------------------------------------------------
# ORIGINALBILD
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# GEMEINSAME OCR-FUNKTION
# ---------------------------------------------------------

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

        # Bild speichern
        with open(source, "wb") as f:
            f.write(r.content)

        # Bild laden
        img = Image.open(source).convert("L")

        # Außenbereiche entfernen
        bbox = img.getbbox()

        if bbox:
            img = img.crop(bbox)

        # Bildgröße begrenzen
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

        # Kontrast verbessern
        img = ImageEnhance.Contrast(img).enhance(2.0)

        # Schärfen
        img = img.filter(ImageFilter.SHARPEN)

        # Speichern
        img.save(
            processed,
            format="PNG"
        )

        # Tesseract OCR
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

            raise RuntimeError(
                result.stderr
            )

        return result.stdout


# ---------------------------------------------------------
# ROHE OCR-AUSGABE
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# SPIELER AUS OCR-TEXT ERKENNEN
# ---------------------------------------------------------

def parse_players(text):

    pattern = re.compile(
        r"([A-Za-zÄÖÜäöüß\- ]+?)\s+"
        r"(\d{2,3})\s+"
