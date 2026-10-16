FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

EXPOSE 9494

CMD ["gunicorn", "wsgi:app", "-b", "0.0.0.0:9494", "-w", "1", "--threads", "4", "--timeout", "300"]
