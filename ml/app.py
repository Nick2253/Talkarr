"""talkarr-ml: the models Talkarr needs but doesn't run itself.

A small, stateless HTTP service, deployed on eezo (the AI host) so the main Talkarr
container never runs models. One endpoint does the work:

    POST /v1/analyze   multipart "audio": any ffmpeg-decodable audio (Talkarr sends
                       16 kHz mono Opus of the dialogue channel)
        -> {"duration": s, "models": {...},
            "regions": [{"start", "end", "speech_prob",
                         "p_en", "top": [[lang, prob], ...]}]}

Regions are Silero VAD speech segments. Each gets VoxLingua107 (ECAPA) language
scores computed on the segment padded with neighbouring audio to at least
``MIN_LID_SECONDS``, because language ID on a one-second "Yeah!" is noise. Scores are
returned raw: smoothing, thresholds and decisions belong to Talkarr, where the eval
harness can tune them.
"""

import logging
import os
import subprocess
import tempfile

import numpy as np
import torch
from flask import Flask, jsonify, request
from silero_vad import get_speech_timestamps, load_silero_vad
from speechbrain.inference.classifiers import EncoderClassifier

SAMPLE_RATE = 16000
MIN_LID_SECONDS = 3.0
MAX_LID_SECONDS = 12.0
TOP_K = 5
LID_SOURCE = os.environ.get("LID_MODEL", "speechbrain/lang-id-voxlingua107-ecapa")
LID_DIR = os.environ.get("LID_MODEL_DIR", "/models/voxlingua107")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("talkarr-ml")
logging.getLogger("speechbrain").setLevel(logging.WARNING)
torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "4")))

app = Flask(__name__)
_models = {}


def models():
    if not _models:
        _models["vad"] = load_silero_vad()
        _models["lid"] = EncoderClassifier.from_hparams(
            source=LID_SOURCE, savedir=LID_DIR, run_opts={"device": "cpu"}
        )
        labels = _models["lid"].hparams.label_encoder
        _models["labels"] = [labels.ind2lab[i].split(":")[0] for i in range(len(labels.ind2lab))]
        _models["en_index"] = _models["labels"].index("en")
    return _models


@app.get("/health")
def health():
    models()
    return jsonify(status="ok")


@app.post("/v1/analyze")
def analyze():
    upload = request.files.get("audio")
    if upload is None:
        return jsonify(error="multipart field 'audio' is required"), 400
    with tempfile.NamedTemporaryFile(suffix=".bin") as tmp:
        upload.save(tmp.name)
        try:
            audio = decode(tmp.name)
        except RuntimeError as e:
            return jsonify(error=str(e)), 400

    m = models()
    wav = torch.from_numpy(audio)
    speech = get_speech_timestamps(
        wav,
        m["vad"],
        sampling_rate=SAMPLE_RATE,
        return_seconds=False,
        min_speech_duration_ms=250,
        min_silence_duration_ms=300,
    )
    regions = [lid(m, audio, seg["start"], seg["end"]) for seg in speech]
    log.info("analyzed %.0f s of audio: %d speech regions", len(audio) / SAMPLE_RATE, len(regions))
    return jsonify(
        duration=len(audio) / SAMPLE_RATE,
        models={"vad": "silero-vad", "lid": LID_SOURCE},
        regions=regions,
    )


def decode(path):
    """Any audio file -> float32 mono 16 kHz samples."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cannot decode audio: {proc.stderr.decode()[-300:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def lid(m, audio, start, end):
    """Language scores for one speech segment (sample indices), with context padding."""
    a, b = _padded(start, end, len(audio))
    with torch.no_grad():
        out = m["lid"].classify_batch(torch.from_numpy(audio[a:b]).unsqueeze(0))
    probs = out[0].exp().squeeze(0)
    probs = probs / probs.sum()
    top = torch.topk(probs, TOP_K)
    return {
        "start": round(start / SAMPLE_RATE, 3),
        "end": round(end / SAMPLE_RATE, 3),
        "p_en": round(float(probs[m["en_index"]]), 4),
        "top": [
            [m["labels"][i], round(float(p), 4)]
            for p, i in zip(top.values, top.indices, strict=True)
        ],
    }


def _padded(start, end, total):
    """Grow [start, end) symmetrically to MIN_LID_SECONDS, cap at MAX_LID_SECONDS."""
    min_len, max_len = int(MIN_LID_SECONDS * SAMPLE_RATE), int(MAX_LID_SECONDS * SAMPLE_RATE)
    length = end - start
    if length < min_len:
        grow = (min_len - length) // 2
        start, end = max(0, start - grow), min(total, end + grow)
    elif length > max_len:
        mid = (start + end) // 2
        start, end = mid - max_len // 2, mid + max_len // 2
    return start, end
