"""Speech to text for the theme: the page records the visitor, app.py hands the audio to transcribe(), and
ElevenLabs (Scribe) returns the words. The key stays on this laptop; the browser never sees it.

  python -m art_curator.listen recording.webm     transcribe a saved recording (any common audio format)

Needs ELEVENLABS_API_KEY in .env with the Speech to Text permission. Optional: ELEVENLABS_STT_MODEL.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

URL = "https://api.elevenlabs.io/v1/speech-to-text"
DEFAULT_MODEL = "scribe_v2"
MAX_BYTES = 10_000_000  # a spoken theme is ~5 s, well under 1 MB; refuse anything absurd
TIMEOUT_S = 30


class ListenError(RuntimeError):
    """The audio couldn't be turned into a theme. The visitor can try again or type."""


def api_key() -> str:
    from robot import _load_env
    _load_env()
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise ListenError("no ELEVENLABS_API_KEY in .env")
    return key


def available() -> bool:
    try:
        api_key()
        return True
    except ListenError:
        return False


def transcribe(audio: bytes, mime: str = "audio/webm") -> str:
    """The words spoken in `audio` (stripped), or ListenError."""
    if not audio:
        raise ListenError("no audio recorded")
    if len(audio) > MAX_BYTES:
        raise ListenError("recording too long")
    boundary = uuid.uuid4().hex
    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/aiff": "aiff",
           "audio/wav": "wav", "audio/mpeg": "mp3"}.get(mime.split(";")[0].strip(), "bin")
    model = os.environ.get("ELEVENLABS_STT_MODEL", "").strip() or DEFAULT_MODEL
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\n{model}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"tag_audio_events\"\r\n\r\nfalse\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"theme.{ext}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "xi-api-key": api_key(), "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            text = json.load(resp).get("text", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        if e.code in (401, 403):
            raise ListenError(f"ElevenLabs refused speech to text ({e.code}): does the key have the "
                              f"Speech to Text permission? {detail}")
        raise ListenError(f"ElevenLabs speech to text error {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ListenError(f"can't reach ElevenLabs ({e})")
    text = " ".join(text.split())
    if not text:
        raise ListenError("didn't catch any words")
    return text


if __name__ == "__main__":
    path = sys.argv[1]
    mime = {"webm": "audio/webm", "m4a": "audio/mp4", "aiff": "audio/aiff", "wav": "audio/wav",
            "mp3": "audio/mpeg", "ogg": "audio/ogg"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
    try:
        print(transcribe(open(path, "rb").read(), mime))
    except ListenError as e:
        raise SystemExit(f"[listen] {e}")
