import os
import io
import uuid
import random
from datetime import datetime

from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from pymongo import MongoClient, DESCENDING
from bson import ObjectId

from PIL import Image

# ---------- APP CONFIG ----------
app = Flask(__name__)
CORS(app)

limiter = Limiter(get_remote_address, app=app, default_limits=["10/minute"])

# ---------- MONGODB CONFIG ----------
MONGO_URI       = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME   = os.getenv("MONGO_DB_NAME", "flood_map")
MONGO_COLL_NAME = os.getenv("MONGO_COLLECTION", "flood_reports")

mongo_client = MongoClient(MONGO_URI)
db_mongo     = mongo_client[MONGO_DB_NAME]
reports_coll = db_mongo[MONGO_COLL_NAME]

# Index for the "newest-first" query used by /reports
reports_coll.create_index([("timestamp", DESCENDING)])

# ---------- S3 CONFIG ----------
S3_BUCKET     = os.getenv("S3_BUCKET_NAME")
S3_REGION     = os.getenv("AWS_REGION", "ap-south-1")
S3_PREFIX     = os.getenv("S3_KEY_PREFIX", "flood-reports/")  # object-key folder
# Optional public base URL — set this if you serve images via CloudFront
# or a custom domain. If unset we build a standard S3 URL.
S3_PUBLIC_BASE = os.getenv("S3_PUBLIC_BASE", "").rstrip("/")

# Credentials come from the EC2 instance role via the metadata service.
# boto3's default credential chain (env vars → shared credentials → instance
# role) handles this automatically — no explicit keys needed.
s3_client = boto3.client("s3", region_name=S3_REGION)


def s3_public_url(key: str) -> str:
    if S3_PUBLIC_BASE:
        return f"{S3_PUBLIC_BASE}/{key}"
    # Standard virtual-hosted-style URL
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"


# ---------- UTILITY FUNCTIONS ----------

def strip_metadata(image_file):
    """
    Strips EXIF and other metadata from an uploaded image file stream.
    Returns a BytesIO stream ready to be read by boto3.upload_fileobj.
    Also returns the detected/normalised format (upper-case, e.g. 'JPEG').
    """
    img_stream = io.BytesIO(image_file.read())
    img_stream.seek(0)

    try:
        img = Image.open(img_stream)
        fmt = (img.format or "JPEG").upper()

        output_stream = io.BytesIO()
        img.save(output_stream, format=fmt, exif=b"")
        output_stream.seek(0)
        return output_stream, fmt

    except Exception as e:
        print(f"Error during image metadata stripping, returning original stream: {e}")
        image_file.seek(0)
        return image_file, "JPEG"


def content_type_for(fmt: str) -> str:
    fmt = fmt.upper()
    return {
        "JPEG": "image/jpeg",
        "JPG":  "image/jpeg",
        "PNG":  "image/png",
        "WEBP": "image/webp",
        "GIF":  "image/gif",
    }.get(fmt, "application/octet-stream")


def extension_for(fmt: str) -> str:
    fmt = fmt.upper()
    return {"JPEG": "jpg", "JPG": "jpg", "PNG": "png",
            "WEBP": "webp", "GIF": "gif"}.get(fmt, "bin")


def upload_to_s3(image_stream: io.BytesIO, fmt: str) -> str:
    """Uploads the sanitised image to S3 and returns the public URL."""
    key = f"{S3_PREFIX}{uuid.uuid4().hex}.{extension_for(fmt)}"
    s3_client.upload_fileobj(
        image_stream,
        S3_BUCKET,
        key,
        ExtraArgs={
            "ContentType": content_type_for(fmt),
            "CacheControl": "public, max-age=31536000, immutable",
            # If your bucket uses ACLs, uncomment the next line and make sure
            # BlockPublicAcls is disabled on the bucket. Otherwise rely on
            # the bucket policy for public read access.
            # "ACL": "public-read",
        },
    )
    return s3_public_url(key)


def _iso(value):
    """
    Normalise a datetime-ish field for the JSON response.
    Handles: None, real datetime objects, and legacy string values that
    were written before the schema was cleaned up.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        # Already stored as a string — return as-is. It's the frontend's
        # job to parse; safeDate() in index.html already handles this.
        return value
    # Anything else (int/float epoch, etc.) — coerce to string as a last resort.
    return str(value)


def serialize_report(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "lat": doc.get("lat"),
        "lng": doc.get("lng"),
        "desc": doc.get("desc"),
        "image": doc.get("image_url"),
        "flood_datetime":    _iso(doc.get("flood_datetime")),
        "flood_depth_cm":    doc.get("flood_depth_cm"),
        "flood_depth_label": doc.get("flood_depth_label"),
        "flood_depth_asset": doc.get("flood_depth_asset"),
        "timestamp":         _iso(doc.get("timestamp")),
    }


# ---------- ROUTES ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/reports")
def reports():
    cursor = reports_coll.find({}).sort("timestamp", DESCENDING)
    return jsonify([serialize_report(doc) for doc in cursor])


@app.route("/submit", methods=["POST"])
@limiter.limit("5/minute")
def submit():
    try:
        raw_lat = request.form.get("lat", "").strip()
        raw_lng = request.form.get("lng", "").strip()

        if not raw_lat or not raw_lng:
            return jsonify({"error": "Location is required. Please pin a location on the map."}), 400

        try:
            lat = float(raw_lat)
            lng = float(raw_lng)
        except ValueError:
            return jsonify({"error": "Invalid location data."}), 400

        # Obfuscate coordinates (~11 m random shift)
        lat += (random.random() - 0.5) * 0.0002
        lng += (random.random() - 0.5) * 0.0002

        desc = request.form.get("desc", "")[:255]

        flood_depth_cm_str = request.form.get("flood_depth_cm", "").strip()
        flood_depth_cm = int(flood_depth_cm_str) if flood_depth_cm_str.isdigit() else None
        flood_depth_label = request.form.get("flood_depth_label", "").strip()[:64] or None
        flood_depth_asset = request.form.get("flood_depth_asset", "").strip()[:16] or None

        flood_datetime_str = request.form.get("flood_datetime", "").strip()
        flood_datetime = None
        if flood_datetime_str:
            try:
                flood_datetime = datetime.strptime(flood_datetime_str, "%Y-%m-%dT%H:%M")
            except ValueError:
                pass

        image_file = request.files.get("image")
        if not image_file:
            return jsonify({"error": "No image uploaded"}), 400

        # Strip metadata and upload to S3
        sanitized_stream, fmt = strip_metadata(image_file)
        try:
            image_url = upload_to_s3(sanitized_stream, fmt)
        except (BotoCoreError, ClientError) as e:
            print(f"S3 upload failed: {e}")
            return jsonify({"error": "Image upload failed. Please try again."}), 502

        # Insert into MongoDB
        doc = {
            "lat": lat,
            "lng": lng,
            "desc": desc,
            "image_url": image_url,
            "flood_datetime": flood_datetime,
            "flood_depth_cm": flood_depth_cm,
            "flood_depth_label": flood_depth_label,
            "flood_depth_asset": flood_depth_asset,
            "timestamp": datetime.utcnow(),
        }
        reports_coll.insert_one(doc)

        return jsonify({"message": "Flood report added successfully!"})

    except Exception as e:
        print(f"SERVER ERROR during submit: {e}")
        return jsonify({"error": "An internal server error occurred. Check server logs for details."}), 500


@app.route("/delete/<string:id>", methods=["DELETE"])
def delete_report(id):
    # Kept as a stub, matching the original file. Wire this up when needed.
    # Example implementation:
    #   try:
    #       oid = ObjectId(id)
    #   except Exception:
    #       return jsonify({"error": "Invalid id"}), 400
    #   doc = reports_coll.find_one_and_delete({"_id": oid})
    #   if not doc:
    #       return jsonify({"error": "Not found"}), 404
    #   # Optionally delete the S3 object here.
    #   return jsonify({"message": "Deleted"})
    pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
