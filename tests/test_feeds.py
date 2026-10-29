from cia_brain.settings import Settings
from cia_brain.sources import SOURCES, archive_url_allowed, selected_sources


def test_catalog_is_archive_collections_only():
    assert SOURCES
    assert {s.kind for s in SOURCES} == {"archive"}
    assert {s.id for s in SOURCES} == {"fbi", "nsa", "nara", "state", "odni", "dia", "crs"}


def test_source_scope():
    settings = Settings(_env_file=None)
    assert "vault.fbi.gov" in settings.allowed_host_set
    assert "crsreports.congress.gov" in settings.allowed_host_set
    assert "www.dni.gov" in settings.allowed_host_set
    assert archive_url_allowed("https://history.state.gov/historicaldocuments/test", settings)
    assert not archive_url_allowed("https://history.state.gov/other", settings)
    assert archive_url_allowed("https://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/doc.pdf", settings)
    assert archive_url_allowed("https://crsreports.congress.gov/product/pdf/R/R12345", settings)
    assert selected_sources("") == []
    assert {s.id for s in selected_sources("fbi,dia", True)} == {"fbi", "dia"}
