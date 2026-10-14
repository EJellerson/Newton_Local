#!/usr/bin/env python3
"""Entry point for Voice Studio's reference ingestion (docs/18 §5).

Run by the control service as
    /usr/bin/sandbox-exec -f config/fish-engine.sb tools/fish-venv/bin/python3 -I \
        scripts/fish_audio.py ingest --input F --outdir D --limits JSON
so private audio is processed with no network access. See
fish_tts/audio_tool.py for what it does.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fish_tts.audio_tool import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
