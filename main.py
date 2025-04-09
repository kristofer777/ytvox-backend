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

DOWNLOADS = Path(".")
job_store = {}

class ExtractRequest(BaseModel):
    url: str

@app.post("/extract")
def start_extraction(request: ExtractRequest):
    job_id = uuid4().hex[:6]
    job_store[job_id] = {
        "status": "processing",
        "progress": 0,
        "start_time": time.time()
    }
    thread = Thread(target=process_acapella, args=(request.url, job_id))
    thread.start()
    return { "job_id": job_id }

@app.get("/progress/{job_id}")
def get_progress(job_id: str):
    job = job_store.get(job_id)
    if not job:
        return { "status": "error", "message": "Job not found" }
    return job

def process_acapella(link, job_id):
    try:
        ID = uuid4().hex[:6]
        output_wav = DOWNLOADS / f"track_{ID}.wav"

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(output_wav).replace(".wav", ".%(ext)s"),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
                'preferredquality': '192',
            }],
            'postprocessor_args': ['-ar', '44100'],
            'quiet': True,
        }

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=True)
            title = info.get('title', f"acapella_{ID}")

        clean_title = re.sub(r'[^\w\s().-]', '', title).strip().replace(" ", "_").upper()
        final_output = DOWNLOADS / f"{clean_title}_ACAPELLA.wav"

        # Run Spleeter separation
        subprocess.run([
            "spleeter", "separate",
            "-p", "spleeter:2stems",
            "-o", str(DOWNLOADS),
            str(output_wav)
        ], check=True)

        # Move extracted vocals to final location
        stem_folder = DOWNLOADS / f"track_{ID}"
        vocals_path = stem_folder / "vocals.wav"
        shutil.move(str(vocals_path), final_output)

        # Clean up
        shutil.rmtree(stem_folder)
        os.remove(output_wav)

        job_store[job_id]["progress"] = 100
        job_store[job_id]["status"] = "done"

    except Exception as e:
        job_store[job_id]["status"] = "error"
        job_store[job_id]["message"] = str(e)