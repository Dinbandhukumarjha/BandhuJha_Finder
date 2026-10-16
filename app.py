
import os
import uuid
import base64
import logging
from math import isfinite

import requests
from flask import Flask, jsonify, render_template, request
from supabase import create_client

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

logging.basicConfig(level=logging.INFO)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = "bandhujha2517@gmail.com"
BUCKET = "saved-items"

supabase = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def json_error(message, status=400):
    return jsonify({"success": False, "message": message}), status


def get_user():
    """Verify the Supabase access token and return its user."""
    if not supabase:
        return None, "Supabase server configuration is missing."

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, "Please log in first."

    token = header[7:].strip()
    if not token:
        return None, "Access token is missing."

    try:
        response = supabase.auth.get_user(token)
        return response.user, None
    except Exception:
        return None, "Your session is invalid or expired."


def photo_signature_ok(content_type, data):
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return (
            len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        )
    return False


def send_email(item_name, notes, lat, lon, photo_data, extension):
    """Send a notification through Resend HTTPS API."""
    if not (RESEND_API_KEY and EMAIL_FROM):
        return False

    maps_url = f"https://maps.google.com/?q={lat},{lon}"
    attachment_name = f"saved-item{extension}"

    payload = {
        "from": EMAIL_FROM,
        "to": [EMAIL_TO],
        "subject": f"New Item Saved - {item_name}",
        "html": (
            "<h2>Bandhu Jha PWA</h2>"
            "<p>A new item was saved in your Personal Item Finder.</p>"
            f"<p><b>Item:</b> {item_name}</p>"
            f"<p><b>Notes:</b> {notes or 'None'}</p>"
            f"<p><b>Latitude:</b> {lat}</p>"
            f"<p><b>Longitude:</b> {lon}</p>"
            f'<p><a href="{maps_url}">Open location in Google Maps</a></p>'
        ),
        "attachments": [{
            "filename": attachment_name,
            "content": base64.b64encode(photo_data).decode("ascii"),
        }],
    }

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if not response.ok:
            logging.error("Resend error: %s", response.text[:500])
        return response.ok
    except requests.RequestException:
        logging.exception("Email request failed")
        return False


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "app": "Bandhu Jha PWA"})


@app.post("/save-item")
def save_item():
    user, error = get_user()
    if error:
        return json_error(error, 401)

    if not SUPABASE_URL or not RESEND_API_KEY:
        return json_error(
            "Server setup incomplete. Configure Supabase and Resend first.",
            503,
        )

    name = request.form.get("item_name", "").strip()
    notes = request.form.get("notes", "").strip()
    photo = request.files.get("photo")

    if not name or len(name) > 100:
        return json_error("Item name is required (maximum 100 characters).")
    if len(notes) > 500:
        return json_error("Notes must be 500 characters or fewer.")
    if not photo:
        return json_error("Please select an item photo.")

    content_type = (photo.mimetype or "").lower()
    extension = ALLOWED_TYPES.get(content_type)
    if not extension:
        return json_error("Only JPEG, PNG and WebP photos are allowed.")

    photo_data = photo.read()
    if not photo_data or len(photo_data) > 4 * 1024 * 1024:
        return json_error("Photo must be smaller than 4 MB.")
    if not photo_signature_ok(content_type, photo_data):
        return json_error("The selected file is not a valid supported image.")

    try:
        lat = float(request.form.get("lat", ""))
        lon = float(request.form.get("lon", ""))
    except (ValueError, TypeError):
        return json_error("Valid GPS coordinates are required.")

    if (
        not isfinite(lat) or not isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        return json_error("GPS coordinates are outside the valid range.")

    user_id = str(user.id)
    photo_path = f"{user_id}/{uuid.uuid4().hex}{extension}"

    try:
        supabase.storage.from_(BUCKET).upload(
            photo_path,
            photo_data,
            {"content-type": content_type, "upsert": "false"},
        )

        result = supabase.table("saved_items").insert({
            "user_id": user_id,
            "item_name": name,
            "notes": notes,
            "latitude": lat,
            "longitude": lon,
            "photo_path": photo_path,
        }).execute()

        row = result.data[0] if result.data else {}
    except Exception:
        logging.exception("Failed to save item")
        try:
            supabase.storage.from_(BUCKET).remove([photo_path])
        except Exception:
            logging.exception("Photo cleanup failed")
        return json_error("Could not save item. Check Supabase setup.", 500)

    email_sent = send_email(name, notes, lat, lon, photo_data, extension)

    return jsonify({
        "success": True,
        "item": {
            "id": row.get("id"),
            "item_name": name,
            "notes": notes,
            "latitude": lat,
            "longitude": lon,
        },
        "email_sent": email_sent,
        "message": (
            "Item saved. Email request accepted."
            if email_sent
            else "Item saved, but email was not sent successfully."
        ),
    }), 201


@app.get("/my-items")
def my_items():
    user, error = get_user()
    if error:
        return json_error(error, 401)

    try:
        result = (
            supabase.table("saved_items")
            .select(
                "id,item_name,notes,latitude,longitude,photo_path,created_at"
            )
            .eq("user_id", str(user.id))
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        items = []
        for row in result.data or []:
            try:
                signed = supabase.storage.from_(BUCKET).create_signed_url(
                    row["photo_path"], 300
                )
                row["photo_url"] = signed.get("signedURL") or signed.get(
                    "signedUrl", ""
                )
            except Exception:
                logging.exception("Could not sign photo URL")
                row["photo_url"] = ""

            row.pop("photo_path", None)
            items.append(row)

        return jsonify({"success": True, "items": items})
    except Exception:
        logging.exception("Could not load gallery")
        return json_error("Could not load items. Check Supabase setup.", 500)


@app.errorhandler(413)
def too_large(_error):
    return json_error("Request too large. Please use a smaller photo.", 413)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
