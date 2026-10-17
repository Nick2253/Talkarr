"""Container smoke test: analyse 4 s of tone + 4 s of silence end to end."""

import json
import subprocess
import urllib.request

subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=f=220:d=4",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=16000:cl=mono:d=4",
        "-filter_complex",
        "[0][1]concat=n=2:v=0:a=1",
        "-ar",
        "16000",
        "-ac",
        "1",
        "/tmp/smoke.wav",
    ],
    check=True,
)
with open("/tmp/smoke.wav", "rb") as f:
    audio = f.read()
boundary = "talkarrsmoke"
disposition = 'Content-Disposition: form-data; name="audio"; filename="smoke.wav"'
body = (
    f"--{boundary}\r\n{disposition}\r\n\r\n".encode() + audio + f"\r\n--{boundary}--\r\n".encode()
)
req = urllib.request.Request(
    "http://127.0.0.1:8000/v1/analyze",
    body,
    {"Content-Type": f"multipart/form-data; boundary={boundary}"},
)
result = json.load(urllib.request.urlopen(req, timeout=300))
print(json.dumps({k: result[k] for k in ("duration", "models")}), len(result["regions"]), "regions")
assert abs(result["duration"] - 8.0) < 0.5, result["duration"]
