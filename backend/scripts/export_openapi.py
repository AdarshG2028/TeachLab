"""Write the OpenAPI schema to a file (used to generate the frontend's typed client).

Usage: uv run python scripts/export_openapi.py ../frontend/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    out.write_text(json.dumps(create_app().openapi(), indent=2), encoding="utf-8")
    print(f"wrote {out}")
