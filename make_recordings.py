"""Generate the demo call recordings. macOS only; the outputs are committed, so
nobody else needs to run this.

One clip per call type, spoken line by line from demo_data.CALL_SCRIPTS with the
Mac's built-in voices (Lekha = agent, Rishi = customer). Also writes timings.json
so each transcript line is timestamped where it is heard in the audio.

    python make_recordings.py
"""
import json
import os
import subprocess
import tempfile
import wave

from demo_data import CALL_SCRIPTS, RECORDING_DIR

VOICE = {"agent": "Lekha", "customer": "Rishi"}
RATE = 16000   # samples per second (phone-call quality)
GAP = 0.6      # seconds of silence between turns


def main():
    os.makedirs(RECORDING_DIR, exist_ok=True)
    timings = {}
    with tempfile.TemporaryDirectory() as tmp:
        for outcome, lines in CALL_SCRIPTS.items():
            chunks, offsets, t = [], [], 0.0
            for n, (role, text) in enumerate(lines):
                part = os.path.join(tmp, f"{outcome}-{n}.wav")
                subprocess.run(["say", "-v", VOICE[role], "-o", part, f"--data-format=LEI16@{RATE}", text], check=True)
                with wave.open(part) as w:
                    audio = w.readframes(w.getnframes())
                offsets.append(round(t, 2))
                chunks.append(audio + b"\0\0" * int(RATE * GAP))
                t += len(audio) / 2 / RATE + GAP
            full = os.path.join(tmp, f"{outcome}.wav")
            with wave.open(full, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(b"".join(chunks))
            out = os.path.join(RECORDING_DIR, f"{outcome}.m4a")
            subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "32000", full, out], check=True)
            timings[outcome] = {"offsets": offsets, "duration": round(t, 2)}
            print(f"{outcome:15} {t:5.1f}s  {os.path.getsize(out) // 1024} KB")
    with open(os.path.join(RECORDING_DIR, "timings.json"), "w") as f:
        json.dump(timings, f, indent=2)


if __name__ == "__main__":
    main()
