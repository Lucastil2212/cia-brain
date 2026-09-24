from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .settings import Settings


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def extension_for(url: str, mime: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and 1 < len(suffix) <= 12:
        return suffix
    guessed = mimetypes.guess_extension((mime or "").split(";")[0].strip())
    return guessed or ".bin"


def cas_path(settings: Settings, digest: str, ext: str) -> Path:
    return settings.raw_dir / digest[:2] / digest[2:4] / f"{digest}{ext}"


class StreamingCASWriter:
    def __init__(self, settings: Settings, url: str, mime: str):
        self.settings = settings
        self.url = url
        self.mime = mime
        self._hash = hashlib.sha256()
        self._size = 0
        fd, tmp = tempfile.mkstemp(prefix="fetch-", dir=settings.raw_dir)
        os.close(fd)
        self.tmp_path = Path(tmp)
        self._fh = self.tmp_path.open("wb")

    @property
    def size(self) -> int:
        return self._size

    def write(self, chunk: bytes) -> None:
        self._hash.update(chunk)
        self._size += len(chunk)
        self._fh.write(chunk)

    def commit(self) -> tuple[str, Path]:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        digest = self._hash.hexdigest()
        ext = extension_for(self.url, self.mime)
        dst = cas_path(self.settings, digest, ext)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            self.tmp_path.unlink(missing_ok=True)
        else:
            os.replace(self.tmp_path, dst)
        return digest, dst

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            self.tmp_path.unlink(missing_ok=True)


SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
}


def _redact_headers(headers: dict) -> dict:
    out = {}
    for key, value in (headers or {}).items():
        if str(key).lower() in SENSITIVE_HEADERS:
            out[key] = "[redacted]"
        else:
            out[key] = value
    return out


def write_manifest(settings: Settings, digest: str, payload: dict) -> Path:
    out = settings.manifests_dir / digest[:2] / f"{digest}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    source = {
        "url": payload.get("url"),
        "final_url": payload.get("final_url"),
        "fetched_at": payload.get("fetched_at"),
        "status_code": payload.get("status_code"),
    }
    manifest = {
        "sha256": digest,
        "path": payload.get("path"),
        "mime": payload.get("mime"),
        "size": payload.get("size"),
        "headers": _redact_headers(payload.get("headers", {})),
        "sources": [source],
    }
    if out.exists():
        try:
            previous = json.loads(out.read_text(encoding="utf-8"))
            sources = previous.get("sources", [])
            key = (source.get("url"), source.get("final_url"))
            if key not in {(x.get("url"), x.get("final_url")) for x in sources}:
                sources.append(source)
            manifest["sources"] = sources
        except Exception:
            pass
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    return out
