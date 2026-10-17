"""Spoken narration: a short preview after curating, each artwork as its tile is carried, then the full
curatorial statement, a closing reflection and the sign-off once everything is on the table.

Voice: ElevenLabs when ELEVENLABS_API_KEY is set (in .env), else the Mac's `say`. Right after curating,
every line of the exhibition is generated in parallel (preview first), so nothing waits later; clips are
cached in runtime/voice_cache/. Any line ElevenLabs can't deliver in time (no wifi, quota) is spoken by
`say` instead: the demo never goes silent. VERIARM_VOICE=0 mutes (lines are still printed).
  .env: ELEVENLABS_API_KEY=...   optional: ELEVENLABS_VOICE_ID=... (see `voices`), ELEVENLABS_MODEL=...,
        ART_MAC_VOICE="Ava (Premium)" (a better `say` voice, if downloaded in System Settings)

Pacing (exhibition.execute calls quiet() between steps): each artwork's description plays WHILE its tile is
carried, and the next tile doesn't start until that description has finished; the closing words wait for
the last one. wait=True is fully sequential instead: the arm pauses for every sentence, then moves.

  python -m art_curator.narrate                 hear a sample narration (offline curator, no robot)
  python -m art_curator.narrate say "Hello"     say one sentence with the configured voice
  python -m art_curator.narrate voices          list the ElevenLabs voices on your account

`say` reads English only: other languages fall back to a short English line. ElevenLabs reads them as is.
"""
import argparse
import asyncio
import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor

from art_curator.exhibition import RUNTIME

ORDINAL = {1: "First", 2: "Next", 3: "And finally"}
SIGN_OFF = "Thank you for curating with me. Until our next exhibition, stay curious!"  # every exhibition ends on this
CACHE = RUNTIME / "voice_cache"
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # "George": warm, storytelling (ElevenLabs default library)
DEFAULT_MODEL = "eleven_multilingual_v2"   # their most natural model; also reads non-English text
SYNTH_TIMEOUT_S = 20.0  # waiting longer than this for a clip: say it with the Mac voice instead


def english(text: str) -> bool:
    """Mostly ASCII letters: something the Mac's English voice can read."""
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) > 0.9


# ---------- the two voices ----------

class ElevenLabs:
    def __init__(self, key: str, voice_id: str, model: str):
        self.key, self.voice_id, self.model = key, voice_id, model

    def clip(self, text: str):
        """Path of an mp3 of `text`, from the cache or freshly generated. Raises on any failure."""
        name = hashlib.sha1(f"{self.voice_id}|{self.model}|{text}".encode()).hexdigest()
        path = CACHE / f"{name}.mp3"
        if path.exists():
            return path
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}?output_format=mp3_44100_128",
            data=json.dumps({"text": text, "model_id": self.model}).encode(),
            headers={"xi-api-key": self.key, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=SYNTH_TIMEOUT_S) as resp:
            audio = resp.read()
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(audio)
        tmp.replace(path)
        return path


def mac_say(text: str) -> None:
    voice = os.environ.get("ART_MAC_VOICE")
    if shutil.which("say"):
        subprocess.run(["say", *(["-v", voice] if voice else []), text], check=False)


def configured_elevenlabs() -> ElevenLabs | None:
    from robot import _load_env  # the repo's .env, like the robot and Claude keys
    _load_env()
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key or not shutil.which("afplay"):
        return None
    return ElevenLabs(key, os.environ.get("ELEVENLABS_VOICE_ID", "").strip() or DEFAULT_VOICE_ID,
                      os.environ.get("ELEVENLABS_MODEL", "").strip() or DEFAULT_MODEL)


# ---------- the narrator ----------

class Narrator:
    """Speaks one line after another from its own queue; the caller never blocks unless wait=True."""

    def __init__(self, wait: bool = False):
        self.wait = wait
        self.muted = os.environ.get("VERIARM_VOICE", "1") == "0"
        self.eleven = None if self.muted else configured_elevenlabs()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.clips: dict[str, Future] = {}
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._speak_lines, daemon=True).start()
        if not self.muted:
            print(f"[voice] {'ElevenLabs voice ' + self.eleven.voice_id if self.eleven else 'Mac voice (say)'}")

    # -- plumbing --

    def _prepare(self, texts: list[str]) -> None:
        """Start generating these lines now (in this order), so they're ready when it's their turn."""
        if self.eleven:
            for text in texts:
                if text not in self.clips:
                    self.clips[text] = self.pool.submit(self.eleven.clip, text)

    def _speak_lines(self) -> None:
        while True:
            text, mac_text = self.lines.get()
            try:
                if not self.muted:
                    self._speak(text, mac_text)
            finally:
                self.lines.task_done()

    def _speak(self, text: str, mac_text: str) -> None:
        if self.eleven:
            try:
                self._prepare([text])
                path = self.clips[text].result(timeout=SYNTH_TIMEOUT_S)
                subprocess.run(["afplay", str(path)], check=False)
                return
            except Exception as e:  # network, quota, timeout: this line goes to the Mac voice
                self.clips.pop(text, None)
                if getattr(e, "code", None) in (401, 402, 403):  # bad key / out of credits: won't recover
                    self.eleven = None
                    print(f"[voice] ElevenLabs refused ({e}); Mac voice from now on")
                else:
                    print(f"[voice] ElevenLabs failed ({type(e).__name__}: {e}); Mac voice for this line")
        mac_say(text if english(text) else mac_text)

    def say(self, text: str, mac_text: str | None = None) -> None:
        """Queue a line. mac_text: what the English-only Mac voice says if `text` isn't English."""
        print(f"[say] {text}")
        self.lines.put((text, mac_text or "Your exhibition."))
        if self.wait:
            self.lines.join()

    async def quiet(self) -> None:
        """Wait until everything said so far has been spoken (the arm keeps whatever pose it has)."""
        await asyncio.to_thread(self.lines.join)

    # -- the script --

    @staticmethod
    def _preview(ex):
        return f"{ex['exhibition_title']}. {ex['preview']}"

    @staticmethod
    def _starting(ex):
        return f"Now assembling {ex['exhibition_title']}."

    @staticmethod
    def _placing(pick):
        return f"{ORDINAL.get(pick['position'], 'Then')}, {pick['title']}, by {pick['artist']}. {pick['reason']}"

    @staticmethod
    def _path(ex):
        a, b, c = (p["title"] for p in ex["selected_artworks"])  # validated: positions 1, 2, 3 in order
        return f"{ex['exhibition_title']}: from {a}, through {b}, to {c}."

    @staticmethod
    def _names(ex):
        a, b, c = (p["title"] for p in ex["selected_artworks"])
        return f"Your exhibition: {a}, {b}, and {c}."

    def curated(self, ex: dict) -> None:
        """A new exhibition: start generating every line of it, and say the short preview."""
        self._prepare([self._preview(ex), self._starting(ex)]
                      + [self._placing(p) for p in ex["selected_artworks"]]
                      + [self._path(ex), ex["curatorial_statement"], ex["closing"], SIGN_OFF])
        self.say(self._preview(ex), self._names(ex))

    def starting(self, ex: dict) -> None:
        self.say(self._starting(ex), "Now assembling your exhibition.")

    def placing(self, pick: dict) -> None:
        """Called as the arm sets off for this artwork: the description plays while it's carried."""
        intro = f"{ORDINAL.get(pick['position'], 'Then')}, {pick['title']}, by {pick['artist']}."
        self.say(self._placing(pick), intro)

    def complete(self, ex: dict) -> None:
        """Everything is on the table: the path through the three works, the full curatorial statement,
        the curator's closing reflection, then the same sign-off every time."""
        self.say(self._path(ex), self._names(ex))
        self.say(ex["curatorial_statement"], " ")
        self.say(ex["closing"], " ")
        self.say(SIGN_OFF)

    def failed(self, pick: dict) -> None:
        self.say(f"I couldn't place {pick['title']}. Please check the table.")


# ---------- command line ----------

def list_voices() -> None:
    eleven = configured_elevenlabs()
    if not eleven:
        raise SystemExit("No ELEVENLABS_API_KEY in .env")
    req = urllib.request.Request("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": eleven.key})
    with urllib.request.urlopen(req, timeout=20) as resp:
        voices = json.load(resp)["voices"]
    for v in voices:
        labels = ", ".join(str(x) for x in (v.get("labels") or {}).values())
        print(f"{v['voice_id']}  {v['name']:28s} {labels}")
    print("\nPick one: ELEVENLABS_VOICE_ID=<id> in .env")


def sample() -> None:
    from art_curator.curator import curate
    ex = curate("joy", offline=True)
    n = Narrator(wait=True)
    n.curated(ex)
    n.starting(ex)
    for p in ex["selected_artworks"]:
        n.placing(p)
    n.complete(ex)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", nargs="?", choices=["sample", "say", "voices"], default="sample")
    p.add_argument("text", nargs="?")
    args = p.parse_args()
    if args.cmd == "voices":
        list_voices()
    elif args.cmd == "say":
        Narrator(wait=True).say(args.text or SIGN_OFF)
    else:
        sample()


if __name__ == "__main__":
    main()
