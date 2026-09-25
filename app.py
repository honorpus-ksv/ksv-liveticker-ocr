from flask import Flask, jsonify, Response
import requests
import subprocess
import tempfile
import os
from PIL import Image, ImageEnhance, ImageFilter

app = Flask(__name__)

SOURCE_URL = "http://ksv-weissach.host4free.de/Kegelbahn/Index.png"


@app.route("/")
def home():
    return jsonify({
        "service": "KSV Weissach Liveticker OCR",
        "status": "online",
        "image": "/image",
        "ocr": "/ocr"
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


@app.route("/ocr")
def ocr():
    try:
        # Aktuelles Bild vom KSV-Server laden
        r = requests.get(
            SOURCE_URL,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.png")
            processed = os.path.join(tmp, "processed.png")

            # Originalbild speichern
            with open(source, "wb") as f:
                f.write(r.content)

            # Bild laden und in Graustufen umwandeln
            img = Image.open(source).convert("L")

            # Leere/schwarze Außenbereiche entfernen
            bbox = img.getbbox()

            if bbox:
                img = img.crop(bbox)

            # Bildgröße für Render begrenzen
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

            # Kontrast erhöhen
            img = ImageEnhance.Contrast(img).enhance(2.0)

            # Bild leicht schärfen
            img = img.filter(ImageFilter.SHARPEN)

            # Für Tesseract speichern
            img.save(processed, format="PNG")

            # OCR durchführen
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
                return jsonify({
                    "success": False,
                    "error": result.stderr
                }), 500

            text = result.stdout

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=port
    )
