from cia_brain.crawler import RobotsRules, canonicalize_url, is_allowed_host


def test_canonicalize_removes_tracking_and_fragment():
    u = canonicalize_url("HTTPS://WWW.CIA.GOV/foo?utm_source=x&a=1#frag")
    assert u == "https://www.cia.gov/foo?a=1"


def test_allowed_host_is_exact():
    hosts = {"www.cia.gov", "cia.gov"}
    assert is_allowed_host("https://www.cia.gov/x", hosts)
    assert not is_allowed_host("https://cia.gov.evil.example/x", hosts)


def test_robots_wildcards_and_end_anchor():
    robots = RobotsRules(
        "User-agent: *\nDisallow: /*.json$\nDisallow: /readingroom/search/\nAllow: /readingroom/search/help.html\nCrawl-delay: 10",
        "ManticoreEducationalArchiver/1.0",
    )
    assert not robots.can_fetch("https://www.cia.gov/data/foo.json")
    assert robots.can_fetch("https://www.cia.gov/data/foo.json?download=1")
    assert not robots.can_fetch("https://www.cia.gov/readingroom/search/results")
    assert robots.can_fetch("https://www.cia.gov/readingroom/search/help.html")
    assert robots.crawl_delay == 10
