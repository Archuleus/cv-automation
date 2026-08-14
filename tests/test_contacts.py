"""Career-page discovery and published-address extraction."""

from __future__ import annotations

import pytest

from jobbot.contacts.assisted import links_for_company
from jobbot.contacts.emails import (
    best_hiring_email,
    contact_page_urls,
    extract_emails,
    score_address,
)
from jobbot.contacts.pages import (
    CAREER_PATHS,
    candidate_urls,
    find_career_links,
    is_careers_hub,
    looks_like_careers_page,
)

HOMEPAGE = """
<html><body>
  <nav>
    <a href="/hakkimizda">Hakkımızda</a>
    <a href="/urunler">Ürünler</a>
    <a href="/kariyer">Kariyer</a>
    <a href="https://blog.acme.com.tr/kariyer-yolculugu">Kariyer Yolculuğu (blog)</a>
  </nav>
  <footer><a href="/iletisim">İletişim</a></footer>
</body></html>
"""

CAREERS_PAGE = """
<html><body>
  <h1>Açık Pozisyonlar</h1>
  <p>Ekibimize katılmak için başvurun.</p>
  <a href="mailto:ik@acme.com.tr">ik@acme.com.tr</a>
  <p>Genel sorular: info@acme.com.tr</p>
</body></html>
"""


class TestCandidateUrls:
    def test_covers_turkish_and_english_paths(self):
        urls = candidate_urls("acme.com.tr")

        assert any(url.endswith("/kariyer") for url in urls)
        assert any(url.endswith("/careers") for url in urls)

    def test_turkish_paths_come_first(self):
        # This arm exists because the Turkish market is the gap; the cheapest
        # requests should be spent where the answer most likely is.
        first_english = next(i for i, path in enumerate(CAREER_PATHS) if path == "/careers")
        first_turkish = next(i for i, path in enumerate(CAREER_PATHS) if path == "/kariyer")

        assert first_turkish < first_english

    def test_normalises_the_domain(self):
        assert candidate_urls("WWW.Acme.com.tr/")[0].startswith("https://acme.com.tr/")


class TestLooksLikeCareersPage:
    def test_accepts_a_turkish_careers_page(self):
        assert looks_like_careers_page(CAREERS_PAGE)

    def test_accepts_an_english_careers_page(self):
        html = "<html><body><h1>Careers</h1><p>Open positions. Apply now.</p></body></html>"

        assert looks_like_careers_page(html)

    def test_rejects_a_soft_404_that_returns_the_homepage(self):
        # Many sites answer every path with 200 and their homepage, so the
        # status code proves nothing at all.
        html = "<html><body><h1>Acme</h1><p>Ürünlerimiz ve çözümlerimiz.</p></body></html>"

        assert not looks_like_careers_page(html)

    def test_rejects_empty_input(self):
        assert not looks_like_careers_page("")


class TestIsCareersHub:
    # Verbatim shape of Turkcell's /insan-kaynaklari page: a SharePoint shell
    # whose only visible text asks the browser to enable scripts, while the
    # real careers links sit in the HTML.
    JS_SHELL = """
    <html><body>
      <p>You may be trying to access this site from a secured browser on the
         server. Please enable scripts and reload this page.</p>
      <a href="/kariyer-firsatlari">Kariyer Fırsatları</a>
      <a href="https://kariyer.acme.com.tr/SignIn">Kariyer</a>
    </body></html>
    """

    def test_a_javascript_rendered_careers_page_is_recognised(self):
        # The text heuristic is right that there is no hiring vocabulary here,
        # and wrong that this is not a careers page.
        assert not looks_like_careers_page(self.JS_SHELL)
        assert is_careers_hub(self.JS_SHELL, url="https://acme.com.tr/insan-kaynaklari",
                              domain="acme.com.tr")

    def test_a_page_with_real_hiring_text_still_qualifies(self):
        assert is_careers_hub(CAREERS_PAGE, url="https://acme.com.tr/kariyer",
                              domain="acme.com.tr")

    def test_a_page_with_one_stray_career_link_does_not_qualify(self):
        # A single footer link must not promote an unrelated page.
        html = '<html><body><h1>Ürünler</h1><a href="/kariyer">Kariyer</a></body></html>'

        assert not is_careers_hub(html, url="https://acme.com.tr/urunler", domain="acme.com.tr")

    def test_an_empty_page_does_not_qualify(self):
        assert not is_careers_hub("", url="https://acme.com.tr/kariyer", domain="acme.com.tr")


class TestFindCareerLinks:
    def test_finds_the_careers_link(self):
        links = find_career_links(HOMEPAGE, base_url="https://acme.com.tr", domain="acme.com.tr")

        assert links
        assert links[0].url == "https://acme.com.tr/kariyer"

    def test_skips_a_blog_post_that_mentions_careers(self):
        links = find_career_links(HOMEPAGE, base_url="https://acme.com.tr", domain="acme.com.tr")

        assert not any("blog" in link.url for link in links)

    def test_keeps_an_offsite_ats_link_at_lower_confidence(self):
        html = '<a href="https://boards.greenhouse.io/acme">Careers</a>'

        links = find_career_links(html, base_url="https://acme.com", domain="acme.com")

        assert links[0].is_offsite
        assert "greenhouse" in links[0].url

    def test_ignores_mailto_and_anchors(self):
        html = '<a href="mailto:ik@acme.com">Kariyer</a><a href="#kariyer">Kariyer</a>'

        assert find_career_links(html, base_url="https://acme.com", domain="acme.com") == []

    def test_handles_a_page_with_no_links(self):
        assert find_career_links("<html></html>", base_url="https://a.com", domain="a.com") == []


class TestScoreAddress:
    @pytest.mark.parametrize(
        "address",
        ["ik@acme.com.tr", "kariyer@acme.com.tr", "hr@acme.com.tr", "careers@acme.com.tr"],
    )
    def test_hiring_addresses_score_highest(self, address: str):
        assert score_address(address, company_domain="acme.com.tr") >= 0.9

    def test_a_shared_inbox_scores_lower_than_a_hiring_one(self):
        hiring = score_address("ik@acme.com.tr", company_domain="acme.com.tr")
        generic = score_address("info@acme.com.tr", company_domain="acme.com.tr")

        assert generic < hiring

    def test_a_named_person_scores_lowest(self):
        # They did not ask to be written to.
        person = score_address("ahmet.yilmaz@acme.com.tr", company_domain="acme.com.tr")

        assert person < score_address("info@acme.com.tr", company_domain="acme.com.tr")

    def test_an_offsite_address_is_penalised(self):
        onsite = score_address("ik@acme.com.tr", company_domain="acme.com.tr")
        offsite = score_address("ik@gmail.com", company_domain="acme.com.tr")

        assert offsite < onsite

    def test_a_subdomain_counts_as_the_company(self):
        assert score_address("ik@mail.acme.com.tr", company_domain="acme.com.tr") >= 0.9


class TestExtractEmails:
    def test_finds_both_mailto_and_plain_text(self):
        found = extract_emails(CAREERS_PAGE, company_domain="acme.com.tr")

        addresses = {item.address for item in found}
        assert addresses == {"ik@acme.com.tr", "info@acme.com.tr"}

    def test_ranks_the_hiring_address_first(self):
        found = extract_emails(CAREERS_PAGE, company_domain="acme.com.tr")

        assert found[0].address == "ik@acme.com.tr"

    def test_trusts_a_mailto_slightly_more_than_loose_text(self):
        marked_up = extract_emails(
            '<a href="mailto:info@acme.com">x</a>', company_domain="acme.com"
        )
        loose = extract_emails("<p>info@acme.com</p>", company_domain="acme.com")

        assert marked_up[0].confidence > loose[0].confidence

    @pytest.mark.parametrize(
        "address",
        ["noreply@acme.com", "kvkk@acme.com", "webmaster@acme.com", "satis@acme.com"],
    )
    def test_drops_addresses_that_are_never_a_place_to_apply(self, address: str):
        found = extract_emails(f"<p>{address}</p>", company_domain="acme.com")

        assert found == []

    def test_ignores_asset_filenames_that_look_like_addresses(self):
        html = '<img src="sprite@2x.png"><p>logo@acme.svg</p>'

        assert extract_emails(html, company_domain="acme.com") == []

    def test_ignores_tracking_hashes(self):
        html = "<p>0123456789abcdef0123456789abcdef@acme.com</p>"

        assert extract_emails(html, company_domain="acme.com") == []

    def test_strips_trailing_punctuation(self):
        found = extract_emails("<p>Yazın: ik@acme.com.</p>", company_domain="acme.com")

        assert found[0].address == "ik@acme.com"

    def test_empty_page_yields_nothing(self):
        assert extract_emails("", company_domain="acme.com") == []


class TestNeverGuesses:
    """The rule that protects deliverability: only published addresses."""

    def test_a_page_with_no_address_produces_no_address(self):
        # No `ik@domain` is invented just because it would probably work. A
        # guess that bounces teaches the receiving system that this sender does
        # not know who it is writing to.
        html = "<html><body><h1>Kariyer</h1><p>Açık pozisyon yok.</p></body></html>"

        assert extract_emails(html, company_domain="acme.com.tr") == []

    def test_best_hiring_email_returns_none_rather_than_a_guess(self):
        assert best_hiring_email([]) is None

    def test_best_hiring_email_rejects_a_low_confidence_personal_address(self):
        found = extract_emails(
            "<p>ahmet.yilmaz@acme.com.tr</p>", company_domain="acme.com.tr"
        )

        assert found
        assert best_hiring_email(found) is None


class TestContactPageUrls:
    def test_covers_turkish_and_english_contact_paths(self):
        urls = contact_page_urls("acme.com.tr")

        assert any(url.endswith("/iletisim") for url in urls)
        assert any(url.endswith("/contact") for url in urls)


class TestAssistedLinks:
    def test_produces_links_without_fetching_anything(self):
        links = links_for_company("Trendyol")

        assert {link.site for link in links} == {"LinkedIn", "kariyer.net"}
        assert all(link.url.startswith("https://") for link in links)

    def test_linkedin_search_is_scoped_to_turkiye(self):
        # Without the geo id LinkedIn silently searches wherever the browser
        # appears to be.
        jobs = next(link for link in links_for_company("Getir") if "jobs" in link.url)

        assert "geoId=102105699" in jobs.url

    def test_company_names_are_url_encoded(self):
        link = links_for_company("Yapı Kredi")[0]

        assert " " not in link.url
