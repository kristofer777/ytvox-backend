FROM python:3.8-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install necessary system packages
RUN apt-get update && \
    apt-get install -y ffmpeg git build-essential && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Upgrade pip and install dependencies
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

EXPOSE 10000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]