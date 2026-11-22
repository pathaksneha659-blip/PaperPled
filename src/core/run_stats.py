import json
import os
import tempfile
import threading
from pathlib import Path

from src.config import settings


_lock = threading.Lock()


def record(section: str, **kv):
    """Merge a named stats section into output/run_stats.json."""
    path = settings.OUTPUT_DIR / "run_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid run stats JSON in {path}") from error

        target = data
        parts = section.split(".")
        for part in parts[:-1]:
            value = target.setdefault(part, {})
            if not isinstance(value, dict):
                raise ValueError(f"Stats path {part!r} is not an object")
            target = value
        current = target.setdefault(parts[-1], {})
        if not isinstance(current, dict):
            raise ValueError(f"Stats section {section!r} is not an object")
        current.update(kv)

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            newline="",
        ) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = handle.name
        os.replace(temporary, path)
