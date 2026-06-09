from flask import Flask, request, send_file, render_template, session, jsonify
import subprocess
import os
import platform
from PIL import Image
import io
import zipfile
import uuid
import base64
import logging
from flask_session import Session
import requests

app = Flask(__name__)
app.logger.setLevel(logging.DEBUG)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SECRET_KEY"] = os.urandom(24)
Session(app)

if platform.system() == "Windows":
    ASTCENC_PATH = "./bin/astcenc-avx2.exe"
else:
    ASTCENC_PATH = "./bin/astcenc-avx2"

class RemoteFile:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    def save(self, dst_path: str):
        with open(dst_path, "wb") as f:
            f.write(self._data)

def convert_astc_bytes(astc_bytes, filename):
    """Convert raw ASTC bytes into PNG bytes using astcenc subprocess."""
    unique_id = str(uuid.uuid4())
    astc_path = f"temp_{unique_id}.astc"
    tga_path = f"temp_{unique_id}.tga"

    try:
        with open(astc_path, "wb") as f:
            f.write(astc_bytes)

        if not os.path.exists(ASTCENC_PATH):
            raise FileNotFoundError(f"astcenc binary not found at {ASTCENC_PATH}")

        profiles = ["-dl", "-ds", "-dh", "-dH"]
        for profile in profiles:
            try:
                subprocess.run(
                    [ASTCENC_PATH, profile, astc_path, tga_path],
                    check=True,
                    capture_output=True,
                    text=True
                )
                if os.path.exists(tga_path) and os.path.getsize(tga_path) > 0:
                    img = Image.open(tga_path)
                    png_buffer = io.BytesIO()
                    img.save(png_buffer, format="PNG")
                    png_buffer.seek(0)
                    return png_buffer.getvalue(), f"{os.path.splitext(filename)[0]}.png"
            except subprocess.CalledProcessError:
                continue
        raise RuntimeError("Failed to convert ASTC with any profile")
    finally:
        for path in [astc_path, tga_path]:
            if os.path.exists(path):
                os.remove(path)

@app.route("/fetch_id", methods=["POST"])
def fetch_id():
    item_id = request.form.get("item_id")
    if not item_id or not item_id.isdigit():
        return render_template("index.html", error="Invalid Item ID", results=[])

    url = f"https://dl.cdn.freefiremobile.com/live/ABHotUpdates/IconCDN/android/{item_id}_rgb.astc"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return render_template("index.html", error=f"Failed to fetch ASTC file (status {response.status_code})", results=[])

        unique_id = str(uuid.uuid4())
        astc_path = f"temp_{unique_id}.astc"
        with open(astc_path, "wb") as f:
            f.write(response.content)

        tga_path = f"temp_{unique_id}.tga"
        results = []
        try:
            profiles = ["-dl", "-ds", "-dh", "-dH"]
            for profile in profiles:
                try:
                    subprocess.run(
                        [ASTCENC_PATH, profile, astc_path, tga_path],
                        check=True,
                        capture_output=True,
                        text=True
                    )
                    if os.path.exists(tga_path) and os.path.getsize(tga_path) > 0:
                        img = Image.open(tga_path)
                        img.thumbnail((200, 200))
                        png_buffer = io.BytesIO()
                        img.save(png_buffer, format="PNG")
                        png_buffer.seek(0)

                        png_filename = f"{item_id}.png"
                        png_base64 = base64.b64encode(png_buffer.getvalue()).decode("utf-8")
                        results.append({
                            "filename": f"{item_id}_rgb.astc",
                            "png_filename": png_filename,
                            "png_base64": png_base64
                        })
                        break
                except subprocess.CalledProcessError:
                    continue
            if not results:
                results.append({"filename": f"{item_id}_rgb.astc", "error": "Failed to convert with any profile"})
        finally:
            for path in [astc_path, tga_path]:
                if os.path.exists(path):
                    os.remove(path)

        return render_template("index.html", results=results, zip_available=False, error=None)

    except Exception as e:
        return render_template("index.html", error=f"Error fetching file: {str(e)}", results=[])


@app.route("/", methods=["GET", "POST"])
def convert():
    if request.method == "GET":
        return render_template("index.html", results=[], zip_available=False, error=None)

    if request.method == "POST":
        files = request.files.getlist("files") if "files" in request.files else []

        item_id = (request.form.get("item_id") or "").strip()
        if item_id:
            if not item_id.isdigit():
                return render_template("index.html", error="Item ID must be numeric", results=[])
            cdn_url = f"https://dl.cdn.freefiremobile.com/live/ABHotUpdates/IconCDN/android/{item_id}_rgb.astc"
            try:
                resp = requests.get(cdn_url, timeout=10, headers={"User-Agent": "astc2png/1.0 (+https://github.com/yourname)"})
                if resp.status_code != 200:
                    return render_template("index.html", error=f"Failed to fetch item {item_id}: HTTP {resp.status_code}", results=[])
                content = resp.content

                if len(content) < 4 or content[:4] != b"\x13\xAB\xA1\x5C":
                    return render_template("index.html", error=f"Fetched file for item {item_id} is not a valid ASTC", results=[])
                
                remote_filename = f"{item_id}.astc"
                files.append(RemoteFile(remote_filename, content))
            except requests.RequestException as e:
                app.logger.error(f"Error fetching {cdn_url}: {str(e)}")
                return render_template("index.html", error=f"Error fetching item {item_id}: {str(e)}", results=[])

        if not files or all(getattr(f, "filename", "") == "" for f in files):
            return render_template("index.html", error="No files selected or fetched", results=[])

        results = []
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for file in files:
                filename = getattr(file, "filename", "")
                if not filename.endswith(".astc"):
                    results.append({"filename": filename, "error": "Invalid file format"})
                    continue

                unique_id = str(uuid.uuid4())
                astc_path = f"temp_{unique_id}.astc"
                tga_path = f"temp_{unique_id}.tga"

                try:
                    file.save(astc_path)

                    if not os.path.exists(ASTCENC_PATH):
                        return render_template("index.html", error=f"astcenc binary not found at {ASTCENC_PATH}", results=[])

                    if platform.system() != "Windows":
                        try:
                            os.chmod(ASTCENC_PATH, 0o755)
                            app.logger.info(f"Set executable permissions for {ASTCENC_PATH}")
                        except Exception as e:
                            app.logger.error(f"Failed to set permissions for {ASTCENC_PATH}: {str(e)}")

                    if not os.access(ASTCENC_PATH, os.X_OK):
                        return render_template("index.html", error=f"astcenc binary is not executable: {ASTCENC_PATH}", results=[])

                    with open(astc_path, "rb") as f_read:
                        if f_read.read(4) != b"\x13\xAB\xA1\x5C":
                            results.append({"filename": filename, "error": "Invalid ASTC file"})
                            continue

                    profiles = ["-dl", "-ds", "-dh", "-dH"]
                    tga_valid = False
                    for profile in profiles:
                        try:
                            result = subprocess.run(
                                [ASTCENC_PATH, profile, astc_path, tga_path],
                                check=True,
                                capture_output=True,
                                text=True
                            )
                            app.logger.info(f"astcenc output for {filename} with {profile}: {result.stdout}")

                            if not os.path.exists(tga_path) or os.path.getsize(tga_path) == 0:
                                results.append({"filename": filename, "error": f"No output produced for {profile}"})
                                continue

                            img = Image.open(tga_path)
                            if img.size == (0, 0):
                                results.append({"filename": filename, "error": f"Empty image produced for {profile}"})
                                continue

                            img.thumbnail((200, 200))
                            png_buffer = io.BytesIO()
                            img.save(png_buffer, format="PNG")
                            png_buffer.seek(0)

                            png_filename = f"{os.path.splitext(filename)[0]}.png"
                            zip_file.writestr(png_filename, png_buffer.getvalue())

                            png_base64 = base64.b64encode(png_buffer.getvalue()).decode("utf-8")
                            results.append({
                                "filename": filename,
                                "png_filename": png_filename,
                                "png_base64": png_base64
                            })
                            tga_valid = True
                            break
                        except subprocess.CalledProcessError as e:
                            app.logger.error(f"astcenc failed for {filename} with {profile}: {e.stderr}")
                            continue

                    if not tga_valid:
                        results.append({"filename": filename, "error": "Failed to convert with any profile"})
                except Exception as e:
                    app.logger.error(f"Error processing {filename}: {str(e)}")
                    results.append({"filename": filename, "error": f"Error: {str(e)}"})
                finally:
                    for path in [astc_path, tga_path]:
                        if os.path.exists(path):
                            try:
                                os.remove(path)
                            except Exception as e:
                                app.logger.warning(f"Failed to remove {path}: {e}")

        if any("error" not in r for r in results):
            session["zip_buffer"] = zip_buffer.getvalue()
            return render_template(
                "index.html",
                results=results,
                zip_available=True,
                error=None
            )
        else:
            return render_template("index.html", results=results, zip_available=False, error="No valid ASTC files processed")

@app.route("/download_zip")
def download_zip():
    if "zip_buffer" not in session:
        return "No files to download", 400
    zip_data = session.pop("zip_buffer")
    return send_file(
        io.BytesIO(zip_data),
        mimetype="application/zip",
        as_attachment=True,
        download_name="converted_pngs.zip"
    )

@app.route("/api/convert", methods=["POST"])
def api_convert():
    """Upload one or more .astc files → PNG/ZIP"""
    if "files" not in request.files:
        return jsonify({"error": "No files uploaded"}), 400

    files = request.files.getlist("files")
    output_files = []

    for file in files:
        try:
            png_data, png_name = convert_astc_bytes(file.read(), file.filename)
            output_files.append((png_name, png_data))
        except Exception as e:
            return jsonify({"error": f"{file.filename}: {str(e)}"}), 400

    if len(output_files) == 1:
        png_name, png_data = output_files[0]
        return send_file(
            io.BytesIO(png_data),
            mimetype="image/png",
            as_attachment=True,
            download_name=png_name
        )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in output_files:
            zf.writestr(name, data)
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name="converted.zip"
    )

@app.route("/api/fetch", methods=["GET"])
def api_fetch():
    """Fetch ASTC by item_id from CDN → PNG"""
    item_id = request.args.get("item_id")
    if not item_id or not item_id.isdigit():
        return jsonify({"error": "Missing or invalid item_id"}), 400

    url = f"https://dl.cdn.freefiremobile.com/live/ABHotUpdates/IconCDN/android/{item_id}_rgb.astc"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        return jsonify({"error": f"Failed to fetch ASTC file (HTTP {resp.status_code})"}), 404

    try:
        png_data, png_name = convert_astc_bytes(resp.content, f"{item_id}.astc")
        return send_file(
            io.BytesIO(png_data),
            mimetype="image/png",
            as_attachment=True,
            download_name=png_name
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)