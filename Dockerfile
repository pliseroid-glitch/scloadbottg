FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# HuggingFace Spaces expects port 7860 to be open (health check)
# We'll run a dummy HTTP server alongside the bot
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 7860

CMD ["./start.sh"]
