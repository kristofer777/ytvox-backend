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

app = FastAPI()

# Use /tmp directory for temporary files on Render (ephemeral filesystem)
# Or use Render Disks if persistence is needed beyond restarts/deploys
DOWNLOADS = Path("./download_data") # Store downloads in a sub-directory
DOWNLOADS.mkdir(exist_ok=True) # Create the directory if it doesn't exist

job_store = {}

class ExtractRequest(BaseModel):
    url: str

@app.post("/extract")
def start_extraction(request: ExtractRequest):
    job_id = uuid4().hex[:6]
    job_store[job_id] = {
        "status": "queued",
        "progress": 0,
        "start_time": time.time(),
        "url": request.url,
        "result_url": None, # Placeholder for potential future download link
        "message": None
    }
    # Start processing in a background thread
    thread = Thread(target=process_acapella, args=(request.url, job_id))
    thread.start()
    return { "job_id": job_id }

@app.get("/progress/{job_id}")
def get_progress(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

# Example health check endpoint
@app.get("/health")
def health_check():
    return {"status": "ok"}

def process_acapella(link, job_id):
    try:
        job_store[job_id]["status"] = "downloading"
        ID = uuid4().hex[:6] # Unique ID for this specific processing run
        # Define output path within the DOWNLOADS directory
        output_wav = DOWNLOADS / f"track_{ID}.wav"

        ydl_opts = {
            'format': 'bestaudio/best',
            # Ensure output template uses the correct path and avoids extension conflict
            'outtmpl': str(DOWNLOADS / f"track_{ID}.%(ext)s"),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
                'preferredquality': '192', # Quality setting for the WAV
            }],
            'postprocessor_args': [
                '-ar', '44100' # Set audio sample rate
            ],
            'quiet': True, # Suppress yt-dlp console output
            'progress_hooks': [lambda d: update_download_progress(d, job_id)],
            'noplaylist': True, # Ensure only single video is downloaded if playlist URL is given
            # Consider adding download size/duration limits if needed
            # 'max_filesize': '50m',
            # 'duration': 600, # max 10 minutes
        }

        title = f"audio_{ID}" # Default title
        with YoutubeDL(ydl_opts) as ydl:
            try:
                # Download and extract info
                info = ydl.extract_info(link, download=True)
                # Safely get title after download completes
                title = info.get('title', f"audio_{ID}")
            except Exception as ydl_error:
                print(f"Error during download/extraction for job {job_id}: {ydl_error}")
                raise ValueError(f"yt-dlp failed: {ydl_error}") # Re-raise for job status


        job_store[job_id]["status"] = "processing"
        job_store[job_id]["progress"] = 50 # Rough progress update

        # Clean the title for use in filenames
        clean_title = re.sub(r'[^\w\s().-]', '', title).strip().replace(" ", "_")
        if not clean_title: # Handle cases where title becomes empty after cleaning
             clean_title = f"audio_{ID}"
        final_output_filename = f"{clean_title}_ACAPELLA.wav"
        final_output_path = DOWNLOADS / final_output_filename

        # Define the expected path Spleeter will output vocals to
        # Spleeter creates a subdirectory named after the input file stem
        stem_folder_name = output_wav.stem # e.g., "track_abcdef"
        stem_folder_path = DOWNLOADS / stem_folder_name
        expected_vocals_path = stem_folder_path / "vocals.wav"

        print(f"Job {job_id}: Running Spleeter on {output_wav}...")
        # Run Spleeter separation
        spleeter_command = [
            "spleeter", "separate",
            "-p", "spleeter:2stems", # Use 2 stems model (vocals/accompaniment)
            "-o", str(DOWNLOADS), # Output directory
            str(output_wav)      # Input audio file
        ]

        # Capture Spleeter output/errors for debugging
        result = subprocess.run(spleeter_command, capture_output=True, text=True, check=False) # Don't raise exception immediately

        if result.returncode != 0:
            print(f"Spleeter failed for job {job_id}. Return code: {result.returncode}")
            print(f"Stderr: {result.stderr}")
            print(f"Stdout: {result.stdout}")
            # Check if output wav was even created before trying to cleanup
            if output_wav.exists():
                 os.remove(output_wav)
            raise RuntimeError(f"Spleeter failed: {result.stderr[:500]}") # Raise error with spleeter output

        print(f"Job {job_id}: Spleeter finished. Moving vocals...")

        # Check if the expected vocals file exists
        if not expected_vocals_path.exists():
            print(f"Error for job {job_id}: Expected vocals file not found at {expected_vocals_path}")
            # Clean up intermediate files even if vocals weren't found
            if stem_folder_path.exists():
                shutil.rmtree(stem_folder_path)
            if output_wav.exists():
                os.remove(output_wav)
            raise FileNotFoundError(f"Spleeter output (vocals.wav) not found in {stem_folder_path}")

        # Move extracted vocals to the final desired location and name
        shutil.move(str(expected_vocals_path), final_output_path)
        print(f"Job {job_id}: Vocals moved to {final_output_path}")

        # Clean up intermediate files/folders
        print(f"Job {job_id}: Cleaning up...")
        if stem_folder_path.exists():
            shutil.rmtree(stem_folder_path)
        if output_wav.exists():
            os.remove(output_wav)

        job_store[job_id]["progress"] = 100
        job_store[job_id]["status"] = "done"
        # In a real app, you'd provide a way to download final_output_path
        # For now, we just mark as done. Consider adding 'result_filename': final_output_filename
        job_store[job_id]["result_url"] = f"/downloads/{final_output_filename}" # Example relative path
        print(f"Job {job_id}: Successfully completed.")

    except Exception as e:
        print(f"Error processing job {job_id}: {e}")
        job_store[job_id]["status"] = "error"
        job_store[job_id]["message"] = str(e)
        job_store[job_id]["progress"] = 0 # Reset progress on error

        # Attempt cleanup even on error
        try:
            if 'output_wav' in locals() and output_wav.exists():
                os.remove(output_wav)
            if 'stem_folder_path' in locals() and stem_folder_path.exists():
                shutil.rmtree(stem_folder_path)
        except Exception as cleanup_error:
            print(f"Error during cleanup for job {job_id}: {cleanup_error}")


def update_download_progress(d, job_id):
    """Hook for yt-dlp to update download progress."""
    if d['status'] == 'downloading':
        # Estimate progress: (downloaded_bytes / total_bytes) * 50% (since download is roughly half the work)
        total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
        if total_bytes:
            progress = int((d['downloaded_bytes'] / total_bytes) * 50)
            job_store[job_id]['progress'] = min(progress, 50) # Cap at 50 for download phase
            job_store[job_id]['status'] = 'downloading'
    elif d['status'] == 'finished':
        job_store[job_id]['progress'] = 50 # Mark download as complete
        job_store[job_id]['status'] = 'processing' # Move to next phase status
    elif d['status'] == 'error':
        job_store[job_id]['status'] = 'error'
        job_store[job_id]['message'] = 'Download failed'


# --- Optional: Add endpoint to serve the files ---
# Note: This is basic. For production, use a proper file serving solution or cloud storage.
# This also requires Render Disks for persistence, or files will be lost on deploys/restarts.
from fastapi.staticfiles import StaticFiles
# Mount the downloads directory to be accessible via /downloads URL path
# Check if DOWNLOADS dir exists; Render might clear it. Should be okay if created at start.
if DOWNLOADS.is_dir():
    app.mount("/downloads", StaticFiles(directory=DOWNLOADS), name="downloads")
else:
    print(f"Warning: Directory {DOWNLOADS} not found for static file serving.")

# --- Job Cleanup (Optional but Recommended) ---
# Simple cleanup of old jobs (e.g., older than 1 hour)
# This should run periodically, maybe in another thread or via a scheduled task system.
JOB_TTL = 3600 # 1 hour in seconds

def cleanup_old_jobs():
    while True:
        now = time.time()
        jobs_to_delete = []
        files_to_delete = []
        try:
            # Iterate safely over a copy of keys
            for job_id, job_data in list(job_store.items()):
                if now - job_data.get("start_time", 0) > JOB_TTL:
                    jobs_to_delete.append(job_id)
                    # Try to find the associated file to delete it too
                    if job_data.get("status") == "done" and job_data.get("result_url"):
                         filename = job_data["result_url"].split('/')[-1]
                         filepath = DOWNLOADS / filename
                         if filepath.exists():
                             files_to_delete.append(filepath)

            for job_id in jobs_to_delete:
                if job_id in job_store:
                    del job_store[job_id]
                print(f"Cleaned up old job: {job_id}")

            for filepath in files_to_delete:
                 try:
                     os.remove(filepath)
                     print(f"Cleaned up old file: {filepath}")
                 except OSError as e:
                     print(f"Error deleting file {filepath}: {e}")

        except Exception as e:
            print(f"Error during job cleanup: {e}")

        time.sleep(600) # Run cleanup every 10 minutes

# Start the cleanup thread
# cleanup_thread = Thread(target=cleanup_old_jobs, daemon=True)
# cleanup_thread.start()
# Note: Daemon threads might be abruptly stopped. Consider a more robust scheduling mechanism
# for production (like APScheduler, Celery Beat, or a separate cleanup service).
# For simplicity here, we might skip starting the thread automatically.