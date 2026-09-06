#!/usr/bin/env python3
"""Generates a tiny synthetic 5-second MP4 (test pattern + sine tone + scene
changes) used by the integration test, so no binary video needs to be
committed to the repo."""
import subprocess
import sys
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "sample" / "sample.mp4"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=10:duration=5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=5",
        "-vf",
        "hue=h=t*60",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(OUTPUT),
    ]
    subprocess.run(cmd, check=True)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    sys.exit(main())
