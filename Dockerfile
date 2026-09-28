FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Mount a volume at /app/data to keep user settings between restarts
CMD ["python", "bot.py"]
