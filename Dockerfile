# Use an official Python runtime compatible with Spleeter/TensorFlow requirements
# Using Python 3.10 as specified previously
FROM python:3.10-slim

# Set environment variables to prevent interactive prompts during installs
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies:
# - ffmpeg: Required by yt-dlp postprocessor and potentially Spleeter
# - git: Might be needed if pip installs packages from git repos (sometimes dependencies do)
# - build-essential: Sometimes needed for compiling C extensions in Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install Python dependencies
# Using --no-cache-dir keeps the image size down
# ---> MODIFIED THIS LINE to explicitly upgrade yt-dlp <---
RUN pip install --no-cache-dir yt-dlp --upgrade && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Make port 10000 available to the world outside this container
# Render injects the PORT env var, which the start command will use.
# EXPOSE is more for documentation and local use.
EXPOSE 10000

# Command to run the application using Uvicorn
# Render will override this with its Start Command, but this is good practice for local testing.
# It uses port 10000 to match the EXPOSE directive.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]