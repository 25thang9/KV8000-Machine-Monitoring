from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
load_dotenv(ROOT / ".env")

if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "machine_monitoring.settings")

from waitress import serve  # noqa: E402
from machine_monitoring.wsgi import application  # noqa: E402


def main() -> int:
    host = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("WEB_PORT", "8001"))
    threads = max(8, int(os.getenv("WEB_THREADS", "32")))
    print(f"WEB READY: http://{host}:{port} | threads={threads}")
    serve(
        application,
        host=host,
        port=port,
        threads=threads,
        channel_timeout=120,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
