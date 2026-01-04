import re
import pytest
from unittest.mock import patch, MagicMock
from src.company_scraper import CompanyScraper

# 絶対/相対の /l/?uddg=..., 相対パス, 除外ドメインを含むサンプル
SAMPLE_HTML = """
<html>
  <body>
    <!-- 相対の /l/?uddg= -->
    <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fhome">Example</a>
    <!-- 絶対の /l/?uddg= -->
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fabs.co.jp%2F">ABS</a>
    <!-- 除外ドメイン -->
    <a class="result__a" href="https://facebook.com/profile">FB</a>
    <!-- プロトコルなし -->
    <a class="result__a" href="//bar.com/page">Bar</a>
    <!-- 相対パス -->
    <a class="result__a" href="/relative/path">Rel</a>
    <!-- 会社概要のパス -->
    <a class="result__a" href="https://example.co.jp/company/overview">Profile</a>
    <!-- 空 href -->
    <a class="result__a" href="">Empty</a>
    <!-- 旅行系集客ドメイン（除外対象） -->
    <a class="result__a" href="https://travel.rakuten.co.jp/hotel/123">Rakuten Travel</a>
    <!-- 除外ドメイン -->
    <a class="result__a" href="https://twitter.com/foo">TW</a>
  </body>
</html>
"""
@pytest.fixture
def scraper():
    sc = CompanyScraper(headless=True)
    sc.http_session = None  # テストでは requests.get のモックを使う
    return sc


def _strip_rep_tags(value: str) -> str:
    return re.sub(r"^(?:\[[A-Z_]+\])+", "", value or "").strip()


def test_search_engines_env_parsing(monkeypatch):
    monkeypatch.setenv("SEARCH_ENGINES", "duckduckgo,bing,unknown,DDG")
    sc = CompanyScraper(headless=True)
    assert sc.search_engines == ["ddg", "bing"]

@pytest.mark.asyncio
@patch("src.company_scraper.requests.get")
async def test_search_company_filters_and_resolves(mock_get, scraper):
    mock_response = MagicMock()
    mock_response.text = SAMPLE_HTML
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    urls = await scraper.search_company("トヨタ自動車株式会社", "愛知県豊田市", num_results=10)
    queries = {call.kwargs["params"]["q"] for call in mock_get.call_args_list}
    allowed_queries = {
        "トヨタ自動車株式会社 会社概要 公式",
        "トヨタ自動車株式会社 企業情報 公式",
        "トヨタ自動車株式会社 会社情報 公式",
        "トヨタ自動車株式会社 愛知県豊田市 会社概要 公式",
        "トヨタ自動車株式会社 愛知県豊田市 企業情報 公式",
        "トヨタ自動車株式会社 愛知県豊田市 会社情報 公式",
    }
    assert queries.issubset(allowed_queries)
    assert allowed_queries & queries

    # /l/?uddg= が正しく剥がれている（相対/絶対）
    assert "https://example.com/home" in urls
    assert any(u.startswith("https://abs.co.jp") for u in urls)

    # 除外ドメインが含まれない
    assert not any("facebook.com" in u for u in urls)
    assert not any("twitter.com" in u for u in urls)
    assert not any("rakuten" in u for u in urls)

    # プロトコルなし → https
    assert any(u.startswith("https://bar.com") for u in urls)

    # 空文字を含まない & 上限件数
    assert all(u for u in urls)
    assert len(urls) <= 10

@pytest.mark.asyncio
@patch("src.company_scraper.requests.get")
async def test_search_company_limit_num_results(mock_get, scraper):
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_HTML
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    urls = await scraper.search_company("社名", "住所", num_results=2)
    assert len(urls) == 2

@pytest.mark.asyncio
@patch("src.company_scraper.requests.get")
async def test_search_company_empty_on_http_error(mock_get, scraper):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP Error")
    mock_get.return_value = mock_resp

    urls = await scraper.search_company("社名", "住所")
    assert urls == []


def test_is_likely_official_site_true(scraper):
    text = "会社概要\n株式会社Exampleは・・・"
    assert scraper.is_likely_official_site(
        "株式会社Example",
        "https://www.example.co.jp/about",
        {"text": text, "html": f"<title>{text}</title>"},
    )

def test_clean_candidate_url_relative(scraper):
    assert scraper._clean_candidate_url("/relative/path") == "https://duckduckgo.com/relative/path"
    assert scraper._clean_candidate_url("//bar.com/page").startswith("https://")



def test_is_likely_official_site_false(scraper):
    text = "楽天トラベルで株式会社Exampleの宿泊プラン"
    assert not scraper.is_likely_official_site(
        "株式会社Example",
        "https://travel.rakuten.co.jp/hotel/123",
        {"text": text},
    )

@pytest.mark.asyncio
@patch("src.company_scraper.requests.get")
async def test_search_company_prefetch_excludes_directory_like_urls(mock_get, scraper):
    html = """
    <html><body>
      <a class="result__a" href="https://some-directory.example.com/company/12345">Dir</a>
      <a class="result__a" href="https://some-directory.example.com/detail?corporate_number=1234567890123">Dir2</a>
      <a class="result__a" href="https://example.co.jp/company/overview">Official</a>
    </body></html>
    """
    mock_response = MagicMock()
    mock_response.text = html
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    urls = await scraper.search_company("社名", "住所", num_results=10)
    assert "https://example.co.jp/company/overview" in urls
    assert not any("some-directory.example.com/company/12345" in u for u in urls)
    assert not any("some-directory.example.com/detail" in u for u in urls)


def test_is_likely_official_site_romaji(scraper):
    text = "会社概要とお問い合わせ"
    assert scraper.is_likely_official_site(
        "株式会社創明社",
        "https://someisha.co.jp",
        text,
    )


def test_normalize_homepage_url_contact(scraper):
    html = """
    <html>
      <head><title>お問い合わせ</title></head>
      <body>問い合わせフォームです。</body>
    </html>
    """
    normalized = scraper.normalize_homepage_url(
        "https://www.example.co.jp/contact/index.html",
        {"html": html},
    )
    assert normalized == "https://www.example.co.jp/"


def test_normalize_homepage_url_canonical(scraper):
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://corp.example.co.jp/company/overview" />
      </head>
      <body>会社概要</body>
    </html>
    """
    normalized = scraper.normalize_homepage_url(
        "https://corp.example.co.jp/company/overview?ref=ddg",
        {"html": html},
    )
    assert normalized == "https://corp.example.co.jp/company/overview"


def test_normalize_homepage_url_canonical_root_keeps_path(scraper):
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://www.example.co.jp/" />
      </head>
      <body>会社概要</body>
    </html>
    """
    normalized = scraper.normalize_homepage_url(
        "https://www.example.co.jp/c/158/1300",
        {"html": html},
    )
    assert normalized == "https://www.example.co.jp/c/158/1300"


def test_clean_rep_name_removes_union_title(scraper):
    assert scraper.clean_rep_name("組合長　田中太郎") == "田中太郎"
    assert scraper.clean_rep_name("代表理事組合長 田中太郎") == "田中太郎"
    assert scraper.clean_rep_name("組合長") is None


def test_clean_rep_name_handles_chairman_title(scraper):
    assert scraper.clean_rep_name("会長 佐藤太郎") == "佐藤太郎"
    assert scraper.clean_rep_name("会長") is None

def test_clean_rep_name_strips_exec_officer_variants(scraper):
    assert scraper.clean_rep_name("代表取締役社長執行役員 塩津伸男") == "塩津伸男"
    assert scraper.clean_rep_name("代表取締役社長 執行役員 塩津伸男") == "塩津伸男"

def test_clean_rep_name_rejects_generic_company_word(scraper):
    assert scraper.clean_rep_name("企業") is None

def test_clean_rep_name_allows_single_kanji_tokens(scraper):
    assert scraper.clean_rep_name("関 進") == "関 進"
    assert scraper.clean_rep_name("関進") == "関進"


def test_extract_candidates_keeps_full_rep_name(scraper):
    text = "会社概要\n所在地 東京都千代田区"
    html = """
    <table>
      <tr><th>代表取締役会長</th><td>飯野　靖司</td></tr>
      <tr><th>所在地</th><td>東京都千代田区</td></tr>
    </table>
    """
    cands = scraper.extract_candidates(text, html=html)
    assert any(_strip_rep_tags(name).replace(" ", "") == "飯野靖司" for name in cands["rep_names"])

def test_extract_candidates_rep_name_then_role(scraper):
    text = "役員紹介\n所在地 東京都千代田区"
    html = """
    <dl>
      <dt>代表取締役社長</dt><dd>平野井 順一</dd>
      <dt>所在地</dt><dd>東京都千代田区</dd>
    </dl>
    """
    cands = scraper.extract_candidates(text, html=html)
    assert any(_strip_rep_tags(name).replace(" ", "") == "平野井順一" for name in cands["rep_names"])

def test_extract_candidates_rep_picks_ceo_when_multiple(scraper):
    text = "企業情報\n代表者 代表取締役会長 関 進、代表取締役社長 関 裕之\n電話番号 044-210-1000（代）"
    html = """
    <table>
      <tr><th>代表者</th><td>代表取締役会長 関 進、代表取締役社長 関 裕之</td></tr>
      <tr><th>電話番号</th><td>044-210-1000（代）</td></tr>
    </table>
    """
    cands = scraper.extract_candidates(text, html=html)
    assert any(_strip_rep_tags(name).replace(" ", "").endswith("関裕之") for name in cands["rep_names"])

def test_extract_candidates_rep_rejects_long_paragraph(scraper):
    text = "代表取締役\n当社グループは、昭和32年4月に石炭配送を主体として創業しました。"
    html = """
    <div>代表取締役</div>
    <div>当社グループは、昭和32年4月に石炭配送を主体として創業しました。</div>
    """
    cands = scraper.extract_candidates(text, html=html)
    assert not any("グループ" in _strip_rep_tags(name) for name in cands["rep_names"])


def test_extract_candidates_finance_inline_variations(scraper):
    text = "事業概要 売上高 12億円 営業利益 ▲3百万円 年商 15億"
    cands = scraper.extract_candidates(text)
    assert "12億円" in cands["revenues"]
    assert "▲3百万円" in cands["profits"]
    assert any("15億" in rev for rev in cands["revenues"])


def test_extract_candidates_finance_loss_keywords(scraper):
    text = "財務データ 営業収入 120億円 営業損益 △3億円 純損失 5億円 は赤字2億円"
    cands = scraper.extract_candidates(text)
    assert "120億円" in cands["revenues"]
    assert any("△3億円" in p for p in cands["profits"])
    assert any("5億円" in p for p in cands["profits"])
    assert any("2億円" in p for p in cands["profits"])


def test_founded_year_jp_era(scraper):
    text = "創立 昭和53年3月20日"
    cands = scraper.extract_candidates(text)
    assert "1978" in cands["founded_years"]


def test_founded_year_english_era_letter(scraper):
    text = "設立 H2年4月1日"
    cands = scraper.extract_candidates(text)
    assert "1990" in cands["founded_years"]


def test_is_likely_official_site_excludes_known_aggregator(scraper):
    text = "観陽亭のご案内"
    assert not scraper.is_likely_official_site(
        "株式会社観陽亭",
        "https://tsukumado.com/member/51/",
        {"text": text},
    )


def test_is_likely_official_site_suspect_host_with_address(scraper):
    text = "〒409-2937 山梨県南巨摩郡身延町身延一色1350 株式会社創明社の公式サイトです。"
    extracted = {
        "addresses": ["〒409-2937 山梨県南巨摩郡身延町身延一色1350"],
    }
    assert scraper.is_likely_official_site(
        "株式会社創明社",
        "https://www.big-advance.site/c/158/1300",
        {"text": text},
        "〒409-2937 山梨県南巨摩郡身延町身延一色1350",
        extracted,
    )


def test_is_likely_official_site_suspect_host_without_address(scraper):
    text = "株式会社創明社の紹介ページです。"
    assert not scraper.is_likely_official_site(
        "株式会社創明社",
        "https://www.big-advance.site/c/158/1300",
        {"text": text},
    )


def test_is_likely_official_site_partial_address(scraper):
    text = "アクセス\n〒409-2524 山梨県南巨摩郡身延町身延3678 甘養亭製菓店"
    extracted = {
        "addresses": ["〒409-2524 山梨県南巨摩郡身延町身延3678"],
    }
    assert scraper.is_likely_official_site(
        "甘養亭製菓店",
        "https://www.kanyoutei.com/",
        {"text": text},
        "〒409-2524 上町3678",
        extracted,
    )


def test_is_likely_official_site_excludes_directory_url_even_if_name_and_address(scraper):
    html = """
    <html>
      <head><title>株式会社Example | 企業情報</title></head>
      <body>
        株式会社Example
        〒100-0001 東京都千代田区1-1-1
        掲載企業一覧 / 企業検索 / 企業データベース
      </body>
    </html>
    """
    details = scraper.is_likely_official_site(
        "株式会社Example",
        "https://some-directory.example.com/companies/12345",
        {"text": "株式会社Example 〒100-0001 東京都千代田区1-1-1 掲載企業 一覧 検索", "html": html},
        "〒100-0001 東京都千代田区1-1-1",
        {"addresses": ["〒100-0001 東京都千代田区1-1-1"]},
        return_details=True,
    )
    assert isinstance(details, dict)
    assert details["directory_like"] is True
    assert details["is_official"] is False


def test_is_likely_official_site_brand_domain_rescued_by_evidence(scraper):
    html = """
    <html>
      <head>
        <title>株式会社Example</title>
        <meta property="og:site_name" content="株式会社Example" />
        <script type="application/ld+json">
          {"@context":"https://schema.org","@type":"Organization","name":"株式会社Example","telephone":"03-0000-0000"}
        </script>
      </head>
      <body>
        <h1>株式会社Example</h1>
        <footer>© 株式会社Example</footer>
      </body>
    </html>
    """
    details = scraper.is_likely_official_site(
        "株式会社Example",
        "https://brand-example.com/",
        {"text": "株式会社Example 公式サイト", "html": html},
        "〒100-0001 東京都千代田区1-1-1",
        {"addresses": []},
        return_details=True,
    )
    assert isinstance(details, dict)
    assert details["official_evidence_score"] >= 9
    assert details["directory_like"] is False
    assert details["is_official"] is True


def test_is_likely_official_site_excludes_note(scraper):
    text = "[公式] 甘養亭製菓店の最新情報"
    assert not scraper.is_likely_official_site(
        "甘養亭製菓店",
        "https://note.com/company_official/n/abc",
        {"text": text},
    )


def test_is_likely_official_site_allows_google_sites(scraper):
    text = "会社概要\n建築資材専門商社 大田機材株式会社 〒113-0033 東京都文京区本郷3-35"
    extracted = {"addresses": ["〒113-0033 東京都文京区本郷3-35"]}
    assert scraper.is_likely_official_site(
        "大田機材株式会社",
        "https://sites.google.com/view/otakizai-n/",
        {"text": text},
        "〒113-0033 東京都文京区本郷3-35",
        extracted,
    )


def test_is_likely_official_site_details(scraper):
    text = "会社概要\n株式会社Exampleの公式サイト"
    details = scraper.is_likely_official_site(
        "株式会社Example",
        "https://www.example.co.jp/company",
        {"text": text},
        return_details=True,
    )
    assert isinstance(details, dict)
    assert details["is_official"] is True
    assert details["score"] >= 4


def test_extract_candidates_phone_with_parentheses(scraper):
    text = "基本情報\nTEL（0834）21-3641\nFAX（0834）32-3494"
    cands = scraper.extract_candidates(text)
    assert "0834-21-3641" in cands["phone_numbers"]


def test_extract_candidates_securities_code(scraper):
    text = "会社概要 証券コード：1234 東証プライム（9101）"
    cands = scraper.extract_candidates(text)
    assert any("1234" in l for l in cands["listings"])
    assert any("9101" in l for l in cands["listings"])


def test_extract_candidates_rejects_address_form_noise(scraper):
    text = (
        "住所\n"
        "Japan 郵便番号 (半角数字) 住所検索 都道府県 北海道 青森県 岩手県 宮城県 秋田県 "
        "山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 "
        "市区町村・番地 マンション・ビル名\n"
    )
    cands = scraper.extract_candidates(text)
    assert cands["addresses"] == []
