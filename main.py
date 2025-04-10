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
# Import YoutubeDL specifically to catch its download errors if needed
from yt_dlp import YoutubeDL, DownloadError
from fastapi.staticfiles import StaticFiles

# ---> ADD THIS IMPORT FOR CORS <---
from fastapi.middleware.cors import CORSMiddleware

# --- FastAPI App Initialization ---
app = FastAPI()

# ---> ADD CORS MIDDLEWARE CONFIGURATION <---
# Using the extension ID you provided
chrome_extension_origin = "chrome-extension://modaahafjnllmkaabcinemclpbbklgnn"

origins = [
    chrome_extension_origin,
    # You can add other origins here if needed, e.g., for local testing:
    # "http://localhost",
    # "http://127.0.0.1",
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
DOWNLOADS = Path("./download_data")
DOWNLOADS.mkdir(exist_ok=True)
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
        "message": "Job added to queue." # Initial message
    }
    thread = Thread(target=process_acapella, args=(request.url, job_id))
    thread.start()
    # Return the initial job status along with the ID
    # ---> MODIFIED THIS LINE for Python < 3.9 compatibility <---
    return {**job_store[job_id], "job_id": job_id}

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

# --- Static File Serving (Optional) ---
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
    output_wav = None
    stem_folder_path = None
    try:
        # Ensure job exists before proceeding
        if job_id not in job_store:
             print(f"Job {job_id}: Aborting processing, job not found in store.")
             return

        job_store[job_id]["status"] = "downloading"
        job_store[job_id]["progress"] = 5
        job_store[job_id]["message"] = "Starting download..."
        print(f"Job {job_id}: Starting download for {link}")

        ID = uuid4().hex[:6]
        output_wav = DOWNLOADS / f"track_{ID}.wav"

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
            # Add options yt-dlp suggests might help with bot detection (no guarantees)
            'cookiesfrombrowser': ('chrome',), # Tries to use Chrome cookies if available (unlikely on server)
            'sleep_interval_requests': 1, # Wait 1s between http requests
            'sleep_interval': 3 # Wait 3s before starting download
        }

        title = f"audio_{ID}"
        with YoutubeDL(ydl_opts) as ydl:
            try:
                # Check job status again before download attempt
                if job_store.get(job_id, {}).get("status") == "error":
                    print(f"Job {job_id}: Skipping download, job already marked as error.")
                    return
                info = ydl.extract_info(link, download=True)
                title = info.get('title', f"audio_{ID}")
                print(f"Job {job_id}: Download finished. Title: {title}")
            # Catch specific yt-dlp DownloadError
            except DownloadError as ydl_error:
                # ---> ADDED PRINT HERE for clearer server log <---
                print(f"Job {job_id}: yt-dlp DownloadError: {ydl_error}")
                # Check for common bot detection message
                if "confirm you.re not a bot" in str(ydl_error).lower():
                    error_message = "Download failed: Blocked by YouTube verification."
                else:
                    error_message = f"Download failed: {str(ydl_error)[:200]}"
                # Raise a standard error to be caught by the outer handler
                raise ValueError(error_message)
            except Exception as ydl_generic_error:
                 print(f"Job {job_id}: Generic yt-dlp Error: {ydl_generic_error}")
                 error_message = f"yt-dlp failed unexpectedly: {str(ydl_generic_error)[:200]}"
                 raise ValueError(error_message)

        # Ensure job still exists and wasn't errored during download hook
        if job_store.get(job_id, {}).get("status") == "error":
            print(f"Job {job_id}: Aborting after download attempt, job status is error.")
            return
        if not output_wav.exists():
             print(f"Job {job_id}: Error - Expected WAV file {output_wav} not found after yt-dlp.")
             raise FileNotFoundError(f"Output WAV {output_wav.name} missing after download attempt.")


        job_store[job_id]["status"] = "processing"
        job_store[job_id]["progress"] = 50
        job_store[job_id]["message"] = "Processing audio..."

        clean_title = re.sub(r'[^\w\s().-]', '', title).strip().replace(" ", "_")
        if not clean_title: clean_title = f"audio_{ID}"
        final_output_filename = f"{clean_title}_ACAPELLA.wav"
        final_output_path = DOWNLOADS / final_output_filename

        stem_folder_name = output_wav.stem
        stem_folder_path = DOWNLOADS / stem_folder_name
        expected_vocals_path = stem_folder_path / "vocals.wav"

        print(f"Job {job_id}: Running Spleeter on {output_wav}...")
        spleeter_command = [
            "spleeter", "separate", "-p", "spleeter:2stems",
            "-o", str(DOWNLOADS), str(output_wav)
        ]

        result = subprocess.run(spleeter_command, capture_output=True, text=True, check=False)

        if result.returncode != 0:
            error_detail = result.stderr[:500].strip()
            print(f"Spleeter failed for job {job_id}. Code: {result.returncode}. Error: {error_detail}")
            # Check common errors
            if "out of memory" in error_detail.lower() or "killed" in error_detail.lower():
                 raise MemoryError(f"Spleeter failed: Out of memory. Consider upgrading service plan.")
            else:
                 raise RuntimeError(f"Spleeter processing failed: {error_detail}")

        print(f"Job {job_id}: Spleeter finished. Checking for {expected_vocals_path}...")

        if not expected_vocals_path.exists():
            print(f"Error for job {job_id}: Expected vocals file not found at {expected_vocals_path}")
            raise FileNotFoundError(f"Spleeter output vocals.wav not found in {stem_folder_path}")

        shutil.move(str(expected_vocals_path), final_output_path)
        print(f"Job {job_id}: Vocals moved to {final_output_path}")

        job_store[job_id]["progress"] = 100
        job_store[job_id]["status"] = "done"
        job_store[job_id]["result_url"] = f"/downloads/{final_output_filename}"
        job_store[job_id]["message"] = "Extraction successful."
        print(f"Job {job_id}: Successfully completed.")

    except Exception as e:
        error_type = type(e).__name__
        print(f"Error processing job {job_id} ({error_type}): {e}")
        if job_id in job_store:
            job_store[job_id]["status"] = "error"
            # Provide specific message for known recoverable errors
            if isinstance(e, FileNotFoundError):
                 job_store[job_id]["message"] = f"Processing error: Intermediate file missing."
            elif isinstance(e, MemoryError):
                 job_store[job_id]["message"] = str(e) # Use the specific memory error message
            elif isinstance(e, ValueError): # Catches our raised yt-dlp errors
                 job_store[job_id]["message"] = str(e)
            else: # Generic catch-all
                 job_store[job_id]["message"] = f"Error ({error_type}): {str(e)[:200]}"
            job_store[job_id]["progress"] = 0

    finally:
        # --- Cleanup ---
        print(f"Job {job_id}: Cleaning up intermediate files...")
        try:
            # Check if stem_folder_path was defined before trying to use it
            if 'stem_folder_path' in locals() and stem_folder_path is not None and stem_folder_path.exists():
                shutil.rmtree(stem_folder_path)
                print(f"Job {job_id}: Removed directory {stem_folder_path}")
            # Check if output_wav was defined before trying to use it
            if output_wav is not None and output_wav.exists():
                os.remove(output_wav)
                print(f"Job {job_id}: Removed file {output_wav}")
        except Exception as cleanup_error:
            print(f"Error during cleanup for job {job_id}: {cleanup_error}")


def update_download_progress(d, job_id):
    """Hook for yt-dlp to update download progress smoothly."""
    # Ensure job exists and hasn't already errored out
    if job_id not in job_store or job_store[job_id].get("status") == "error":
         return

    if d['status'] == 'downloading':
        # Only update if status is still downloading
        if job_store[job_id]['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total_bytes and total_bytes > 0:
                download_progress = int((d['downloaded_bytes'] / total_bytes) * 45)
                job_store[job_id]['progress'] = min(5 + download_progress, 49) # Cap below 50
                job_store[job_id]['message'] = f"Downloading... {job_store[job_id]['progress']}%"
            else:
                 job_store[job_id]['progress'] = max(job_store[job_id].get('progress', 5), 10)
                 job_store[job_id]['message'] = f"Downloading..."

    elif d['status'] == 'finished':
        if job_store[job_id]['status'] == 'downloading': # Prevent overwrite if already processing
            job_store[job_id]['progress'] = 50
            job_store[job_id]['status'] = 'processing'
            job_store[job_id]['message'] = "Download complete. Processing..."
    elif d['status'] == 'error':
        # Don't overwrite a more specific error message potentially set elsewhere
        if job_store[job_id]['status'] != 'error':
             job_store[job_id]['status'] = 'error'
             job_store[job_id]['message'] = 'Download failed (yt-dlp hook).'
             job_store[job_id]['progress'] = 0


# --- Uvicorn Entry Point (for local testing) ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)