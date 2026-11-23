"""Speaking exact words: text → Hermes' own TTS → 24 kHz mono PCM → the bot's microphone.

The stock google_meet plugin "speaks" by sending the text to OpenAI's Realtime model as a USER turn,
so what is heard is that model's reply to the text, not the text. Approving words would not bind
what is said. Here the words are synthesized verbatim:

* Synthesis is Hermes' ``text_to_speech_tool`` with the provider the owner configured
  (``tts.provider`` in config.yaml: edge today; openai, piper (open-weight, local), kittentts,
  neutts … by changing that one setting). Its normalizer strips markdown and expands units; our
  filter (rules.speech_problems) already refused markdown, so the words are the approved ones.
* Each line is played straight into the BlackHole virtual microphone by one short ffmpeg run
  (audiotoolbox output, the device found by NAME in ffmpeg's output-device list). The bot's own
  long-running relay (it tails ``speaker.pcm`` into one ffmpeg that lives for the whole meeting) played
  the first line and then nothing: in the live test on 2026-11-13 eight answers were written and only
  the first was heard, while a fresh one-shot ffmpeg was heard at once. ``speaker.pcm`` still marks a
  meeting joined with its voice on; nothing is written to it any more.
"""

from __future__ import annotations

import json
import os
import re
import threading
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

SAMPLE_RATE = 24000


class SpeechError(RuntimeError):
    pass


def ffmpeg() -> str:
    for c in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg"):
        if c and os.access(c, os.X_OK):
            return c
    raise SpeechError("ffmpeg not found")


def synthesize(text: str) -> List[str]:
    """Audio files for ``text`` from Hermes' configured TTS provider."""
    try:
        from tools.tts_tool import text_to_speech_tool
    except ImportError as exc:
        raise SpeechError("Hermes text-to-speech is unavailable: %s" % exc) from exc
    out = Path(tempfile.mkdtemp(prefix="omni-meet-tts-")) / "line.mp3"
    result = json.loads(text_to_speech_tool(text, output_path=str(out)))
    if not result.get("success"):
        raise SpeechError("text-to-speech failed: %s" % str(result.get("error") or result)[:200])
    paths = result.get("file_paths") or [result.get("file_path")]
    paths = [p for p in paths if p and os.path.isfile(p)]
    if not paths:
        raise SpeechError("text-to-speech produced no audio")
    return paths


def to_pcm(paths: List[str]) -> bytes:
    pcm = b""
    for p in paths:
        r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", p,
                            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-"],
                           capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not r.stdout:
            raise SpeechError("audio conversion failed: %s" % r.stderr.decode(errors="replace")[-200:])
        pcm += r.stdout
    # A quarter second of silence before and after, so the first word is not clipped by the mic gate.
    pad = b"\x00\x00" * (SAMPLE_RATE // 4)
    return pad + pcm + pad


VOICE_DEVICE = "BlackHole 2ch"
_PLAY_LOCK = threading.Lock()


def output_device_index(name: str = VOICE_DEVICE, run=subprocess.run) -> str:
    """ffmpeg's audiotoolbox OUTPUT index for the device called ``name`` (the stock bot looks the name up
    in the avfoundation INPUT list, a different numbering)."""
    r = run([ffmpeg(), "-hide_banner", "-f", "lavfi", "-i", "anullsrc=r=%d:cl=mono" % SAMPLE_RATE, "-t", "0.02",
             "-f", "audiotoolbox", "-list_devices", "true", "-"],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    for line in (r.stderr or "").splitlines():
        m = re.search(r"\[(\d+)\]\s+(.+?),\s*\S+\s*$", line)
        if m and m.group(2).strip().lower() == name.lower():
            return m.group(1)
    raise SpeechError("%s is not an audio output on this Mac (brew install --cask blackhole-2ch)" % name)


def play(meeting_dir: Path, pcm: bytes, run=subprocess.run) -> float:
    """Play the clip into Omni's virtual microphone now (one at a time). Returns its length in seconds."""
    if not (Path(meeting_dir) / "speaker.pcm").exists():
        raise SpeechError("the bot has no voice in this meeting (not joined with speaking on)")
    seconds = len(pcm) / 2 / SAMPLE_RATE
    with _PLAY_LOCK:
        index = output_device_index(run=run)
        r = run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                 "-i", "-", "-f", "audiotoolbox", "-audio_device_index", index, "-"],
                input=pcm, capture_output=True, timeout=seconds + 30)
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else str(r.stderr)
            raise SpeechError("playing into %s failed: %s" % (VOICE_DEVICE, err[-200:]))
    return seconds


def say(meeting_dir: Path, text: str) -> float:
    paths = synthesize(text)
    try:
        return play(meeting_dir, to_pcm(paths))
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass
