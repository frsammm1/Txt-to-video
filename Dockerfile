FROM python:3.10-slim-buster

# Install FFmpeg and Git (Critical for media processing)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

# Run the bot
CMD ["python3", "main.py"]
