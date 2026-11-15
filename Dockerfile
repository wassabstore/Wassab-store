FROM python:3.12-slim

# ffmpeg: دمج الصوت والفيديو | aria2: تحميل أسرع | deno: مطلوب لتحدّيات يوتيوب
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg aria2 curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY downloader_bot_v2.py .

# تحديث yt-dlp عند كل تشغيل لأن يوتيوب يغيّر أشياء باستمرار
CMD ["sh", "-c", "pip install -U --no-cache-dir 'yt-dlp[default]' && python downloader_bot_v2.py"]
