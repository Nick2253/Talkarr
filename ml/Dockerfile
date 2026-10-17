# talkarr-ml: VAD + spoken language ID for Talkarr (CPU). Runs in the seastone stack on Steel.
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the model weights into the image: no downloads at runtime, reproducible results.
ENV LID_MODEL_DIR=/models/voxlingua107 HF_HOME=/models/hf
RUN python -c "from speechbrain.inference.classifiers import EncoderClassifier as E; \
E.from_hparams(source='speechbrain/lang-id-voxlingua107-ecapa', savedir='/models/voxlingua107')" && \
    python -c "from silero_vad import load_silero_vad; load_silero_vad()" && \
    chmod -R a+rX /models

COPY app.py smoke.py ./
USER 65534:65534
ENV PYTHONUNBUFFERED=1 TORCH_THREADS=4
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=8).status != 200)"
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "2", "--timeout", "1800"]
