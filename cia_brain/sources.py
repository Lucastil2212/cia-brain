"""Curated public sources. Archive access remains subject to robots.txt."""

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Source:
    id: str
    agency: str
    url: str
    kind: str
    modalities: tuple[str, ...]
    interval: int = 0
    documentation: str = ""


SOURCES = (
    Source(
        "cia",
        "CIA",
        "https://www.cia.gov/readingroom/",
        "archive",
        ("text", "pdf"),
        documentation="https://www.cia.gov/readingroom/",
    ),
    Source("fbi", "FBI", "https://vault.fbi.gov/", "archive", ("text", "pdf")),
    Source(
        "nsa",
        "NSA",
        "https://www.nsa.gov/Helpful-Links/NSA-FOIA/Declassification-Transparency-Initiatives/",
        "archive",
        ("text", "pdf"),
    ),
    Source("nara", "NARA", "https://www.archives.gov/declassification", "archive", ("text", "pdf")),
    Source(
        "state",
        "Department of State",
        "https://history.state.gov/historicaldocuments",
        "archive",
        ("text",),
    ),
    Source(
        "odni",
        "ODNI",
        "https://www.odni.gov/accountability/",
        "archive",
        ("text", "pdf"),
        documentation="https://www.odni.gov/accountability/",
    ),
    Source(
        "dia",
        "DIA",
        "https://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/",
        "archive",
        ("text", "pdf"),
        documentation="https://www.dia.mil/FOIA/",
    ),
    Source(
        "crs",
        "Congressional Research Service",
        "https://crsreports.congress.gov/",
        "archive",
        ("text", "pdf"),
        documentation="https://crsreports.congress.gov/",
    ),
)

# Extra attachment / CDN hosts linked from FOIA reading rooms.
ATTACHMENT_HOSTS = {
    "nsa": {"media.defense.gov"},
    "dia": {"media.defense.gov"},
    "odni": {"www.dni.gov", "www.odni.gov"},
}


def selected_sources(ids: str, archive: bool = False) -> list[Source]:
    names = {x.strip() for x in ids.split(",") if x.strip()}
    valid = {s.id for s in SOURCES if (s.kind == "archive") == archive}
    if names - valid:
        raise ValueError(
            f"Unknown {'archive' if archive else 'feed'} sources: {sorted(names - valid)}"
        )
    return [s for s in SOURCES if s.id in names]


def archive_hosts(ids: str) -> set[str]:
    selected = {x.strip() for x in ids.split(",") if x.strip()}
    hosts = {urlsplit(s.url).hostname for s in selected_sources(ids, True)}
    for source_id, extras in ATTACHMENT_HOSTS.items():
        if source_id in selected:
            hosts |= extras
    return {h for h in hosts if h}


def catalog(settings) -> list[dict]:
    enabled = {s.id for s in selected_sources(settings.archive_sources, True)}
    allowed = {x.strip().lower() for x in settings.allowed_hosts.split(",") if x.strip()}
    if allowed & {"cia.gov", "www.cia.gov"}:
        enabled.add("cia")
    return [asdict(s) | {"enabled": s.id in enabled} for s in SOURCES]


def source_for_url(url: str, settings) -> Source | None:
    """Collection that owns a crawled URL, used to stamp agency provenance."""
    from posixpath import normpath

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    allowed = {x.strip().lower() for x in settings.allowed_hosts.split(",") if x.strip()}
    if host in allowed:
        return next((s for s in SOURCES if s.id == "cia"), None)
    path = normpath(parts.path or "/")
    selected = {x.strip() for x in settings.archive_sources.split(",") if x.strip()}
    for source in selected_sources(settings.archive_sources, True):
        root = urlsplit(source.url)
        if host != (root.hostname or "").lower():
            continue
        if source.id in {"fbi", "crs"}:
            return source
        root_path = normpath(root.path or "/")
        if path.startswith(root_path.rstrip("/") + "/") or path.rstrip("/") == root_path.rstrip("/"):
            return source
        if path.startswith("/files/") or "/foia" in path.lower() or "/readingroom" in path.lower().replace("-", ""):
            return source
    for source_id, extras in ATTACHMENT_HOSTS.items():
        if source_id in selected and host in extras:
            return next(s for s in SOURCES if s.id == source_id)
    return None


def collection_url_prefixes(source_id: str) -> list[str]:
    """URL prefixes that belong to a collection, including rows indexed before source_id existed."""
    source = next((item for item in SOURCES if item.id == source_id), None)
    if source is None:
        return []
    if source.id == "cia":
        return ["https://www.cia.gov/readingroom", "https://cia.gov/readingroom"]
    return [source.url.split("?")[0].rstrip("/")]


def archive_url_allowed(url: str, settings) -> bool:
    """Scope catalog archives to collections; permit linked government attachments."""
    from posixpath import normpath

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = normpath(parts.path or "/")
    if ".." in path.split("/"):
        return False
    allowed = {x.strip().lower() for x in settings.allowed_hosts.split(",") if x.strip()}
    if host in allowed:
        # CIA.gov is the seed host. Only the reading room is in the archive.
        return path == "/readingroom" or path.startswith("/readingroom/")
    selected = {x.strip() for x in settings.archive_sources.split(",") if x.strip()}
    for source in selected_sources(settings.archive_sources, True):
        root = urlsplit(source.url)
        if host == root.hostname:
            # Host-wide FOIA vaults / CRS product library.
            if source.id in {"fbi", "crs"}:
                return True
            root_path = normpath(root.path or "/")
            return (
                path.startswith(root_path.rstrip("/") + "/")
                or path.rstrip("/") == root_path.rstrip("/")
                or path.startswith("/files/")
                or "/foia" in path.lower()
                or "/readingroom" in path.lower().replace("-", "")
            )
    for source_id, extras in ATTACHMENT_HOSTS.items():
        if source_id in selected and host in extras:
            return True
    return False
