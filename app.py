import os
from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
from datetime import datetime
import cloudinary
import cloudinary.uploader
from PIL import Image
import io
import random

# ---------- APP CONFIG ----------
app = Flask(__name__)
CORS(app)

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///flood_reports.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

limiter = Limiter(get_remote_address, app=app, default_limits=["10/minute"])

# ---------- CLOUDINARY CONFIG ----------
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# ---------- DATABASE MODEL ----------
class FloodReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lat = db.Column(db.Float, nullable=False)
    lng = db.Column(db.Float, nullable=False)
    desc = db.Column(db.String(255))
    image_url = db.Column(db.String(255))
    flood_datetime = db.Column(db.DateTime, nullable=True)
    flood_depth_cm = db.Column(db.Integer, nullable=True)
    flood_depth_label = db.Column(db.String(64), nullable=True)
    flood_depth_asset = db.Column(db.String(16), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# Ensure database tables are created (run this once)
with app.app_context():
    db.create_all()

# ---------- UTILITY FUNCTIONS ----------

def obfuscate_coord(coord_str):
    """Obfuscates coordinates by applying a small, random shift."""
    try:
        coord = float(coord_str)
        # Add a tiny random offset (up to 0.0001 degrees, about 11 meters)
        shift = (random.random() - 0.5) * 0.0002
        return coord + shift
    except (ValueError, TypeError):
        return None

def strip_metadata(image_file):
    """
    Strips EXIF and other metadata from an image file stream.

    FIX: Ensures the file is read fully into memory, stripped, and the output
    stream pointer is reset (seek(0)) for Cloudinary to read it fully.
    """
    # 1. Read the file stream content into an in-memory BytesIO object
    # This prevents issues with the underlying Werkzeug FileStorage being read partially.
    img_stream = io.BytesIO(image_file.read())
    img_stream.seek(0)
    
    try:
        # 2. Open the image using PIL
        img = Image.open(img_stream)
        
        # 3. Create a new BytesIO object to hold the stripped image data
        output_stream = io.BytesIO()
        
        # 4. Save the image without EXIF. The format is explicitly preserved.
        img.save(output_stream, format=img.format if img.format else 'JPEG', exif=False)
        
        # 5. Reset the pointer to the beginning of the stream for the uploader (CRITICAL STEP)
        output_stream.seek(0)
        return output_stream
    
    except Exception as e:
        # If PIL fails to process (e.g., corrupted file), log error but return original stream
        print(f"Error during image metadata stripping, returning original stream: {e}")
        image_file.seek(0) # Reset original file pointer as a fallback
        return image_file

# ---------- ROUTES ----------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/reports")
def reports():
    reports = FloodReport.query.order_by(FloodReport.timestamp.desc()).all()
    return jsonify([
        {
            "id": r.id,
            "lat": r.lat,
            "lng": r.lng,
            "desc": r.desc,
            "image": r.image_url,
            "flood_datetime": r.flood_datetime.isoformat() if r.flood_datetime else None,
            "flood_depth_cm": r.flood_depth_cm,
            "flood_depth_label": r.flood_depth_label,
            "flood_depth_asset": r.flood_depth_asset,
            "timestamp": r.timestamp.isoformat()
        } for r in reports
    ])

@app.route("/submit", methods=["POST"])
@limiter.limit("5/minute")
def submit():
    try:
        raw_lat = request.form.get("lat", "").strip()
        raw_lng = request.form.get("lng", "").strip()

        # 1. Check not empty
        if not raw_lat or not raw_lng:
            return jsonify({"error": "Location is required. Please pin a location on the map."}), 400

        # 2. Validate they are real numbers
        try:
            lat = float(raw_lat)
            lng = float(raw_lng)
        except ValueError:
            return jsonify({"error": "Invalid location data."}), 400

        # 3. Obfuscate (inline, no helper needed)
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
    
        try:
            lat = float(lat)
            lng = float(lng)
        except ValueError:
            return jsonify({"error": "Invalid location data."}), 400
        
        # Strip metadata and prepare the file stream for upload
        sanitized_image_stream = strip_metadata(image_file)

        # Upload to Cloudinary
        upload_result = cloudinary.uploader.upload(
            sanitized_image_stream, # Use the reset stream
            resource_type="image",
            invalidate=True,
            exif=False
        )
        image_url = upload_result["secure_url"]

        new_report = FloodReport(lat=lat, lng=lng, desc=desc, image_url=image_url, flood_datetime=flood_datetime, flood_depth_cm=flood_depth_cm, flood_depth_label=flood_depth_label, flood_depth_asset=flood_depth_asset)
        db.session.add(new_report)
        db.session.commit()

        return jsonify({"message": "Flood report added successfully!"})
    except Exception as e:
        # Log the full error to your Render logs
        print(f"SERVER ERROR during submit: {e}")
        # Return a standard 500 error to the client
        return jsonify({"error": "An internal server error occurred. Check server logs for details."}), 500

@app.route("/delete/<int:id>", methods=["DELETE"])
def delete_report(id):
    # This route is not modified
    pass
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
