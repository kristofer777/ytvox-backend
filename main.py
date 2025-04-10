# --- Imports ---
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from uuid import uuid4
from pathlib import Path
from threading import Thread
import os
import re
import shutil
import subprocess
import time
from yt_dlp import YoutubeDL
from fastapi.staticfiles import StaticFiles

# ---> ADD THIS IMPORT FOR CORS <---
from fastapi.middleware.cors import CORSMiddleware

# --- FastAPI App Initialization ---
app = FastAPI()

# ---> ADD CORS MIDDLEWARE CONFIGURATION <---
# IMPORTANT: Replace 'YOUR_REAL_EXTENSION_ID_HERE' with the actual ID
#            of your Chrome extension (find it in chrome://extensions/)
#            The format MUST be exactly "chrome-extension://<ID>"
chrome_extension_origin = "chrome-extension://modaahafjnllmkaabcinemclpbbklgnn"

origins = [
    chrome_extension_origin,
    # You can add other origins here if needed, e.g., for local testing:
    # "http://localhost",
    # "http://127.0.0.1",
    # "http://localhost:8080", # Example if you had a local web UI test page
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,      # List of allowed origins
    allow_credentials=True,     # Allow cookies (optional, but often useful)
    allow_methods=["*"],        # Allow all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],        # Allow all headers
)
# ---> END OF CORS SECTION <---


# --- Global Variables & Setup ---
# Use a subdirectory for downloads. Render ephemeral storage or mounted disk.
DOWNLOADS = Path("./download_data")
DOWNLOADS.mkdir(exist_ok=True) # Create the directory if it doesn't exist

# In-memory job store (will be lost on server restart unless using persistent storage)
job_store = {}

# --- Pydantic Models ---
class ExtractRequest(BaseModel):
    url: str

# --- API Endpoints ---
@app.post("/extract")
def start_extraction(request: ExtractRequest):
    """Starts the extraction process in a background thread."""
    job_id = uuid4().hex[:6]
    job_store[job_id] = {
        "status": "queued",
        "progress": 0,
        "start_time": time.time(),
        "url": request.url,
        "result_url": None,
        "message": None
    }
    thread = Thread(target=process_acapella, args=(request.url, job_id))
    thread.start()
    # Return the initial job status along with the ID
    return job_store[job_id] | {"job_id": job_id} # Python 3.9+ dict merge

@app.get("/progress/{job_id}")
def get_progress(job_id: str):
    """Gets the progress/status of a specific job."""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/health")
def health_check():
    """Simple health check endpoint for Render."""
    return {"status": "ok"}

# --- Static File Serving (Optional, requires persistent disk on Render) ---
# Mount the downloads directory to be accessible via /downloads URL path
# Note: Files are ephemeral unless using Render Disks.
if DOWNLOADS.is_dir():
    try:
        app.mount("/downloads", StaticFiles(directory=DOWNLOADS), name="downloads")
        print(f"Serving static files from {DOWNLOADS} at /downloads")
    except RuntimeError as e:
         print(f"Could not mount static directory (may already be mounted): {e}")
else:
    print(f"Warning: Directory {DOWNLOADS} not found for static file serving.")


# --- Background Processing Logic ---
def process_acapella(link, job_id):
    """Downloads audio, runs Spleeter, and cleans up."""
    output_wav = None # Initialize to None
    stem_folder_path = None # Initialize to None
    try:
        job_store[job_id]["status"] = "downloading"
        job_store[job_id]["progress"] = 5 # Small initial progress
        print(f"Job {job_id}: Starting download for {link}")

        ID = uuid4().hex[:6]
        output_wav = DOWNLOADS / f"track_{ID}.wav" # Define path early for cleanup

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(DOWNLOADS / f"track_{ID}.%(ext)s"),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
                'preferredquality': '192',
            }],
            'postprocessor_args': ['-ar', '44100'],
            'quiet': True,
            'progress_hooks': [lambda d: update_download_progress(d, job_id)],
            'noplaylist': True,
            # Consider adding download limits
            # 'max_filesize': '100m',
            # 'match_filter': 'duration < 600', # Max 10 minutes
        }

        title = f"audio_{ID}"
        with YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(link, download=True)
                title = info.get('title', f"audio_{ID}")
                print(f"Job {job_id}: Download finished. Title: {title}")
            except Exception as ydl_error:
                print(f"Error during yt-dlp download/extraction for job {job_id}: {ydl_error}")
                # Provide more specific error if possible
                error_message = f"yt-dlp failed: {str(ydl_error)[:200]}" # Limit error message length
                raise ValueError(error_message)


        job_store[job_id]["status"] = "processing"
        job_store[job_id]["progress"] = 50 # Update progress after download

        clean_title = re.sub(r'[^\w\s().-]', '', title).strip().replace(" ", "_")
        if not clean_title: clean_title = f"audio_{ID}"
        final_output_filename = f"{clean_title}_ACAPELLA.wav"
        final_output_path = DOWNLOADS / final_output_filename

        stem_folder_name = output_wav.stem
        stem_folder_path = DOWNLOADS / stem_folder_name # Define path early
        expected_vocals_path = stem_folder_path / "vocals.wav"

        print(f"Job {job_id}: Running Spleeter on {output_wav}...")
        spleeter_command = [
            "spleeter", "separate", "-p", "spleeter:2stems",
            "-o", str(DOWNLOADS), str(output_wav)
        ]

        result = subprocess.run(spleeter_command, capture_output=True, text=True, check=False)

        if result.returncode != 0:
            error_detail = result.stderr[:500].strip() # Get first 500 chars of error
            print(f"Spleeter failed for job {job_id}. Code: {result.returncode}. Error: {error_detail}")
            raise RuntimeError(f"Spleeter processing failed: {error_detail}")

        print(f"Job {job_id}: Spleeter finished. Checking for {expected_vocals_path}...")

        if not expected_vocals_path.exists():
            print(f"Error for job {job_id}: Expected vocals file not found at {expected_vocals_path}")
            raise FileNotFoundError(f"Spleeter output vocals.wav not found in {stem_folder_path}")

        shutil.move(str(expected_vocals_path), final_output_path)
        print(f"Job {job_id}: Vocals moved to {final_output_path}")

        # --- Update job store upon success ---
        job_store[job_id]["progress"] = 100
        job_store[job_id]["status"] = "done"
        # Provide a relative URL path for downloading (requires StaticFiles mount)
        job_store[job_id]["result_url"] = f"/downloads/{final_output_filename}"
        job_store[job_id]["message"] = "Extraction successful."
        print(f"Job {job_id}: Successfully completed.")

    except Exception as e:
        print(f"Error processing job {job_id}: {e}")
        if job_id in job_store: # Ensure job still exists
            job_store[job_id]["status"] = "error"
            # Limit message length for display
            job_store[job_id]["message"] = str(e)[:500]
            job_store[job_id]["progress"] = 0 # Reset progress on error

    finally:
        # --- Cleanup ---
        print(f"Job {job_id}: Cleaning up intermediate files...")
        try:
            if stem_folder_path and stem_folder_path.exists():
                shutil.rmtree(stem_folder_path)
                print(f"Job {job_id}: Removed directory {stem_folder_path}")
            if output_wav and output_wav.exists():
                os.remove(output_wav)
                print(f"Job {job_id}: Removed file {output_wav}")
        except Exception as cleanup_error:
            print(f"Error during cleanup for job {job_id}: {cleanup_error}")


def update_download_progress(d, job_id):
    """Hook for yt-dlp to update download progress smoothly."""
    if job_id not in job_store: return # Job might have been cancelled/deleted

    if d['status'] == 'downloading':
        job_store[job_id]['status'] = 'downloading'
        total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
        if total_bytes and total_bytes > 0:
            # Calculate progress as 5% to 50% range during download
            download_progress = int((d['downloaded_bytes'] / total_bytes) * 45) # Scale 0-100% to 0-45%
            job_store[job_id]['progress'] = 5 + download_progress # Add base 5%
        else:
             # If no total size, just show small progress
             job_store[job_id]['progress'] = max(job_store[job_id].get('progress', 5), 10)

    elif d['status'] == 'finished':
        job_store[job_id]['progress'] = 50 # Mark download phase as complete
        job_store[job_id]['status'] = 'processing' # Ready for Spleeter
    elif d['status'] == 'error':
        job_store[job_id]['status'] = 'error'
        job_store[job_id]['message'] = 'Download failed via yt-dlp hook'
        job_store[job_id]['progress'] = 0


# --- Optional: Job Cleanup Logic ---
# (Consider a more robust solution like Redis TTL or scheduled tasks for production)
# JOB_TTL = 3600 # 1 hour

# def cleanup_old_jobs():
#     # ... (implementation as before) ...
# pass # Placeholder if not implementing now

# cleanup_thread = Thread(target=cleanup_old_jobs, daemon=True)
# cleanup_thread.start()


# --- Uvicorn Entry Point (for local testing) ---
if __name__ == "__main__":
    import uvicorn
    # Run locally using: python main.py
    # Render uses the startCommand defined in render.yaml or the dashboard
    uvicorn.run(app, host="0.0.0.0", port=8000)