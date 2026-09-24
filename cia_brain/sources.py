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
        "https://www.dni.gov/index.php/foia",
        "archive",
        ("text", "pdf"),
        documentation="https://www.dni.gov/index.php/foia",
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
    Source(
        "usgs",
        "USGS",
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
        "geojson",
        ("text", "geospatial"),
        60,
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php",
    ),
    Source(
        "nws",
        "NOAA/NWS",
        "https://api.weather.gov/alerts/active",
        "geojson",
        ("text", "geospatial"),
        120,
        "https://www.weather.gov/documentation/services-web-api",
    ),
    Source(
        "swpc",
        "NOAA/SWPC",
        "https://services.swpc.noaa.gov/products/geospace/propagated-solar-wind-1-hour.json",
        "timeseries",
        ("telemetry",),
        60,
        "https://www.swpc.noaa.gov/products/real-time-solar-wind",
    ),
    Source(
        "goes",
        "NOAA/NESDIS",
        "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/CONUS/GEOCOLOR/latest.jpg",
        "image",
        ("image",),
        600,
        "https://www.star.nesdis.noaa.gov/GOES/",
    ),
    Source(
        "nasa",
        "NASA",
        "https://images-api.nasa.gov/search?media_type=image,video,audio&year_start=2026&page_size=100",
        "media",
        ("text", "image", "video", "audio"),
        3600,
        "https://images.nasa.gov/docs/images.nasa.gov_api_docs.pdf",
    ),
    Source(
        "nara_catalog",
        "NARA",
        "https://catalog.archives.gov/api/v1/?q=declassified&type=description&rows=50&resultTypes=item,fileUnit",
        "catalog",
        ("text", "geospatial"),
        1800,
        "https://github.com/usnationalarchives/CatalogAPI",
    ),
    Source(
        "state_rss",
        "Department of State",
        "https://www.state.gov/rss-feed/press-releases/feed/",
        "rss",
        ("text",),
        900,
        "https://www.state.gov/rss-feed/",
    ),
    Source(
        "fema_rss",
        "FEMA",
        "https://www.fema.gov/about/news-multimedia/rss",
        "rss",
        ("text",),
        900,
        "https://www.fema.gov/about/news-multimedia",
    ),
    Source(
        "odni_rss",
        "ODNI",
        "https://www.dni.gov/index.php?format=feed&type=rss",
        "rss",
        ("text",),
        1800,
        "https://www.dni.gov/",
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
    enabled |= {s.id for s in selected_sources(settings.live_sources)}
    return [asdict(s) | {"enabled": s.id in enabled} for s in SOURCES]


def archive_url_allowed(url: str, settings) -> bool:
    """Scope catalog archives to collections; permit linked government attachments."""
    from posixpath import normpath

    parts = urlsplit(url)
    host = parts.hostname
    if host in {x.strip().lower() for x in settings.allowed_hosts.split(",")}:
        return True
    selected = {x.strip() for x in settings.archive_sources.split(",") if x.strip()}
    path = normpath(parts.path or "/")
    if ".." in path.split("/"):
        return False
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
