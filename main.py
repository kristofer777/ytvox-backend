from fastapi import FastAPI
from pydantic import BaseModel
from uuid import uuid4
from pathlib import Path
from threading import Thread
import os
import torch
import soundfile as sf
from yt_dlp import YoutubeDL
from demucs.pretrained import get_model
from demucs.apply import apply_model
from demucs.audio import AudioFile
import re

import time

app = FastAPI()

DOWNLOADS = Path.home() / "Downloads"
model = get_model(name="mdx_extra_q")
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
        temp_file = DOWNLOADS / f"temp_track_{ID}.wav"

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(DOWNLOADS / f"temp_track_{ID}.%(ext)s"),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
                'preferredquality': '192',
            }],
            'postprocessor_args': ['-ar', '44100'],
            'prefer_ffmpeg': True,
            'ffmpeg_location': '/opt/homebrew/bin/ffmpeg',
            'quiet': True,
            'noplaylist': True,
        }

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=True)
            title = info.get('title', f"acapella_{ID}")

        # ⛔️ FIX THIS LINE IF YOU HAD THE BAD CHARACTER ERROR
        clean_title = re.sub(r'[^\w\s().-]', '', title).strip().replace(" ", "_").upper()

        final_output = DOWNLOADS / f"{clean_title}_ACAPELLA.wav"

        wav = AudioFile(str(temp_file)).read(streams=0, samplerate=model.samplerate)
        if wav.dim() == 2:
            wav = wav.unsqueeze(0)

        sources = apply_model(model, wav, device="cpu")[0]
        vocals = sources[model.sources.index("vocals")]

        sf.write(str(final_output), vocals.T, samplerate=model.samplerate)
        os.remove(temp_file)

        job_store[job_id]["progress"] = 100
        job_store[job_id]["status"] = "done"

    except Exception as e:
        job_store[job_id]["status"] = "error"
        job_store[job_id]["message"] = str(e)