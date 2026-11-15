"""Install adapter-test dependencies matching the installed Home Assistant."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import homeassistant


def main() -> None:
    components = Path(homeassistant.__file__).parent / "components"
    requirements = []
    for component in ("homekit", "camera", "ffmpeg"):
        manifest = components / component / "manifest.json"
        requirements.extend(json.loads(manifest.read_text(encoding="utf-8"))["requirements"])
    subprocess.check_call([sys.executable, "-m", "pip", "install", *requirements])


if __name__ == "__main__":
    main()
