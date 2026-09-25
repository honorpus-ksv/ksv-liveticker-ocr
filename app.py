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
        "ocr": "/ocr",
        "image": "/image"
    })


@app.route("/image")
def image():
    try:
        r = requests.get(SOURCE_URL, timeout=15)
        r.raise_for_status()

        return Response(
            r.content,
            content_type="image/png",
            headers={"Cache-Control": "no-store"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/ocr")
def ocr():
    try:
        r = requests.get(SOURCE_URL, timeout=15)
        r.raise_for_status()

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.png")
            processed = os.path.join(tmp, "processed.png")

            with open(source, "wb") as f:
                f.write(r.content)

            img = Image.open(source).convert("L")

            img = ImageEnhance.Contrast(img).enhance(2.0)
            img = img.filter(ImageFilter.SHARPEN)

            img.save(processed)

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
                timeout=30
            )

            text = result.stdout.strip()

            lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in text.splitlines()
                if line.strip()
            ]

            return jsonify({
                "success": True,
                "source": SOURCE_URL,
                "lines": lines,
                "raw": text
            })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
