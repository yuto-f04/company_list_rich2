# src/company_scraper.py
import re, urllib.parse, json, os, time, logging
import asyncio
import unicodedata
from collections import deque
from typing import List, Dict, Any, Optional, Iterable
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page,
    TimeoutError as PlaywrightTimeoutError, Route
)

from .site_validator import extract_name_signals, score_name_match

try:
    from pykakasi import kakasi as _kakasi_constructor
except Exception:
    _kakasi_constructor = None

try:
    from unidecode import unidecode as _unidecode
except Exception:
    _unidecode = None

log = logging.getLogger(__name__)

# 深掘り時に優先して辿るパス（日本語含む）
PRIORITY_PATHS = [
    # 概要系（最優先）
    "/company", "/about", "/corporate", "/会社概要", "/企業情報", "/企業概要",
    "/会社情報", "/会社案内", "/法人案内", "/法人概要",
    # 連絡先系
    "/contact", "/access", "/location", "/head-office", "/headquarters", "/お問い合わせ", "/アクセス",
    # IR/決算系
    "/ir", "/investor", "/investor-relations", "/financial", "/disclosure", "/決算",
]
PRIO_WORDS = [
    # 概要系（最優先）
    "会社概要", "企業情報", "企業概要", "会社情報", "法人案内", "法人概要", "会社案内",
    # 連絡先系
    "お問い合わせ", "アクセス", "連絡先", "所在地", "本社", "本店", "窓口", "役員",
    "代表", "代表者", "代表取締役", "社長", "CEO", "ceo", "代表挨拶", "トップメッセージ", "メッセージ", "役員紹介",
    # IR/決算系
    "IR", "ir", "investor", "financial", "ディスクロージャー", "決算",
]
ANCHOR_PRIORITY_WORDS = [
    # 概要系
    "会社概要", "企業情報", "法人案内", "法人概要", "会社案内", "会社紹介", "会社情報", "法人紹介",
    # 連絡先系
    "お問い合わせ", "連絡先", "アクセス", "所在地", "本社", "本店", "窓口", "役員",
    "代表", "代表者", "代表取締役", "社長", "CEO", "ceo", "代表挨拶", "トップメッセージ", "メッセージ", "役員紹介",
    # IR/決算系
    "IR", "ir", "investor", "financial", "ディスクロージャー", "決算",
    # 英語系
    "about", "corporate",
]
PRIORITY_SECTION_KEYWORDS = (
    "contact", "contacts", "inquiry", "support", "contact-us",
    "会社概要", "会社案内", "法人案内", "法人概要", "企業情報", "企業概要",
    "団体概要", "施設案内", "園紹介", "学校案内", "沿革", "会社情報",
    "corporate", "about", "profile", "overview", "information", "access",
    "お問い合わせ", "連絡先", "アクセス", "窓口", "役員",
    "代表", "代表者", "代表取締役", "社長", "CEO", "ceo", "代表挨拶", "トップメッセージ", "メッセージ", "役員紹介",
    "決算", "ir", "investor", "ディスクロージャー", "financial"
)
PRIORITY_CONTACT_KEYWORDS = (
    "contact", "お問い合わせ", "連絡先", "tel", "電話", "アクセス", "窓口", "ir", "investor"
)
PREFECTURE_NAMES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
PREFECTURE_NAME_RE = re.compile("|".join(re.escape(p) for p in PREFECTURE_NAMES))
REP_NAME_EXACT_BLOCKLIST = {
        "ブログ", "blog", "Blog", "BLOG",
        "ニュース", "News", "news",
        "お知らせ", "採用", "求人", "Recruit", "recruit",
        "代表者", "代表者名", "氏名", "お名前", "名前",
        "アクセス", "Access", "access",
        "お問い合わせ", "Contact", "contact",
        "Info", "info", "Information", "information",
        "法人案内", "法人概要", "会社案内", "会社概要", "会社情報", "基本情報",
        "法人情報", "企業情報", "事業案内", "事業紹介",
        "サイトマップ", "Sitemap", "sitemap",
        "交通案内", "アクセスマップ",
        "施設案内", "施設情報",
        "イベント", "トピックス", "Topics", "topics",
        "スタッフ紹介", "スタッフ",
        "メニュー", "Menu", "menu",
        "トップページ", "Home", "home", "ホーム",
        "文字", "文字サイズ",
        "沿革", "法人紹介", "会社紹介",
        "交代", "任を仰せつかりました",
        "所属",
    }
REP_NAME_SUBSTR_BLOCKLIST = (
    "ブログ", "news", "お知らせ", "採用", "求人", "recruit",
        "代表者", "代表者名", "氏名", "お名前", "名前", "担当", "担当者",
    "問い合わせ", "お問い合わせ", "お問合せ", "問合せ",
    "アクセス", "contact", "法人案内", "法人概要", "会社案内", "会社概要",
    "法人情報", "企業情報", "事業案内", "事業紹介",
    "サイトマップ", "sitemap", "交通案内", "アクセスマップ",
    "施設案内", "施設情報", "イベント", "トピックス",
    "スタッフ紹介", "スタッフ", "メニュー", "menu",
    "トップページ", "home", "沿革", "法人紹介", "会社紹介", "会社情報", "基本情報",
    "に関する", "について", "保管", "業務", "役割", "委員会",
    "学校", "学園", "大学", "保育園", "こども園", "組合", "協会",
    "センター", "法人", "こと", "公印", "いただき", "役", "組織"
)
REP_NAME_EXACT_BLOCKLIST_LOWER = {s.lower() for s in REP_NAME_EXACT_BLOCKLIST}
NAME_CHUNK_RE = re.compile(r"[\u4E00-\u9FFF]{1,3}(?:[\u30fb\uff65\s\u3000]{0,1}[\u4E00-\u9FFF]{1,3})+")
KANA_NAME_RE = re.compile(r"[\u3041-\u3096\u30A1-\u30FA\u30FC]{2,}(?:[\u3041-\u3096\u30A1-\u30FA\u30FC\s][\u3041-\u3096\u30A1-\u30FA\u30FC]{2,})+")
REP_BUSINESS_TERMS = (
    "事業",
    "経営",
    "美容",
    "美容室",
    "美容院",
    "サロン",
    "会社概要",
    "店舗",
    "サービス",
    "内容",
    "紹介",
    "概要",
)

PHONE_RE = re.compile(
    r"(?:TEL|Tel|tel|電話)?\s*[:：]?\s*"
    r"(?:\+?81[-‐―－ー–—.\s]*)?"
    r"[\(（]?(0?\d{1,4})[\)）]?\s*"
    r"[-‐―－ー–—.\s]*"
    r"(\d{1,4})\s*"
    r"[-‐―－ー–—.\s]*"
    r"(\d{3,4})"
)

COOKIE_PRIVACY_LINE_RE = re.compile(
    r"(cookie|クッキー|cookie\s*policy|privacy\s*policy|プライバシ|個人情報|利用規約|免責|GDPR|同意|consent|同意する|同意します|拒否|accept|reject)",
    re.IGNORECASE,
)

NAV_LIKE_LINE_RE = re.compile(
    r"^(?:ホーム|home|トップ|top|会社概要|企業情報|会社情報|事業内容|サービス|製品|採用|求人|ニュース|お知らせ|ブログ|blog|お問い合わせ|contact|アクセス|sitemap|サイトマップ|menu|メニュー)$",
    re.IGNORECASE,
)

COOKIE_NODE_HINT_RE = re.compile(r"(cookie|consent|gdpr|privacy|banner|modal|popup)", re.IGNORECASE)

def _normalize_phone_strict(raw: str) -> Optional[str]:
    if not raw:
        return None
    hyphen_match = re.search(
        r"(0\d{1,4})\D+?(\d{1,4})\D+?(\d{3,4})",
        raw,
    )
    if hyphen_match:
        return f"{hyphen_match.group(1)}-{hyphen_match.group(2)}-{hyphen_match.group(3)}"
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("81") and len(digits) >= 10:
        digits = "0" + digits[2:]
    if digits in {"0123456789", "81112345678"}:
        return None
    if not digits.startswith("0") or len(digits) not in (10, 11):
        return None
    m = re.search(r"^(0\d{1,4})(\d{2,4})(\d{3,4})$", digits)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
ZIP_RE = re.compile(r"(〒?\s*\d{3})[-‐―－ー]?(\d{4})")
ADDR_HINT = re.compile(r"(都|道|府|県).+?(市|区|郡|町|村)")
ADDR_FALLBACK_RE = re.compile(
    r"(〒\d{3}-\d{4}[^\n。]{1,}|[一-龥]{2,3}[都道府県][^。\n]{0,120}[市区町村郡][^。\n]{0,140})"
)
CITY_RE = re.compile(r"([一-龥]{2,6}(?:市|区|町|村|郡))")
ADDRESS_FORM_NOISE_RE = re.compile(
    r"(住所検索|都道府県|市区町村|マンション・?ビル名|郵便番号\s*[（(]?\s*半角)",
    re.IGNORECASE,
)
_BINARY_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
REP_RE = re.compile(
    r"(?:代表者|代表取締役|理事長|学長|院長|組合長|会頭|会長|社長)"
    r"(?:\s*[:：]\s*|\s+)"
    r"([一-龥ァ-ンA-Za-z][^\n\r<>\|（）\(\)]{0,39})"
)
LISTING_RE = re.compile(r"(?:上場(?:区分|市場|先)?|株式上場|未上場|非上場|未公開|非公開)\s*[:：]?\s*([^\s、。\n]+)")
KANJI_AMOUNT_CHARS = "0-9０-９零〇一二三四五六七八九十百千万億兆"

CAPITAL_RE = re.compile(
    rf"資本金\s*[:：]?\s*([△▲-]?\s*[{KANJI_AMOUNT_CHARS},\.]+(?:兆|億|万|千)?(?:円|百万円|千円|万円)?)"
)
REVENUE_RE = re.compile(
    r"(?:売上高|売上|売上収益|売上額|営業収益|営業収入|事業収益|年商|売上総額|売上金額|売上高（連結）|売上収益（連結）|売上高\(連結\)|売上収益\(連結\))"
    r"\s*[:：]?\s*"
    rf"([△▲-]?\s*[{KANJI_AMOUNT_CHARS},\.]+(?:兆|億|万|千)?(?:円|百万円|千円|万円)?)"
)
PROFIT_RE = re.compile(
    r"(?:営業利益|経常利益|純利益|当期純利益|営業損益|経常損益|税引後利益|純損益|損益|損失|赤字|営業利益（連結）|経常利益（連結）|純利益（連結）)"
    r"\s*[:：]?\s*"
    rf"([△▲-]?\s*[{KANJI_AMOUNT_CHARS},\.]+(?:兆|億|万|千)?(?:円|百万円|千円|万円)?)"
)
FISCAL_RE = re.compile(
    r"(?:決算(?:月|期|日)?|会計年度|会計期)\s*[:：]?\s*([0-9０-９]{1,2}月(?:末)?|[0-9０-９]{1,2}月期|Q[1-4])",
    re.IGNORECASE,
)
LISTING_KEYWORDS = ("非上場", "未上場", "未公開", "非公開", "上場予定なし")
FOUNDED_RE = re.compile(
    r"[（(]?(?:設立|創業|創立)\s*[:：]?\s*"
    r"((?:明治|大正|昭和|平成|令和|M|T|S|H|R)?\s*[0-9０-９元]{1,4})"
    r"年[）)]?"
)

TABLE_LABEL_MAP = {
    "rep_names": (
        "代表者",
        "代表取締役",
        "代表者名",
        "代表",
        "代表者氏名",
        "代表名",
        "会長",
        "社長",
        "理事長",
        "代表社員",
        "代表理事",
        "組合長",
        "院長",
        "学長",
        "園長",
        "校長",
        "役員",
    ),
    "capitals": ("資本金", "出資金", "資本金(百万円)", "資本金(万円)"),
    "revenues": (
        "売上高", "売上", "売上額", "売上収益", "収益", "営業収益", "営業収入", "事業収益", "年商", "売上総額", "売上金額",
        "売上高（連結）", "売上収益（連結）", "売上高(連結)", "売上収益(連結)"
    ),
    "profits": (
        "利益", "営業利益", "経常利益", "純利益", "当期純利益", "営業損益", "経常損益", "税引後利益", "純損益", "損益", "損失", "赤字",
        "営業利益（連結）", "経常利益（連結）", "純利益（連結）"
    ),
    "fiscal_months": ("決算月", "決算期", "決算日", "決算", "会計期", "会計年度", "決算期(年)"),
    "founded_years": ("設立", "創業", "創立", "設立年", "創立年", "創業年"),
    "listing": ("上場区分", "上場", "市場", "上場先", "証券コード", "非上場", "未上場", "コード番号"),
    "phone_numbers": ("電話", "電話番号", "TEL", "Tel", "連絡先"),
    "addresses": ("所在地", "住所", "本社所在地", "所在地住所", "所在地(本社)", "本社", "本店"),
}
SECURITIES_CODE_RE = re.compile(
    r"(?:証券コード|証券ｺｰﾄﾞ|証券番号|コード番号)\s*[:：]?\s*([0-9]{4})"
)
MARKET_CODE_RE = re.compile(
    r"(?:東証(?:プライム|スタンダード|グロース)?|TSE|JASDAQ|マザーズ)[^0-9]{0,6}?([0-9]{4})",
    re.IGNORECASE,
)


class CompanyScraper:
    PREFECTURE_NAMES = PREFECTURE_NAMES
    """
    DuckDuckGo 非JS(html.duckduckgo.com/html)で検索 → 上位リンク取得
    ＋ Playwrightで本文/スクショ取得。
    各ワーカーでブラウザ/コンテキストを使い回して高速化＆安定化。
    """

    # 除外したいドメイン（口コミ/地図/求人など）
    EXCLUDE_DOMAINS = [
        "facebook.com", "twitter.com", "instagram.com", "x.com",
        "linkedin.com", "youtube.com",
        "google.com/maps", "maps.google.com", "map.yahoo.co.jp", "mapion.co.jp",
        "yahoo.co.jp", "itp.ne.jp", "hotpepper.jp", "r.gnavi.co.jp",
        "tabelog.com", "ekiten.jp", "goo.ne.jp", "recruit.net", "en-gage.net",
        "townpage.goo.ne.jp", "jp-hp.com",
        "hotfrog.jp", "jigyodan.jp",
        "buffett-code.com",
        "nikkei.com",
        "kaisharesearch.com",
        "kensetsumap.com",
        # プレスリリース/求人系（公式でないことが多い）
        "prtimes.jp", "valuepress.jp", "dreamnews.jp",
        "wantedly.com", "openwork.jp", "en-gage.jp",
        # 法人番号・企業DB系
        "corporate-number.com", "houjin-no.com",
        # 求人・まとめ系（公式サイトではないケースが多い）
        "job.goo.to", "job.goo.ne.jp", "job.goo.jp",
        "job.rikunabi.com", "townwork.net", "froma.com",
        "careerindex.jp", "hatalike.jp", "ten-navi.com",
        # 集客・旅行・ショッピング系（公式サイトではないケースが多い）
        "rakuten.co.jp", "rakuten.com", "travelko.com", "jalan.net",
        "ikyu.com", "rurubu.jp", "booking.com", "expedia.co.jp",
        "agoda.com", "tripadvisor.jp", "tripadvisor.com", "hotels.com",
        "travel.yahoo.co.jp", "trivago.jp", "trivago.com",
        "jalan.jp", "asoview.com", "tabikobo.com",
        # 求人・転職系（公式サイトではないケースが多い）
        "mynavi.jp", "tenshoku.mynavi.jp", "rikunabi.jp", "indeed.com", "doda.jp",
        "en-japan.com", "type.jp", "careerconnection.jp", "find-job.net",
        "jobstreet.jp",
        # 企業データベース系
        "info.gbiz.go.jp", "gbiz.go.jp", "salesnow.jp", "baseconnect.in",
        "r-compass.jp", "coki.jp",
        # 介護系まとめ/紹介
        "kaigostar.net",
        "houjin.info",
        # 重い/公式でないケースが多いドメイン
        "biz-maps.com", "catr.jp", "data-link-plus.com", "metoree.com",
        # 海外系まとめ/掲示板
        "zhihu.com", "baidu.com", "tieba.baidu.com", "sogou.com", "sohu.com",
        "weibo.com", "bilibili.com", "douban.com", "toutiao.com", "qq.com",
        # レシピ/料理/ブログ系で誤爆したドメイン
        "mychicagosteak.com", "thepioneerwoman.com", "sipbitego.com",
        "foodnetwork.com", "delish.com", "allrecipes.com", "tasteofhome.com",
        # 企業DBまとめ系（公式ではない）
        "founded-today.com",
    ]

    PRIORITY_PATHS = [
        "/company", "/about", "/profile", "/corporate", "/overview",
        "/contact", "/inquiry", "/access", "/info", "/information",
        "/gaiyou", "/gaiyo", "/gaiyou.html",
        "/会社概要", "/企業情報", "/企業概要", "/会社情報", "/会社案内", "/法人案内", "/法人概要",
        "/団体概要", "/施設案内", "/施設情報", "/法人情報", "/事業案内", "/事業紹介",
        "/窓口案内", "/お問い合わせ", "/アクセス", "/沿革", "/組織図",
    ]

    HARD_EXCLUDE_HOSTS = {
        "travel.rakuten.co.jp",
        "navitime.co.jp",
        "ja.wikipedia.org",
        "kensetumap.com",
        "kaisharesearch.com",
        "houjin.jp",
        "houjin.me",
        "tokubai.co.jp",
        "itp.ne.jp",
        "hotpepper.jp",
        "tblg.jp",
        "retty.me",
        "goguynet.jp",
        "yahoo.co.jp",
        "mapion.co.jp",
        "google.com",
        "tsukumado.com",
        "note.com",
        "note.jp",
        "note.mu",
        "datagojp.com",
        "landwatch.info",
        "g-search.or.jp",
        "buffett-code.com",
        "info.gbiz.go.jp",
        "gbiz.go.jp",
        "salesnow.jp",
        "baseconnect.in",
        "r-compass.jp",
        "coki.jp",
        "biz-maps.com",
        "data-link-plus.com",
        "gmo-connect.com",
        "musubu.jp",
        "jpdb.biz",
        "houjin-bangou.nta.go.jp",
        "irbank.net",
        "stockclip.net",
        "kabutan.jp",
        "minkabu.com",
        "marketscreener.com",
        "bloomberg.com",
        "alarmbox.jp",
        "infomart.co.jp",
        "fumadata.com",
        "tokyo-seihon.or.jp",
        "nikkei.com",
        "datagipo.jp",
        "kaigostar.net",
        "kensetsumap.com",
        "houjin.info",
        "job.goo.to",
        "job.goo.ne.jp",
        "job.goo.jp",
        "job.rikunabi.com",
        "townwork.net",
        "froma.com",
        "careerindex.jp",
        "hatalike.jp",
        "ten-navi.com",
        "prtimes.jp",
        "valuepress.jp",
        "dreamnews.jp",
        "wantedly.com",
        "openwork.jp",
        "en-gage.jp",
        "corporate-number.com",
        "houjin-no.com",
        "zhihu.com",
        "baidu.com",
        "tieba.baidu.com",
        "sogou.com",
        "sohu.com",
        "weibo.com",
        "bilibili.com",
        "douban.com",
        "toutiao.com",
        "qq.com",
        "akala.ai",
        "catr.jp",
        "metoree.com",
    }

    SUSPECT_HOSTS = {
        "big-advance.site",
        "ameblo.jp",
        "blog.jp",
        "ja-jp.facebook.com",
    }

    GOV_ENTITY_KEYWORDS = (
        "県", "府", "都", "道", "市", "区", "町", "村", "庁", "役所",
        "議会", "連合", "連絡協議会", "消防", "警察", "公共", "振興局",
        "上下水道", "教育委員会", "広域", "公社", "公団", "自治体", "道路公社",
    )
    EDU_ENTITY_KEYWORDS = (
        "学校", "学校法人", "大学", "学院", "高等学校", "高校",
        "中学校", "小学校", "幼稚園", "こども園", "保育園", "専門学校",
    )
    MED_ENTITY_KEYWORDS = (
        "病院", "クリニック", "診療所", "医療法人", "社会医療法人",
        "保健", "衛生", "看護", "福祉", "介護", "社会福祉法人",
    )
    NPO_ENTITY_KEYWORDS = (
        "社団法人", "財団法人", "公益社団", "公益財団", "一般社団", "一般財団",
        "協会", "連盟", "組合", "商工会", "協議会", "NPO", "非営利",
    )

    ENTITY_SITE_SUFFIXES = {
        "gov": (".lg.jp", ".go.jp"),
        "edu": (".ac.jp", ".ed.jp"),
        "med": (".or.jp", ".go.jp"),
        "npo": (".or.jp",),
    }

    NON_OFFICIAL_KEYWORDS = {
        "recruit", "career", "job", "jobs", "kyujin", "haken", "派遣",
        "hotel", "travel", "tour", "booking", "reservation", "yoyaku",
        "mall", "store", "shop", "coupon", "catalog", "price",
        "seikyu", "delivery", "ranking", "review", "口コミ", "比較",
        # 料理/ブログ/まとめ系の誤爆抑止
        "recipe", "cooking", "food", "gourmet", "kitchen", "steak", "bbq", "grill",
        "university", "blog", "press", "news",
    }

    NON_OFFICIAL_SNIPPET_KEYWORDS = (
        "口コミ", "求人", "求人情報", "転職", "派遣", "予約", "地図", "アクセスマップ",
        "リストス", "上場区分", "企業情報サイト", "まとめ", "一覧", "ランキング", "プラン",
        "sales promotion", "booking", "reservation", "hotel", "travel", "camp",
    )
    # 企業DB/ディレクトリ系の強いシグナル（URLパス＋本文）
    CORPORATE_NUMBER_RE = re.compile(r"(?<!\d)\d{13}(?!\d)")
    DIRECTORY_URL_PATTERNS = (
        re.compile(r"/companies/\d+(?:/|$)", re.IGNORECASE),
        re.compile(r"/company/\d+(?:/|$)", re.IGNORECASE),
        re.compile(r"/detail/\d+(?:/|$)", re.IGNORECASE),
        re.compile(r"/(?:directory|listing|db)(?:/|$)", re.IGNORECASE),
        re.compile(r"/search(?:/|$)", re.IGNORECASE),
    )
    # URLだけで高精度に企業DB/ディレクトリを疑えるパターン（prefetch用）
    DIRECTORY_URL_PATTERNS_STRONG = (
        re.compile(r"/(?:companies|company|corp|corporation|detail)/\d+(?:/|$)", re.IGNORECASE),
        re.compile(r"/(?:companies|company|corp|corporation|detail)/\d{13}(?:/|$)", re.IGNORECASE),
        re.compile(r"/\d{13}(?:/|$)", re.IGNORECASE),
    )
    DIRECTORY_QUERY_ID_KEYS = ("company_id", "companyid", "cid", "id", "detail_id", "detailid")
    DIRECTORY_QUERY_CORP_KEYS = ("corporate_number", "corporatenumber", "hojin_no", "houjin_no", "hojinbango", "corporateno")
    DIRECTORY_TEXT_KEYWORDS_STRONG = (
        "掲載企業",
        "掲載情報",
        "掲載企業数",
        "企業一覧",
        "企業検索",
        "企業を探す",
        "検索",
        "絞り込み",
        "他社",
        "企業データベース",
        "企業db",
        "企業情報db",
        "企業情報サイト",
        "法人番号",
        "この企業情報は",
        "掲載している企業",
    )
    DIRECTORY_TEXT_PATTERNS = (
        re.compile(r"(企業|会社).{0,6}(一覧|検索|データベース|db)", re.IGNORECASE),
        re.compile(r"(掲載|登録).{0,8}(企業|会社)", re.IGNORECASE),
    )
    EXEC_TITLE_KEYWORDS = (
        "代表取締役", "代表理事", "代表者", "社長", "会長", "理事長", "学長",
        "園長", "校長", "院長", "組合長", "議長", "知事", "市長", "区長", "町長", "村長",
    )

    CORP_SUFFIXES = [
        "株式会社", "（株）", "(株)", "有限会社", "合同会社", "合名会社", "合資会社",
        "Inc.", "Inc", "Co.", "Co", "Corporation", "Company", "Ltd.", "Ltd",
        "Holding", "Holdings", "HD", "グループ", "ホールディングス", "本社",
    ]
    # 公式判定で優先的に許容するTLD（汎用TLDは追加の名称/住所一致が必須）
    ALLOWED_OFFICIAL_TLDS = (
        ".co.jp", ".or.jp", ".ac.jp", ".ed.jp", ".go.jp", ".lg.jp", ".gr.jp",
        ".jp", ".com", ".net", ".org", ".biz", ".info", ".co",
    )
    GENERIC_TLDS = (".com", ".net", ".org", ".biz", ".info", ".co")
    ALLOWED_HOST_WHITELIST = {"big-advance.site"}

    # 優先的に巡回したいURLのキーワード
    CANDIDATE_PRIORITIES = (
        "会社概要", "会社情報", "企業情報", "corporate", "about",
        "お問い合わせ", "問い合わせ", "contact",
        "アクセス", "access", "本社", "所在地", "沿革",
    )
    PROFILE_URL_HINTS = (
        "company", "about", "profile", "corporate", "overview", "info",
        "information", "gaiyou", "gaiyo", "kaisya", "outline",
        "companyinfo", "company-information",
    )

    _romaji_converter = None  # lazy pykakasi converter

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.browser_disabled = False
        self.browser_disabled_reason = ""
        # ページ単位のタイムアウトを短めに（デフォルト7秒）
        self.page_timeout_ms = int(os.getenv("PAGE_TIMEOUT_MS", "7000"))
        self.slow_page_threshold_ms = int(os.getenv("SLOW_PAGE_THRESHOLD_MS", "7000"))
        # networkidle は長く待つと誤検知になりやすいので短めに抑える
        self.network_idle_timeout_ms = int(os.getenv("NETWORK_IDLE_TIMEOUT_MS", "1500"))
        # load イベント待ちも短めに抑えて遅延を避ける
        self.load_wait_timeout_ms = int(os.getenv("LOAD_WAIT_TIMEOUT_MS", "2500"))
        self.skip_slow_hosts = os.getenv("SKIP_SLOW_HOSTS", "true").lower() == "true"
        self.slow_hosts: Dict[str, Dict[str, int]] = {}
        self.slow_hosts_path = os.getenv("SLOW_HOSTS_PATH", "logs/slow_hosts.txt")
        self.slow_host_ttl_sec = int(os.getenv("SLOW_HOST_TTL_SEC", str(7 * 24 * 3600)))
        self.slow_host_hits = max(1, int(os.getenv("SLOW_HOST_HITS", "2")))
        self.page_cache: Dict[str, Dict[str, Any]] = {}
        self.use_http_first = os.getenv("USE_HTTP_FIRST", "true").lower() == "true"
        self.http_timeout_ms = int(os.getenv("HTTP_TIMEOUT_MS", "6000"))
        self.search_cache: Dict[tuple[str, str], List[str]] = {}
        # 共有 HTTP セッションでコネクションを再利用し、検索/HTTP取得のレイテンシを抑える
        self.http_session: Optional[requests.Session] = requests.Session()
        # 検索エンジン（環境変数 SEARCH_ENGINES=ddg,bing 等で指定。既定は ddg）
        raw_engines = os.getenv("SEARCH_ENGINES", "ddg")
        engines: list[str] = []
        for part in (raw_engines or "").replace(" ", ",").split(","):
            token = (part or "").strip().lower()
            if not token:
                continue
            if token in {"duckduckgo", "ddg"}:
                token = "ddg"
            if token not in {"ddg", "bing"}:
                continue
            if token not in engines:
                engines.append(token)
        self.search_engines = engines or ["ddg"]
        # 代表者は構造化ソース（テーブル/ラベル/JSON-LD）のみ許可するか
        self.rep_strict_sources = os.getenv("REP_STRICT_SOURCES", "true").lower() == "true"
        # ブラウザ操作専用セマフォ（HTTPとは別枠で制御し渋滞を防ぐ）
        self.browser_concurrency = max(1, int(os.getenv("BROWSER_CONCURRENCY", "1")))
        self._browser_sem: asyncio.Semaphore | None = None
        self._load_slow_hosts()

    def _get_browser_sem(self) -> asyncio.Semaphore:
        if self._browser_sem is None:
            self._browser_sem = asyncio.Semaphore(self.browser_concurrency)
        return self._browser_sem

    def _session_get(self, url: str, **kwargs: Any):
        """
        requests.Session を優先的に使いつつ、None 設定時は requests.get にフォールバック。
        """
        if self.http_session:
            return self.http_session.get(url, **kwargs)
        return requests.get(url, **kwargs)

    @staticmethod
    def _detect_html_encoding(resp: requests.Response, raw: bytes) -> str:
        """
        レスポンスヘッダが ISO-8859-1 固定で返ってくるサイト対策。
        - meta charset を優先
        - それが無ければ apparent_encoding（chardet）を利用
        - 最後に UTF-8
        """
        try:
            if raw:
                m = re.search(br'charset=["\']?([\w-]+)', raw[:10240], flags=re.I)
                if m:
                    return m.group(1).decode("ascii", "ignore") or "utf-8"
        except Exception:
            pass
        if resp.apparent_encoding:
            return resp.apparent_encoding
        return "utf-8"

    # ===== 公式判定ヘルパ =====
    @classmethod
    def _normalize_company_name(cls, company_name: str) -> str:
        if not company_name:
            return ""
        norm = unicodedata.normalize("NFKC", company_name)
        for suffix in cls.CORP_SUFFIXES:
            norm = norm.replace(suffix, "")
        norm = re.sub(r"[\s　]+", "", norm)
        return norm

    @classmethod
    def _detect_entity_tags(cls, company_name: str) -> set[str]:
        tags: set[str] = set()
        if not company_name:
            return tags
        name = unicodedata.normalize("NFKC", company_name)
        if any(keyword in name for keyword in cls.GOV_ENTITY_KEYWORDS):
            tags.add("gov")
        if any(keyword in name for keyword in cls.EDU_ENTITY_KEYWORDS):
            tags.add("edu")
        if any(keyword in name for keyword in cls.MED_ENTITY_KEYWORDS):
            tags.add("med")
        if any(keyword in name for keyword in cls.NPO_ENTITY_KEYWORDS):
            tags.add("npo")
        return tags

    @classmethod
    def _is_exec_title(cls, label: str) -> bool:
        if not label:
            return False
        normalized = unicodedata.normalize("NFKC", label)
        return any(keyword in normalized for keyword in cls.EXEC_TITLE_KEYWORDS)

    @classmethod
    def _romanize(cls, text: str) -> str:
        if not text:
            return ""
        if _kakasi_constructor:
            try:
                if cls._romaji_converter is None:
                    cls._romaji_converter = _kakasi_constructor()
                converter = cls._romaji_converter
                if hasattr(converter, "convert"):
                    parts = converter.convert(text)
                    converted = "".join(
                        item.get("hepburn") or item.get("kana") or item.get("hira") or ""
                        for item in parts
                    )
                    if converted:
                        return converted
                elif hasattr(converter, "getConverter"):
                    legacy = converter.getConverter()
                    converted = legacy.do(text)
                    if converted:
                        return converted
                elif callable(converter):
                    converted = str(converter(text))
                    if converted:
                        return converted
            except Exception:
                cls._romaji_converter = None
        if _unidecode:
            try:
                converted = _unidecode(text)
                if converted:
                    return converted
            except Exception:
                pass
        return ""

    @classmethod
    def _company_tokens(cls, company_name: str) -> List[str]:
        norm = cls._normalize_company_name(company_name)
        tokens = cls._ascii_tokens(norm)
        romaji = cls._romanize(norm)
        romaji_ascii = cls._ascii_tokens(romaji)
        tokens.extend(romaji_ascii)

        compact = re.sub(r"[^A-Za-z0-9]", "", romaji or "").lower()
        if len(compact) >= 4:
            tokens.append(compact)

        parts = [p for p in re.split(r"[^A-Za-z0-9]+", (romaji or "").lower()) if len(p) >= 2]
        for i in range(len(parts)):
            joined = parts[i]
            if len(joined) >= 4:
                tokens.append(joined)
            for j in range(i + 1, min(len(parts), i + 3)):
                joined += parts[j]
                if len(joined) >= 4:
                    tokens.append(joined)

        # カタカナのスペルアウトをアルファベットに変換して頭文字を拾う（例: アイ・アイ・エム -> iim）
        katakana_map = [
            ("ダブリュー", "w"), ("エックス", "x"), ("ジェー", "j"), ("ジェイ", "j"),
            ("シー", "c"), ("スリー", "3"), ("ツー", "2"), ("ワイ", "y"), ("ゼット", "z"),
            ("ビー", "b"), ("ディー", "d"), ("エム", "m"), ("エヌ", "n"), ("ピー", "p"),
            ("キュー", "q"), ("エル", "l"), ("エフ", "f"), ("エイチ", "h"), ("エー", "a"),
            ("ビー", "b"), ("シー", "c"), ("ディー", "d"), ("イー", "e"), ("アール", "r"),
            ("エス", "s"), ("ティー", "t"), ("ユー", "u"), ("ブイ", "v"), ("ズィー", "z"),
            ("アイ", "i"), ("ジェー", "j"), ("ケー", "k"), ("エックス", "x"), ("ダブル", "w"),
            ("キュー", "q"), ("ワイ", "y"), ("ゼット", "z"), ("ゼロ", "0"), ("オー", "o"),
        ]
        katakana_patterns = sorted(katakana_map, key=lambda x: len(x[0]), reverse=True)
        katakana_parts = re.findall(r"[ァ-ヶー]+", company_name or "")

        def katakana_to_acronym(text: str) -> str:
            out = ""
            i = 0
            while i < len(text):
                matched = False
                for k, v in katakana_patterns:
                    if text.startswith(k, i):
                        out += v
                        i += len(k)
                        matched = True
                        break
                if not matched:
                    i += 1
            return out

        for kp in katakana_parts:
            acr = katakana_to_acronym(kp)
            if len(acr) >= 2:
                tokens.append(acr.lower())

        # ローマ字→英語の簡易変換（数字・色などの定番パターン）
        translate_map = {
            "ichi": "one", "ni": "two", "san": "three", "yon": "four", "shi": "four",
            "go": "five", "roku": "six", "nana": "seven", "shichi": "seven",
            "hachi": "eight", "kyu": "nine", "ku": "nine", "ju": "ten", "juu": "ten",
            "hyaku": "hundred", "sen": "thousand", "man": "man", "oku": "oku",
            "aka": "red", "ao": "blue", "midori": "green", "kuro": "black", "shiro": "white",
        }
        translated: List[str] = []
        for tok in list(tokens):
            for jp, en in translate_map.items():
                if jp in tok:
                    cand = tok.replace(jp, en)
                    if len(cand) >= 4:
                        translated.append(cand)
        tokens.extend(translated)

        seen: set[str] = set()
        ordered: List[str] = []
        for tok in tokens:
            if not tok or tok in seen:
                continue
            seen.add(tok)
            ordered.append(tok)
        return ordered

    @staticmethod
    def _host_matches_suffix(host: str, suffix: str) -> bool:
        suffix = suffix.lstrip(".")
        return host.endswith(suffix)

    @classmethod
    def _allowed_official_host(cls, url: str) -> tuple[str, str, bool, bool, bool]:
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.netloc or "").lower().split(":")[0]
            path_lower = (parsed.path or "").lower()
        except Exception:
            return "", "", False, False, False

        allowed_tld = any(host.endswith(tld) for tld in cls.ALLOWED_OFFICIAL_TLDS)
        whitelist_hit = any(host == wh or host.endswith(f".{wh}") for wh in cls.ALLOWED_HOST_WHITELIST)
        is_google_sites = host == "sites.google.com" or (host.endswith(".google.com") and "sites" in path_lower)
        return host, path_lower, allowed_tld, whitelist_hit, is_google_sites

    def is_relevant_profile_url(self, company_name: str, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc.lower()
            path_lower = (parsed.path or "").lower()
        except Exception:
            return False
        keyword_hit = any(hint in path_lower for hint in self.PROFILE_URL_HINTS)
        entity_tags = self._detect_entity_tags(company_name)
        tokens = self._company_tokens(company_name)
        host_token_hit = self._host_token_hit(tokens, url) if tokens else False
        score = self._domain_score(tokens, url)

        entity_host_match = False
        for tag in entity_tags:
            allowed = self.ENTITY_SITE_SUFFIXES.get(tag, ())
            if allowed and any(self._host_matches_suffix(host, suffix) for suffix in allowed):
                entity_host_match = True
                break

        if "gov" in entity_tags and not entity_host_match:
            return False

        if not tokens and not entity_host_match:
            return keyword_hit and score >= 2

        if not entity_host_match and not host_token_hit:
            return False

        if entity_host_match:
            return keyword_hit or host_token_hit or score >= 1

        # host_token_hit is guaranteed True beyond this point
        return keyword_hit or score >= 1

    @staticmethod
    def _ascii_tokens(text: str) -> List[str]:
        return [tok.lower() for tok in re.findall(r"[A-Za-z0-9]{2,}", text or "")]

    @staticmethod
    def _extract_name_chunk(text: str) -> Optional[str]:
        matches = NAME_CHUNK_RE.findall(text or "")
        cleaned: List[str] = []
        for m in matches:
            candidate = re.sub(r"\s+", "", m.strip())
            if any(stop in candidate for stop in ("法人", "学校", "協会", "委員会", "役員", "学校長")):
                continue
            if 2 <= len(candidate) <= 6:
                cleaned.append(candidate)
        if cleaned:
            return cleaned[-1]
        return None

    @staticmethod
    def _convert_jp_era_to_year(text: str) -> Optional[str]:
        norm = unicodedata.normalize("NFKC", text or "").strip()
        if not norm:
            return None
        era_map = {
            "明治": 1868, "M": 1868, "m": 1868,
            "大正": 1912, "T": 1912, "t": 1912,
            "昭和": 1926, "S": 1926, "s": 1926,
            "平成": 1989, "H": 1989, "h": 1989,
            "令和": 2019, "R": 2019, "r": 2019,
        }
        m = re.search(r"(明治|大正|昭和|平成|令和|[MTSHRmtsr])\s*([0-9０-９]+|元)", norm)
        if not m:
            return None
        era = m.group(1)
        year_str = m.group(2)
        base = era_map.get(era)
        if base is None:
            return None
        year_num = 1 if year_str == "元" else int(unicodedata.normalize("NFKC", year_str))
        if year_num <= 0 or year_num > 300:
            return None
        return str(base + year_num - 1)

    @staticmethod
    def _parse_founded_year(value: str) -> Optional[str]:
        norm = unicodedata.normalize("NFKC", value or "")
        era_year = CompanyScraper._convert_jp_era_to_year(norm)
        if era_year:
            return era_year
        m4 = re.search(r"([12]\d{3})\s*年?", norm)
        if m4:
            return m4.group(1)
        return None

    @staticmethod
    def _looks_mojibake(text: str) -> bool:
        if not text:
            return False
        if "\ufffd" in text:
            return True
        if re.search(r"[ぁ-んァ-ン一-龥]", text):
            return False
        latin_count = sum(1 for ch in text if "\u00c0" <= ch <= "\u00ff")
        if latin_count >= 3 and latin_count / max(len(text), 1) >= 0.15:
            return True
        return bool(re.search(r"[ÃÂãâæçïðñöøûüÿ]", text) and latin_count >= 2)

    @staticmethod
    def _is_address_form_noise(text: str) -> bool:
        if not text:
            return False
        s = unicodedata.normalize("NFKC", str(text)).strip()
        if not s:
            return False
        if ADDRESS_FORM_NOISE_RE.search(s):
            return True
        if "郵便番号" in s and not ZIP_RE.search(s):
            return True
        try:
            pref_hits = sum(1 for pref in PREFECTURE_NAMES if pref and pref in s)
        except Exception:
            pref_hits = 0
        return pref_hits >= 3

    @staticmethod
    def _normalize_address_candidate(val: str) -> str:
        if not val:
            return ""
        if CompanyScraper._looks_mojibake(str(val)):
            return ""
        val = unicodedata.normalize("NFKC", val)
        # HTML断片を除去
        val = re.sub(r"(?is)<style.*?>.*?</style>", " ", val)
        val = re.sub(r"(?is)<!--.*?-->", " ", val)
        val = re.sub(r"<[^>]+>", " ", val)
        val = re.sub(r"\bbr\s*/?\b", " ", val, flags=re.I)
        val = re.sub(r'\b(?:class|id|style|data-[\w-]+)\s*=\s*"[^"]*"', " ", val, flags=re.I)
        val = val.replace(">", " ").replace("<", " ")
        val = val.replace("&nbsp;", " ")
        val = re.sub(r"(?i)<br\s*/?>", " ", val)
        val = val.replace("\u3000", " ")
        val = re.sub(r"[‐―－ー–—]", "-", val)
        # JS/トラッキング断片が混入するケースを早めにカット
        val = re.split(
            r"(window\.\w+|dataLayer\s*=|gtm\.|googletagmanager|nr-data\.net|newrelic|bam\.nr-data\.net|function\s*\(|<script|</script>|gac?\.push|gtag|_gaq)",
            val,
            maxsplit=1,
        )[0]
        # 以降の余計な部分（TELやリンクの残骸）をカット
        val = re.split(r"(?:TEL|Tel|tel|電話|☎|℡|FAX|Fax|fax|ファックス)[:：.．]?\s*", val)[0]
        val = re.split(r"https?://|href=|HREF=", val)[0]
        # 地図/マップ/アクセスはここでもカット
        val = re.split(r"(地図アプリ|地図で見る|マップ|Google\s*マップ|map|アクセス|アクセスマップ|ルート|経路|Directions?)", val, maxsplit=1, flags=re.I)[0]
        arrow_idx = min([idx for idx in (val.find("→"), val.find("⇒")) if idx >= 0], default=-1)
        if arrow_idx >= 0:
            val = val[:arrow_idx]
        # 先頭に「住所/所在地」などのラベルが混入したケースを除去
        for _ in range(3):
            cleaned = re.sub(r"^(?:【\s*)?(?:本社|本店)?(?:所在地|住所)(?:】\s*)?\s*[:：]?\s*", "", val).strip()
            if cleaned == val:
                break
            val = cleaned
        # 住所の後ろに付くことが多い非住所要素（従業員/許可/設立など）で打ち切る
        tail_re = re.compile(
            r"\s*(?:"
            r"従業員(?:数)?|社員(?:数)?|職員(?:数)?|スタッフ(?:数)?|人数|"
            r"営業時間|受付時間|定休日|"
            r"代表者|代表取締役|取締役|社長|会長|理事長|"
            r"資本金|設立|創業|沿革|"
            r"(?:一般|特定)?(?:貨物|運送|建設|産廃|産業廃棄物|古物)?(?:業)?(?:許可|免許|登録|届出)|"
            r"事業内容|サービス|"
            r"お問い合わせ|お問合せ|問い合わせ|採用|求人"
            r")\b",
            re.IGNORECASE,
        )
        m_tail = tail_re.search(val)
        if m_tail:
            val = val[: m_tail.start()].strip()
        val = re.sub(r"\s+", " ", val).strip(" \"'")
        # 地図・マップ系が出たらそこまででカット
        m_map = re.search(r"(地図アプリ|マップ|Google\s*マップ|地図|map)", val, flags=re.I)
        if m_map:
            val = val[: m_map.start()].strip()
        if not val:
            return ""
        if CompanyScraper._is_address_form_noise(val):
            return ""
        return val

    @staticmethod
    def _clean_text_value(val: str) -> str:
        if not val:
            return ""
        cleaned = unicodedata.normalize("NFKC", val)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = re.sub(r"(?i)<br\s*/?>", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @staticmethod
    def _is_amount_like(val: str) -> bool:
        if not val:
            return False
        if re.search(r"(従業員|社員|職員|スタッフ)", val):
            return False
        if re.search(r"(名|人)\b", val):
            return False
        if not re.search(rf"[{KANJI_AMOUNT_CHARS}]", val):
            return False
        return bool(re.search(r"(円|万|億|兆)", val))

    @staticmethod
    def looks_like_address(text: str) -> bool:
        if not text:
            return False
        s = text.strip()
        if not s:
            return False
        if CompanyScraper._is_address_form_noise(s):
            return False
        if ZIP_RE.search(s):
            return True
        pref = CompanyScraper._extract_prefecture(s) or (PREFECTURE_NAME_RE.search(s).group(0) if PREFECTURE_NAME_RE.search(s) else "")
        has_city = bool(CITY_RE.search(s))
        if pref and has_city:
            return True
        if (pref or has_city) and re.search(r"(丁目|番地|号|ビル|マンション)", s) and re.search(r"\d", s):
            return True
        return False

    @staticmethod
    def _looks_like_person_name(name: str) -> bool:
        """
        代表者名として妥当かを軽く確認する。ラベル横の値の精度を上げるために使用。
        """
        if not name:
            return False
        if len(name) > 15:
            return False
        if any(term in name for term in REP_BUSINESS_TERMS):
            return False
        if re.search(r"[0-9@]", name):
            return False
        has_kanji = bool(NAME_CHUNK_RE.search(name))
        has_kana = bool(KANA_NAME_RE.search(name))
        if has_kana and not has_kanji:
            # Kana-only names should have a separator to avoid generic nouns (e.g., グループ).
            if " " not in name and "・" not in name and "･" not in name:
                return False
        return bool(has_kanji or has_kana)

    @staticmethod
    def _looks_like_full_address(text: str) -> bool:
        """
        住所として採用するための厳しめチェック:
        - 郵便番号がある、または
        - 都道府県 + 市区町村 が両方含まれる
        """
        if not text:
            return False
        s = text.strip()
        if not s:
            return False
        if CompanyScraper._is_address_form_noise(s):
            return False
        m = ZIP_RE.search(s)
        if m:
            # 郵便番号以外の本体が十分あるか確認
            body = s[m.end():].strip()
            if len(body) >= 1:
                return True
        pref = CompanyScraper._extract_prefecture(s)
        city = CompanyScraper._extract_city(s)
        return bool(pref and city)

    @staticmethod
    def clean_rep_name(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        text = str(raw).replace("\u200b", "").strip()
        if not text:
            return None
        news_words = ("退任", "就任", "人事", "異動", "お知らせ", "ニュース", "プレスリリース")
        if any(w in text for w in news_words):
            return None
        text = unicodedata.normalize("NFKC", text)
        text_norm = text.strip()
        if not text_norm:
            return None
        if re.fullmatch(r"[-ー―‐—–]+", text_norm):
            return None
        placeholders = {
            "未定",
            "準備中",
            "未公開",
            "非公開",
            "不明",
            "未記載",
            "掲載なし",
            "なし",
            "無し",
            "未登録",
            "該当なし",
            "n/a",
            "na",
            "none",
            "null",
        }
        text_lower = text_norm.lower()
        if text_norm in placeholders or text_lower in placeholders:
            return None
        text = text_norm
        # strip label-like prefixes when merged into the value
        text = re.sub(r"^(?:代表者名?|代表取締役|代表者|代表|社長|会長|理事長|院長|学長|園長|校長|CEO)\s*[:：]\s*", "", text)
        text = re.sub(r"^(?:氏名|お名前|名前)\s*[:：]\s*", "", text)
        text = re.sub(r"^(?:代表者名?|代表取締役|代表者|代表|社長|会長|理事長|院長|学長|園長|校長|CEO)\s+", "", text)
        text = re.sub(r"^(?:氏名|お名前|名前)\s+", "", text)
        # remove trailing "others" markers
        text = re.sub(r"(?:他|ほか|外|等)\s*\d*(?:名|人)?\s*$", "", text)
        # remove parentheses content
        text = re.sub(r"[（(][^）)]*[）)]", "", text)
        # keep only segment before punctuation/newline
        text = re.split(r"(?i)(?:TEL|電話|FAX|メール|E-mail|住所|所在地|本社)", text, maxsplit=1)[0]
        text = re.split(r"[、。\n/|｜,;；]", text)[0]
        text = text.strip(" 　:：-‐―－ー'\"/／")
        titles = (
            "代表取締役社長", "代表取締役副社長", "代表取締役会長", "代表取締役",
            "代表社員", "代表理事", "代表理事長", "代表執行役", "代表執行役社長",
            "代表取締役社長執行役員",
            "代表執行役社長兼CEO", "代表取締役社長兼CEO", "代表取締役社長CEO",
            "代表取締役社長兼COO", "代表取締役社長兼社長執行役員",
            "代表者", "代表", "代表主宰", "代表校長",
            "理事長", "学長", "園長", "校長", "院長", "所長", "館長", "組合長",
            "支配人", "店主", "会長", "社長", "総支配人", "CEO", "COO", "CFO", "代表取締役副会長",
        )
        while True:
            removed = False
            for t in titles:
                if text.startswith(t):
                    text = text[len(t):]
                    removed = True
                    break
            if not removed:
                break
        text = text.strip(" 　")
        # 代表者欄は役職が重なりやすいので、先頭/末尾の役職語を追加で剥がす
        role_words = (
            "社長執行役員",
            "執行役員",
            "取締役社長",
            "取締役会長",
            "取締役副社長",
            "専務取締役",
            "常務取締役",
            "取締役",
            "執行役",
            "監査役",
            "非常勤取締役",
            "常勤監査役",
            "会長",
            "社長",
            "CEO",
            "COO",
            "CFO",
            "CTO",
        )
        while True:
            before = text
            for w in role_words:
                text = re.sub(rf"^{re.escape(w)}[\\s　]*", "", text)
                text = re.sub(rf"[\\s　]*{re.escape(w)}$", "", text)
            text = text.strip(" 　")
            if text == before:
                break
        if text.endswith(("氏", "様", "さま", "殿", "先生")):
            text = text[:-1]
        text = re.sub(r"(と申します|といたします|になります|させていただきます|いたします|いたしました)$", "", text)
        text = re.sub(r"^(の|当社|当園|当組合|当法人|弊社|弊園|弊組合|私|わたくし)", "", text)
        text = re.sub(r"\s+", " ", text)
        text = text.strip()
        if not text:
            return None
        compact = re.sub(r"[\s\u3000]+", "", text)
        kana_only = bool(re.fullmatch(r"[ぁ-んァ-ンー・･\s]+", text))
        if kana_only and KANA_NAME_RE.search(text):
            text = re.sub(r"\s+", " ", text).strip()
            if 2 <= len(text) <= 40:
                return text
        if len(text) < 2 or len(text) > 40:
            return None
        generic_words = {
            "氏名",
            "お名前",
            "名前",
            "name",
            "Name",
            "NAME",
            "役職",
            "役名",
            "役割",
            "担当",
            "担当者",
            "選任",
            "代表者",
            "代表者名",
            "企業",
            "法人",
            "会社",
        }
        if text in generic_words or compact in generic_words:
            return None
        if re.search(r"(氏名|お名前|名前|役職|役名|役割|担当|担当者|選任|代表者)", text) or re.search(r"(氏名|お名前|名前|役職|役名|役割|担当|担当者|選任|代表者)", compact):
            return None
        if re.search(r"(概要|会社概要|事業概要|法人概要)", text) or re.search(r"(概要|会社概要|事業概要|法人概要)", compact):
            return None
        if any(word in text for word in ("株式会社", "有限会社", "合名会社", "合資会社", "合同会社")) or any(word in compact for word in ("株式会社", "有限会社", "合名会社", "合資会社", "合同会社")):
            return None
        for stop in (
            "創業", "創立", "創設", "メッセージ", "ご挨拶", "からの", "決裁",
            "沿革", "代表挨拶", "お問い合わせ", "お問合せ", "問合せ", "取引先", "主な取引",
            "顧問", "顧問弁護士", "顧問社労士", "弁護士", "司法書士", "行政書士", "税理士", "社労士",
        ):
            if stop in text or stop in compact:
                return None
        for stop in ("就任", "あいさつ", "ごあいさつ", "挨拶", "あいさつ文", "就任のご挨拶"):
            if stop in text or stop in compact:
                return None
        # 住所/所在地が混入しているケースを除外
        if re.search(r"(所在地|住所|本社|所在地:|住所:)", text) or re.search(r"(所在地|住所|本社|所在地:|住所:)", compact):
            return None
        # 業務/部門名が混入しているケースを除外
        business_terms = (
            "建設", "建築", "土木", "工事", "施工", "管理", "品質", "安全", "環境",
            "技術", "技能", "営業", "企画", "製造", "サービス", "メンテ", "生産", "部", "課", "室"
        )
        if any(term in text for term in business_terms) or any(term in compact for term in business_terms):
            return None
        # 役職併記を除去してから判定（兼社長/兼CEO 等）
        text = re.sub(r"兼.{0,10}$", "", text)
        text = re.sub(r"(CEO|COO|CFO)$", "", text, flags=re.I)
        text = text.strip(" 　-‐―－ー/／")
        if not text:
            return None
        # 文末の助詞/説明終端を落とす
        text = re.sub(r"(さん|は|です|でした|となります|となっております)$", "", text).strip()
        lower_text = text.lower()
        compact = re.sub(r"[\s\u3000]+", "", text)
        lower_compact = compact.lower()
        if (
            text in REP_NAME_EXACT_BLOCKLIST
            or lower_text in REP_NAME_EXACT_BLOCKLIST_LOWER
            or compact in REP_NAME_EXACT_BLOCKLIST
            or lower_compact in REP_NAME_EXACT_BLOCKLIST_LOWER
        ):
            return None
        for stop_word in REP_NAME_SUBSTR_BLOCKLIST:
            if stop_word in text or stop_word in lower_text or stop_word in compact or stop_word in lower_compact:
                return None
        if text in PREFECTURE_NAMES or compact in PREFECTURE_NAMES:
            return None
        if "@" in text or re.search(r"https?://", text):
            return None
        # 役職語が残る場合は「氏名だけ」に寄せてから再判定（代表者欄の表記ゆれ対策）
        text = re.sub(r"^(?:取締役|執行役(?:員)?|監査役|役員)\\s*", "", text)
        text = re.sub(r"\\s*(?:取締役|執行役(?:員)?|監査役|役員)$", "", text)
        text = text.strip(" 　")
        if not text:
            return None
        # 役職だけ（取締役/部長等）になってしまったケースを除外
        if re.fullmatch(r"(取締役|執行役(?:員)?|監査役|役員|部長|課長|主任|係長|担当|マネージャ|manager|director)", text, flags=re.I):
            return None
        # 氏名らしさチェック（1文字姓も許容: 例「関 進」「東 太郎」）
        if not (NAME_CHUNK_RE.search(text) or KANA_NAME_RE.search(text)):
            return None
        tokens = [tok for tok in re.split(r"\s+", text) if tok]
        if len(tokens) > 4:
            return None
        if any(len(tok) > 8 for tok in tokens if re.search(r"[一-龥]", tok)):
            return None
        policy_words = (
            "社是",
            "社訓",
            "方針",
            "理念",
            "スローガン",
            "ミッション",
            "ビジョン",
            "バリュー",
            "ポリシー",
            "コンプライアンス",
            "品質",
            "環境",
            "安全",
            "情報セキュリティ",
            "マネジメント",
        )
        if any(word in text for word in policy_words):
            return None
        if re.search(r"(こと|する|される|ます|でした|いたします|いただき)", text):
            return None
        if re.search(r"(登録されていません|未登録|準備中|編集中)", text):
            return None
        if not re.search(r"[一-龥ぁ-んァ-ン]", text):
            return None
        return text

    def _build_company_queries(self, company_name: str, address: Optional[str]) -> List[str]:
        """
        会社名に公式+情報系キーワードを付けたクエリを生成する。
        """
        base_name = (company_name or "").strip()
        if not base_name:
            return []
        # 住所は「都道府県+市区町村」程度に圧縮して検索精度を上げる（誤った住所が混ざる場合に備え、住所無しクエリも併用）
        loc = ""
        addr_norm = unicodedata.normalize("NFKC", address or "").strip()
        # フォーム由来ノイズ/補助情報を軽く除去（入力CSVの品質が悪いケース対策）
        if addr_norm:
            addr_norm = re.sub(r"[（(]\s*(?:市区町村|自治体)コ[-ー]ド\s*[:：]\s*\d+\s*[)）]", "", addr_norm)
            addr_norm = re.sub(r"(?:市区町村|自治体)コ[-ー]ド\s*[:：]\s*\d+", "", addr_norm)
            addr_norm = re.sub(r"\s+", " ", addr_norm).strip()
        if addr_norm:
            pref = self._extract_prefecture(addr_norm)
            city_match = CITY_RE.search(addr_norm)
            city = city_match.group(1) if city_match else ""
            if pref and city and city.startswith(pref):
                loc = city.strip()
            else:
                loc = "".join([pref, city]).strip()

        base_queries = [
            f"{base_name} 会社概要 公式",
            f"{base_name} 企業情報 公式",
            f"{base_name} 会社情報 公式",
        ]
        if loc:
            base_queries.extend(
                [
                    f"{base_name} {loc} 会社概要 公式",
                    f"{base_name} {loc} 企業情報 公式",
                    f"{base_name} {loc} 会社情報 公式",
                ]
            )
        # 末尾/重複を正規化しつつ、順序を維持して重複排除
        seen: set[str] = set()
        ordered: list[str] = []
        for q in base_queries:
            qn = re.sub(r"\s+", " ", q).strip()
            if not qn or qn in seen:
                continue
            seen.add(qn)
            ordered.append(qn)
        return ordered

    @staticmethod
    def _domain_tokens(url: str) -> List[str]:
        host = urlparse(url).netloc.lower()
        host = host.split(":")[0]
        pieces = re.split(r"[.\-]", host)
        ignore = {"www", "co", "or", "ne", "go", "gr", "ed", "lg", "jp", "com", "net", "biz", "inc"}
        return [p for p in pieces if p and p not in ignore]

    def _host_token_hit(self, company_tokens: List[str], url: str) -> bool:
        try:
            host = urlparse(url).netloc.lower().split(":")[0]
        except Exception:
            return False
        host_compact = host.replace("-", "").replace(".", "")
        domain_tokens = self._domain_tokens(url)
        generic_tokens = {"system", "systems", "tech", "technology", "consulting", "solution", "solutions", "soft", "software", "works", "service", "services", "info", "web"}
        for token in company_tokens:
            if not token:
                continue
            if token in host or token in host_compact:
                if len(token) <= 2 or token in generic_tokens:
                    continue
                return True
            if any(token in dt for dt in domain_tokens):
                if len(token) <= 2 or token in generic_tokens:
                    continue
                return True
            # ローマ字表記揺れ（echo/eiko/eiko-sha等）を許容するゆるい類似判定
            for dt in domain_tokens:
                if dt in generic_tokens:
                    continue
                if len(token) >= 4 and len(dt) >= 4:
                    try:
                        ratio = SequenceMatcher(None, token, dt).ratio()
                    except Exception:
                        ratio = 0.0
                    if ratio >= 0.85:
                        return True
                if (
                    len(token) >= 4
                    and len(dt) >= 4
                    and (token.startswith(dt) or dt.startswith(token))
                ):
                    return True
        return False

    def _domain_score(self, company_tokens: List[str], url: str) -> int:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        host_no_port = host.split(":")[0]
        host_compact = host_no_port.replace("-", "").replace(".", "")
        path_lower = (parsed.path or "").lower()
        score = 0

        # 法人TLD加点
        if re.search(r"\.(co|or|go|ac)\.jp$", host_no_port):
            score += 3
        elif host_no_port.endswith(".jp"):
            score += 2
        elif host_no_port.endswith(".com") or host_no_port.endswith(".net"):
            score += 1

        domain_tokens = self._domain_tokens(url)
        generic_tokens = {"system", "systems", "tech", "technology", "consulting", "solution", "solutions", "soft", "software", "works", "service", "services", "info", "web"}

        for token in company_tokens:
            if not token:
                continue
            token_len = len(token)
            if token_len <= 2:
                continue
            exact = token in host_no_port or token in host_compact
            if exact:
                score += 6
            if any(token in dt for dt in domain_tokens):
                score += 4
            if token in path_lower:
                score += 3
            if token_len >= 5:
                for dt in domain_tokens:
                    if dt in generic_tokens:
                        continue
                    try:
                        ratio = SequenceMatcher(None, token, dt).ratio()
                    except Exception:
                        ratio = 0
                    if ratio >= 0.88:
                        score += 2
                        break

        # 一般語ホストは減点
        if any(dt in generic_tokens for dt in domain_tokens):
            score -= 2

        # パスが会社情報系なら底上げ
        for marker in ("/company", "/about", "/profile", "/overview", "/corporate"):
            if marker in path_lower:
                score += 3
                break
        # 採用/ブログ等なら減点
        for marker in ("/recruit", "/careers", "/job", "/jobs", "/blog", "/news", "/ir/"):
            if marker in path_lower:
                score -= 2
                break

        lowered = host_no_port + path_lower
        if any(kw in lowered for kw in self.NON_OFFICIAL_KEYWORDS):
            score -= 3
        return score

    def _path_priority_value(self, url: str) -> int:
        try:
            path = urllib.parse.urlparse(url).path.lower()
        except Exception:
            return 0
        score = 0
        for idx, marker in enumerate(self.PRIORITY_PATHS):
            if marker.lower() in path:
                score += max(6 - idx, 1)
        # 公式に寄せるパスの底上げ
        for marker in ("/company", "/about", "/profile", "/overview", "/corporate"):
            if marker in path:
                score += 3
                break
        # 非公式に寄せるパスの軽い減点
        for marker in ("/recruit", "/careers", "/job", "/jobs", "/blog", "/news", "/store", "/shop"):
            if marker in path:
                score -= 2
                break
        return score

    def _is_excluded(self, url: str) -> bool:
        lowered = (url or "").lower()
        if any(ex in lowered for ex in self.EXCLUDE_DOMAINS):
            return True
        # fetch前に企業DB/ディレクトリ臭が強いURLを弾く（多少の未取得は許容、誤爆回避優先）
        try:
            directory = self._detect_directory_like(url or "", text="", html="")
            if directory.get("is_directory_like"):
                return True
        except Exception:
            pass
        return False

    def _clean_candidate_url(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        href = self._decode_uddg(raw)
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://duckduckgo.com", href)
        lowered_full = href.lower()
        if self._is_excluded(lowered_full):
            return None
        for kw in ("recipe", "cooking", "steak", "food", "gourmet", "kitchen"):
            if kw in lowered_full:
                return None
        try:
            path_lower = urllib.parse.urlparse(href).path.lower()
            if any(path_lower.endswith(ext) for ext in _BINARY_EXTS):
                return None
        except Exception:
            pass
        return href

    def _is_ddg_challenge(self, html: str) -> bool:
        if not html:
            return False
        lowered = html.lower()
        return (
            "anomaly-modal__title" in lowered
            or "duckduckgo.com/anomaly.js" in lowered
            or "select all squares containing a duck" in lowered
            or "bots use duckduckgo too" in lowered
        )

    async def _fetch_duckduckgo_via_proxy(self, query: str) -> str:
        try:
            proxy_url = "https://r.jina.ai/https://duckduckgo.com/html/"
            resp = await asyncio.to_thread(
                self._session_get,
                proxy_url,
                params={"q": query, "kl": "jp-jp"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=(5, 30),
            )
            resp.raise_for_status()
            return resp.text
        except Exception:
            return ""

    def _extract_search_urls(self, html: str) -> Iterable[str]:
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.select("a.result__a")
        if anchors:
            for a in anchors:
                cleaned = self._clean_candidate_url(a.get("href"))
                if not cleaned or self._is_excluded(cleaned):
                    continue
                yield cleaned
            return

        # Proxy経由のレスポンスはMarkdown/テキスト形式なので手動抽出する
        for match in re.findall(r"https://duckduckgo\.com/l/\?uddg=[^\s)]+", html or ""):
            cleaned = self._clean_candidate_url(match)
            if not cleaned or self._is_excluded(cleaned):
                continue
            yield cleaned

    def _extract_bing_urls(self, html: str) -> Iterable[str]:
        soup = BeautifulSoup(html, "html.parser")
        for block in soup.select("li.b_algo h2 a"):
            href = block.get("href")
            if not href:
                continue
            href = href.strip()
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = urllib.parse.urljoin("https://www.bing.com", href)
            if "bing.com/ck/a" in href.lower():
                try:
                    parsed = urllib.parse.urlparse(href)
                    qs = urllib.parse.parse_qs(parsed.query or "")
                    if "u" in qs and qs["u"]:
                        decoded = urllib.parse.unquote(qs["u"][0])
                        if decoded.startswith("a1") and len(decoded) > 2:
                            import base64
                            try:
                                decoded = base64.urlsafe_b64decode(decoded[2:] + "=" * (-len(decoded[2:]) % 4)).decode("utf-8")
                            except Exception:
                                decoded = decoded[2:]
                        if decoded:
                            href = decoded
                except Exception:
                    pass
            if not href or self._is_excluded(href):
                continue
            yield href

    async def _fetch_duckduckgo(self, query: str) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ja,en-US;q=0.9",
            "Referer": "https://duckduckgo.com/",
        }
        for attempt in range(3):
            try:
                resp = await asyncio.to_thread(
                    self._session_get,
                    "https://html.duckduckgo.com/html",
                    params={"q": query, "kl": "jp-jp"},
                    headers=headers,
                    timeout=(3, 10),
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(0.8 * (2 ** attempt))
                    continue
                resp.raise_for_status()
                text = resp.text
                if self._is_ddg_challenge(text):
                    proxy_html = await self._fetch_duckduckgo_via_proxy(query)
                    if proxy_html:
                        return proxy_html
                    await asyncio.sleep(0.8 * (2 ** attempt))
                    continue
                return text
            except Exception:
                if attempt == 1:
                    return await self._fetch_duckduckgo_via_proxy(query)
                await asyncio.sleep(0.8 * (2 ** attempt))
        return ""

    async def _fetch_bing(self, query: str) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ja,en-US;q=0.9",
            "Referer": "https://www.bing.com/",
        }
        params = {"q": query, "setlang": "ja", "mkt": "ja-JP"}
        for attempt in range(3):
            try:
                resp = await asyncio.to_thread(
                    self._session_get,
                    "https://www.bing.com/search",
                    params=params,
                    headers=headers,
                    timeout=(5, 30),
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(0.8 * (2 ** attempt))
                    continue
                resp.raise_for_status()
                return resp.text
            except Exception:
                if attempt == 2:
                    return ""
                await asyncio.sleep(0.8 * (2 ** attempt))
        return ""

    def _page_hints(self, page: Optional[Dict[str, Any]]) -> tuple[str, str]:
        if isinstance(page, dict):
            return str(page.get("text") or ""), str(page.get("html") or "")
        return str(page or ""), ""

    @classmethod
    def _filter_noise_lines(cls, text: str) -> str:
        if not text:
            return ""
        lines = []
        for raw in re.split(r"[\r\n]+", text):
            line = re.sub(r"\s+", " ", raw.strip())
            if not line:
                continue
            # 住所/電話っぽい行は優先して残す
            if ZIP_RE.search(line) or PREFECTURE_NAME_RE.search(line) or PHONE_RE.search(line):
                lines.append(line)
                continue
            # cookie/プライバシー/同意バナー等を除外
            if COOKIE_PRIVACY_LINE_RE.search(line):
                continue
            # ナビゲーション単語だけの短行を除外
            if len(line) <= 16 and NAV_LIKE_LINE_RE.match(line):
                continue
            if any(k in line.lower() for k in ("breadcrumb", "パンくず", "メニュー", "menu", "nav")) and len(line) <= 40:
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    @classmethod
    def _clean_text_from_html(cls, html: str, fallback_text: str = "") -> str:
        if not html:
            return cls._filter_noise_lines(fallback_text or "")
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            # BeautifulSoup が落ちる/壊れたHTMLのとき、raw HTML をそのまま流すと
            # <div...> 等の断片が住所に混入しやすいので、雑にタグを落としてから通す。
            raw = html or fallback_text or ""
            raw = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
            raw = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", raw)
            raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
            raw = re.sub(r"<[^>]+>", "\n", raw)
            raw = raw.replace("&nbsp;", " ").replace("&#160;", " ")
            raw = re.sub(r"\n{3,}", "\n\n", raw)
            return cls._filter_noise_lines(raw)

        for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "canvas", "iframe", "form", "button"]):
            tag.decompose()

        for node in soup.find_all(True):
            attrs = " ".join(
                str(node.get(k) or "")
                for k in ("id", "class", "role", "aria-label")
            )
            if attrs and COOKIE_NODE_HINT_RE.search(attrs):
                node.decompose()

        base = soup.body or soup
        text = base.get_text(separator="\n", strip=True)
        text = unicodedata.normalize("NFKC", text)
        text = cls._filter_noise_lines(text)
        if len(text) < 120:
            attr_keys = (
                "aria-label",
                "alt",
                "title",
                "data-label",
                "data-title",
                "data-name",
                "data-value",
                "data-content",
            )
            label_keywords: set[str] = set()
            for kws in TABLE_LABEL_MAP.values():
                label_keywords.update(kws)
            attr_texts: set[str] = set()
            for node in base.find_all(True):
                for key in attr_keys:
                    val = node.get(key)
                    if not isinstance(val, str):
                        continue
                    cleaned = unicodedata.normalize("NFKC", val).strip()
                    if not cleaned or len(cleaned) > 80:
                        continue
                    if (
                        PHONE_RE.search(cleaned)
                        or ZIP_RE.search(cleaned)
                        or FISCAL_RE.search(cleaned)
                        or any(kw in cleaned for kw in label_keywords)
                    ):
                        attr_texts.add(cleaned)
            if attr_texts:
                text = f"{text}\n" + "\n".join(sorted(attr_texts))
        return text or cls._filter_noise_lines(fallback_text or "")

    @staticmethod
    def _meta_strings(html: str) -> str:
        if not html:
            return ""
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return ""
        hints: List[str] = []
        title = soup.title.string if soup.title and soup.title.string else ""
        if title:
            hints.append(title)
        for attr in ("description", "keywords", "og:site_name", "og:title"):
            node = soup.find("meta", attrs={"name": attr}) or soup.find("meta", attrs={"property": attr})
            if node:
                content = node.get("content")
                if content:
                    hints.append(content)
        return " \n".join(hints)

    @staticmethod
    def _safe_json_loads(candidate: str) -> Optional[Any]:
        if not candidate:
            return None
        try:
            return json.loads(candidate)
        except Exception:
            return None

    @classmethod
    def _extract_jsonld_objects(cls, html: str) -> List[Dict[str, Any]]:
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for node in soup.find_all("script", attrs={"type": lambda v: v and "ld+json" in str(v).lower()}):
            raw = node.string or node.get_text(" ", strip=True) or ""
            raw = raw.strip()
            if not raw:
                continue
            data = cls._safe_json_loads(raw)
            if not data:
                continue
            candidates: List[Any] = []
            if isinstance(data, dict):
                if isinstance(data.get("@graph"), list):
                    candidates.extend(data.get("@graph") or [])
                else:
                    candidates.append(data)
            elif isinstance(data, list):
                candidates.extend(data)
            for obj in candidates:
                if isinstance(obj, dict):
                    out.append(obj)
        return out

    @classmethod
    def _detect_directory_like(
        cls,
        url: str,
        *,
        text: str = "",
        html: str = "",
    ) -> Dict[str, Any]:
        """
        企業DB/ディレクトリ系ページを強制除外するための判定。
        ドメイン名ではなく URLパス構造＋本文シグナルでスコアリングする。
        """
        score = 0
        reasons: List[str] = []
        try:
            parsed = urllib.parse.urlparse(url or "")
        except Exception:
            parsed = None

        path = ""
        query = ""
        if parsed:
            path = unquote(parsed.path or "")
            query = parsed.query or ""
        path_lower = (path or "").lower()

        for pat in cls.DIRECTORY_URL_PATTERNS_STRONG:
            if pat.search(path_lower):
                score += 10
                reasons.append(f"url_strong:{pat.pattern}")
                break

        for pat in cls.DIRECTORY_URL_PATTERNS:
            if pat.search(path_lower):
                score += 8
                reasons.append(f"url:{pat.pattern}")
                break

        if query:
            qs = parse_qs(query)
            for key in cls.DIRECTORY_QUERY_CORP_KEYS:
                vals = qs.get(key)
                if not vals:
                    continue
                v0 = (vals[0] or "").strip()
                if cls.CORPORATE_NUMBER_RE.fullmatch(v0):
                    score += 10
                    reasons.append(f"query:{key}={v0}")
                    break
            for key in cls.DIRECTORY_QUERY_ID_KEYS:
                vals = qs.get(key)
                if not vals:
                    continue
                v0 = (vals[0] or "").strip()
                if re.fullmatch(r"\d{2,}", v0):
                    score += 4
                    reasons.append(f"query:{key}={v0}")
                    break

        corp_in_path = cls.CORPORATE_NUMBER_RE.search(path_lower or "")
        if corp_in_path:
            score += 10
            reasons.append(f"path:corp={corp_in_path.group(0)}")

        sample_text = (text or "")
        if not sample_text and html:
            try:
                soup = BeautifulSoup(html, "html.parser")
                sample_text = soup.get_text(" ", strip=True)
            except Exception:
                sample_text = ""
        t = unicodedata.normalize("NFKC", sample_text)[:7000]
        t_low = t.lower()
        for kw in cls.DIRECTORY_TEXT_KEYWORDS_STRONG:
            if kw.lower() in t_low:
                score += 2
                reasons.append(f"text:{kw}")
        for pat in cls.DIRECTORY_TEXT_PATTERNS:
            if pat.search(t):
                score += 3
                reasons.append(f"text_re:{pat.pattern}")

        # 多数の企業リンクがある（/companies/ 等）場合はディレクトリUIとみなす
        if html:
            try:
                hits = len(re.findall(r"href=[\"'][^\"']*(?:/companies/|/company/|/detail/)\d+", html, flags=re.I))
            except Exception:
                hits = 0
            if hits >= 4:
                score += 6
                reasons.append(f"html:many_company_links={hits}")

        return {
            "is_directory_like": score >= 10,
            "directory_score": score,
            "directory_reasons": reasons[:12],
        }

    @classmethod
    def _compute_official_evidence(
        cls,
        company_name: str,
        *,
        url: str,
        html: str,
    ) -> Dict[str, Any]:
        """
        ドメイン一致より「公式らしさの根拠」を重視するための加点。
        1回fetchしたHTMLのみを使い、追加のネットワークアクセスはしない。
        """
        evidence: List[str] = []
        score = 0
        norm_name = cls._normalize_company_name(company_name or "")
        full_name = unicodedata.normalize("NFKC", company_name or "").strip()
        name_variants = [v for v in (full_name, norm_name) if v]
        if not html:
            return {"official_evidence_score": 0, "official_evidence": []}
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return {"official_evidence_score": 0, "official_evidence": []}

        def _name_hit(s: str) -> float:
            s = unicodedata.normalize("NFKC", s or "").strip()
            if not s or not name_variants:
                return 0.0
            best = 0.0
            for nv in name_variants:
                if nv in s:
                    return 1.0
                best = max(best, SequenceMatcher(None, nv, s).ratio())
            return best

        title = soup.title.string if soup.title and soup.title.string else ""
        title_ratio = _name_hit(title)
        if title_ratio >= 0.92:
            score += 3
            evidence.append("title")
        elif title_ratio >= 0.85:
            score += 2
            evidence.append("title_partial")

        h1 = ""
        try:
            h1_node = soup.find("h1")
            h1 = h1_node.get_text(" ", strip=True) if h1_node else ""
        except Exception:
            h1 = ""
        h1_ratio = _name_hit(h1)
        if h1_ratio >= 0.92:
            score += 3
            evidence.append("h1")
        elif h1_ratio >= 0.85:
            score += 2
            evidence.append("h1_partial")

        og_site = ""
        try:
            og = soup.find("meta", attrs={"property": "og:site_name"}) or soup.find("meta", attrs={"name": "og:site_name"})
            og_site = (og.get("content") or "").strip() if og else ""
        except Exception:
            og_site = ""
        og_ratio = _name_hit(og_site)
        if og_ratio >= 0.92:
            score += 3
            evidence.append("og:site_name")

        # JSON-LD Organization
        jsonld_objs = cls._extract_jsonld_objects(html)
        org_matched = False
        org_has_addr = False
        org_has_tel = False
        for obj in jsonld_objs:
            raw_type = obj.get("@type") or obj.get("['@type']")  # defensive
            types: List[str] = []
            if isinstance(raw_type, str):
                types = [raw_type]
            elif isinstance(raw_type, list):
                types = [str(t) for t in raw_type if t]
            types_low = {t.lower() for t in types}
            if not ({"organization", "corporation", "localbusiness"} & types_low):
                continue
            name_val = obj.get("name")
            ratio = _name_hit(str(name_val) if name_val is not None else "")
            if ratio >= 0.9:
                org_matched = True
                score += 4
                evidence.append("jsonld:org_name")
                addr_val = obj.get("address")
                tel_val = obj.get("telephone") or obj.get("tel")
                if addr_val:
                    org_has_addr = True
                    score += 2
                    evidence.append("jsonld:address")
                if tel_val:
                    org_has_tel = True
                    score += 1
                    evidence.append("jsonld:telephone")
                break

        # 同一ドメイン内の「会社概要/お問い合わせ/アクセス」リンク
        try:
            links = soup.find_all("a", href=True)
        except Exception:
            links = []
        if links:
            got_profile = False
            got_contact = False
            got_access = False
            for a in links:
                href = (a.get("href") or "").strip()
                text = (a.get_text(" ", strip=True) or "").strip()
                blob = f"{href} {text}".lower()
                if not got_profile and any(k in blob for k in ("会社概要", "企業情報", "会社情報", "about", "corporate", "profile", "overview")):
                    got_profile = True
                if not got_contact and any(k in blob for k in ("お問い合わせ", "お問合せ", "問合せ", "contact", "inquiry")):
                    got_contact = True
                if not got_access and any(k in blob for k in ("アクセス", "所在地", "map", "access")):
                    got_access = True
                if got_profile and got_contact and got_access:
                    break
            if got_profile:
                score += 2
                evidence.append("link:profile")
            if got_contact:
                score += 2
                evidence.append("link:contact")
            if got_access:
                score += 1
                evidence.append("link:access")

        # フッター由来の社名/©
        footer_text = ""
        try:
            footer = soup.find("footer")
            footer_text = footer.get_text(" ", strip=True) if footer else ""
        except Exception:
            footer_text = ""
        footer_text_norm = unicodedata.normalize("NFKC", footer_text or "")
        if any(nv in footer_text_norm for nv in name_variants):
            score += 2
            evidence.append("footer:name")
        if re.search(r"(©|copyright|all rights reserved)", footer_text_norm, flags=re.I):
            score += 1
            evidence.append("footer:copyright")

        # URLがroot/浅いパスほど公式トップ寄り（強い根拠ではないので軽微に加点）
        try:
            depth = max((urllib.parse.urlparse(url).path or "/").strip("/").count("/") + 1, 0)
        except Exception:
            depth = 0
        if depth <= 1:
            score += 1
            evidence.append("url:shallow")

        return {"official_evidence_score": int(score), "official_evidence": evidence[:12]}

    def is_likely_official_site(
        self,
        company_name: str,
        url: str,
        page_info: Optional[Dict[str, Any]] = None,
        expected_address: Optional[str] = None,
        extracted: Optional[Dict[str, List[str]]] = None,
        *,
        return_details: bool = False,
    ) -> bool | Dict[str, Any]:
        def finalize(
            is_official: bool,
            *,
            score: float = 0.0,
            name_present: bool = False,
            strong_domain: bool = False,
            address_match: bool = False,
            prefecture_match: bool = False,
            postal_code_match: bool = False,
            name_match_ratio: float = 0.0,
            name_match_exact: bool = False,
            name_match_partial_only: bool = False,
            name_match_source: str = "",
            domain_score: int = 0,
            host_value: str = "",
            blocked_host: bool = False,
            prefecture_mismatch: bool = False,
            official_evidence_score: int = 0,
            official_evidence: Optional[List[str]] = None,
            directory_like: bool = False,
            directory_score: int = 0,
            directory_reasons: Optional[List[str]] = None,
        ) -> bool | Dict[str, Any]:
            payload = {
                "is_official": is_official,
                "score": score,
                "name_present": name_present,
                "strong_domain": strong_domain,
                "address_match": address_match,
                "prefecture_match": prefecture_match,
                "postal_code_match": postal_code_match,
                "name_match_ratio": name_match_ratio,
                "name_match_exact": name_match_exact,
                "name_match_partial_only": name_match_partial_only,
                "name_match_source": name_match_source,
                "domain_score": domain_score,
                "host": host_value,
                "blocked_host": blocked_host,
                "prefecture_mismatch": prefecture_mismatch,
                "official_evidence_score": official_evidence_score,
                "official_evidence": official_evidence or [],
                "directory_like": directory_like,
                "directory_score": directory_score,
                "directory_reasons": directory_reasons or [],
            }
            return payload if return_details else payload["is_official"]

        try:
            parsed = urllib.parse.urlparse(url)
            parsed_path_lower = (parsed.path or "").lower()
        except Exception:
            return finalize(False)

        host, path_lower, allowed_tld, whitelist_hit, is_google_sites = self._allowed_official_host(url)
        if not host:
            return finalize(False)
        if any(path_lower.endswith(ext) for ext in _BINARY_EXTS):
            return finalize(False, host_value=host, blocked_host=True)
        if host.endswith(".lg.jp"):
            return finalize(False, host_value=host, blocked_host=True)
        company_tokens = self._company_tokens(company_name)
        domain_match_score = self._domain_score(company_tokens, url)
        host_token_hit = self._host_token_hit(company_tokens, url) if company_tokens else False
        if not (allowed_tld or whitelist_hit or is_google_sites):
            if domain_match_score < 6 and not host_token_hit:
                return finalize(False, host_value=host, blocked_host=True)
        parsed = urllib.parse.urlparse(url)
        base_name = (company_name or "").strip()
        is_prefecture_exact = base_name in PREFECTURE_NAMES
        expected_pref = self._extract_prefecture(expected_address or "")
        expected_zip = self._extract_postal_code(expected_address or "")
        expected_city_match = CITY_RE.search(expected_address or "")
        expected_city = expected_city_match.group(1) if expected_city_match else ""
        input_addr_weak = bool(expected_pref and not expected_city and not expected_zip)
        if is_prefecture_exact:
            allowed_suffixes = self.ENTITY_SITE_SUFFIXES.get("gov", ())
            if not any(self._host_matches_suffix(host, suffix) for suffix in allowed_suffixes):
                return finalize(False, host_value=host, blocked_host=True)
        if not is_google_sites and any(host == domain or host.endswith(f".{domain}") for domain in self.HARD_EXCLUDE_HOSTS):
            return finalize(False, host_value=host, blocked_host=True)

        score = 0
        allowed_host_whitelist = self.ALLOWED_HOST_WHITELIST
        if any(host == domain or host.endswith(f".{domain}") for domain in self.SUSPECT_HOSTS):
            score -= 4
        if host in allowed_host_whitelist:
            score += 4  # 許容ホストは減点を打ち消す
        if host.startswith("www."):
            score += 2
        jp_corp_tlds = (".co.jp", ".or.jp", ".ac.jp", ".ed.jp", ".lg.jp", ".gr.jp", ".go.jp")
        if host.endswith(jp_corp_tlds):
            score += 4
        elif host.endswith(".jp"):
            score += 2
        elif any(host.endswith(tld) for tld in self.GENERIC_TLDS):
            score += 1
        generic_tld = any(host.endswith(tld) for tld in self.GENERIC_TLDS) and not host.endswith(jp_corp_tlds)

        company_tokens = self._company_tokens(company_name)
        host_compact = host.replace("-", "").replace(".", "")
        domain_match_score = self._domain_score(company_tokens, url)
        if domain_match_score >= 5:
            name_present_flag = True
        if domain_match_score >= 6:
            score += 3
        elif domain_match_score >= 4:
            score += 2
        elif domain_match_score >= 2:
            score += 1

        domain_tokens = self._domain_tokens(url)
        for token in company_tokens:
            if any(token in dt for dt in domain_tokens):
                score += 3
            if token and token in host:
                score += 2
            if token and len(token) >= 4 and token in parsed_path_lower:
                score += 2

        path_lower = parsed_path_lower
        if host.endswith("google.com") and "sites" in path_lower:
            if any(token in path_lower for token in company_tokens):
                score += 5
            if host == "sites.google.com":
                score += 2

        norm_name = self._normalize_company_name(company_name)
        text_snippet, html = self._page_hints(page_info)
        directory = self._detect_directory_like(url, text=text_snippet or "", html=html or "")
        directory_like = bool(directory.get("is_directory_like"))
        directory_score = int(directory.get("directory_score") or 0)
        directory_reasons = directory.get("directory_reasons") or []
        evidence = self._compute_official_evidence(company_name, url=url, html=html or "")
        official_evidence_score = int(evidence.get("official_evidence_score") or 0)
        official_evidence = evidence.get("official_evidence") or []
        meta_snippet = self._meta_strings(html)
        combined = f"{text_snippet}\n{meta_snippet}".strip()
        lowered = combined.lower()
        signals = extract_name_signals(html or "", text_snippet or "")
        name_match = score_name_match(company_name or "", signals)
        entity_tags = self._detect_entity_tags(company_name)
        def _entity_suffix_hit(tag: str) -> bool:
            allowed = self.ENTITY_SITE_SUFFIXES.get(tag, ())
            return any(self._host_matches_suffix(host, suffix) for suffix in allowed)
        if "gov" in entity_tags and not (_entity_suffix_hit("gov") or is_google_sites):
            return finalize(False, host_value=host, blocked_host=True)
        if "edu" in entity_tags and not (_entity_suffix_hit("edu") or is_google_sites):
            return finalize(False, host_value=host, blocked_host=True)
        company_has_corp = any(suffix in (company_name or "") for suffix in self.CORP_SUFFIXES) or bool(entity_tags)
        host_token_hit = self._host_token_hit(company_tokens, url)
        if not company_tokens:
            host_token_hit = False  # Avoid treating unknown tokens as strong evidence
        loose_host_hit = host_token_hit or (company_has_corp and allowed_tld and domain_match_score >= 3)
        name_present_flag = (
            bool(norm_name and norm_name in combined)
            or (name_match.ratio >= 0.85 and not name_match.partial_only)
            or any(tok in host for tok in company_tokens)
            or any(tok in host_compact for tok in company_tokens)
            or loose_host_hit
        )
        strong_domain_flag = company_has_corp and loose_host_hit and (domain_match_score >= 3 or whitelist_hit or is_google_sites)
        # generic_tld is computed above; generic TLDs need name/address corroboration

        if norm_name and norm_name in combined:
            score += 4
        elif norm_name and len(norm_name) >= 4 and any(part in combined for part in (norm_name[:4], norm_name[-4:])):
            score += 2
        if "公式" in combined or "official" in lowered:
            score += 2
        if any(kw in lowered for kw in self.NON_OFFICIAL_SNIPPET_KEYWORDS):
            score -= 2
        if any(kw in host for kw in self.NON_OFFICIAL_KEYWORDS):
            score -= 3

        # タイトル/h1/og:site_name/body冒頭の一致度（部分一致だけは除外寄りにする）
        if name_match.exact:
            score += 5
        elif name_match.ratio >= 0.92 and not name_match.partial_only:
            score += 3
        elif name_match.partial_only:
            score -= 4

        # 企業DB/ディレクトリ系は強制除外（社名/住所一致しても公式扱いしない）
        if directory_like:
            return finalize(
                False,
                score=min(score, -10),
                name_present=name_present_flag,
                strong_domain=False,
                address_match=False,
                prefecture_match=False,
                postal_code_match=False,
                name_match_ratio=name_match.ratio,
                name_match_exact=name_match.exact,
                name_match_partial_only=name_match.partial_only,
                name_match_source=name_match.best_source,
                domain_score=domain_match_score,
                host_value=host,
                blocked_host=False,
                prefecture_mismatch=False,
                official_evidence_score=official_evidence_score,
                official_evidence=official_evidence,
                directory_like=True,
                directory_score=directory_score,
                directory_reasons=directory_reasons,
            )

        # 入力住所がある場合、ページ側の都道府県が明確に不一致なら低スコア
        pref_mismatch = False
        if expected_pref and not input_addr_weak:
            expected_city_match = CITY_RE.search(expected_address or "")
            expected_city = expected_city_match.group(1) if expected_city_match else ""
            found_prefs = set(PREFECTURE_NAME_RE.findall(combined or ""))
            # 店舗一覧のような大量列挙は除外（=ペナルティ対象から外す）
            if 0 < len(found_prefs) <= 3 and expected_pref not in found_prefs:
                pref_mismatch = True
                score -= 3

        content_name_ok = bool(norm_name and norm_name in combined) or (name_match.ratio >= 0.85 and not name_match.partial_only)
        company_name_has_kanji = bool(re.search(r"[一-龥]", norm_name or ""))
        domain_only_ok = bool(company_name_has_kanji and host_token_hit and domain_match_score >= 5 and not pref_mismatch and not name_match.partial_only)

        # ドメインに社名トークンが強く含まれる場合は即公式とみなす
        if host_token_hit and domain_match_score >= 4:
            # 部分一致しか根拠が無い候補（例: 株式会社ホンマ -> 本間ゴルフ）を除外する
            strong_name_ok = (
                bool(norm_name and norm_name in combined)
                or name_match.exact
                or (name_match.ratio >= 0.92 and not name_match.partial_only)
            )
            if strong_name_ok and not pref_mismatch:
                return finalize(
                    True,
                    score=max(score, 4),
                    name_present=name_present_flag,
                    strong_domain=True,
                    address_match=False,
                    prefecture_match=False,
                    postal_code_match=False,
                    name_match_ratio=name_match.ratio,
                    name_match_exact=name_match.exact,
                    name_match_partial_only=name_match.partial_only,
                    name_match_source=name_match.best_source,
                    domain_score=domain_match_score,
                    host_value=host,
                    blocked_host=False,
                    prefecture_mismatch=pref_mismatch,
                    official_evidence_score=official_evidence_score,
                    official_evidence=official_evidence,
                    directory_like=directory_like,
                    directory_score=directory_score,
                    directory_reasons=directory_reasons,
                )

        address_hit = False
        pref_hit = False
        postal_hit = False
        pref_mismatch_in_address = False
        if expected_address:
            candidate_addrs: List[str] = []
            if extracted and extracted.get("addresses"):
                candidate_addrs.extend(extracted.get("addresses") or [])
            if not candidate_addrs and text_snippet:
                candidate_addrs.extend(ADDR_FALLBACK_RE.findall(text_snippet))
            for cand in candidate_addrs:
                cand_pref = self._extract_prefecture(cand)
                cand_zip = self._extract_postal_code(cand)
                pref_ok = bool(expected_pref and cand_pref and expected_pref == cand_pref)
                zip_ok = bool(expected_zip and cand_zip and expected_zip == cand_zip)
                addr_ok = self._address_matches(expected_address, cand)
                if expected_pref and cand_pref and not pref_ok and not input_addr_weak:
                    pref_mismatch_in_address = True
                    score -= 6
                if expected_city and cand and expected_city not in cand:
                    score -= 1
                if addr_ok:
                    score += 3
                    if pref_ok:
                        score += 2
                    if zip_ok:
                        score += 3
                    address_hit = True
                    pref_hit = pref_ok
                    postal_hit = zip_ok
                    break
                # 都道府県レベルしか合致しない場合も公式寄りに扱う
                if pref_ok and not address_hit:
                    score += 1
                    pref_hit = True

        address_strong = address_hit or postal_hit
        pref_hit_strong = (
            pref_hit
            and not address_strong
            and (
                name_match.exact
                or (name_match.ratio >= 0.92 and not name_match.partial_only)
                or official_evidence_score >= 9
            )
            and (strong_domain_flag or host_token_hit or domain_match_score >= 3 or whitelist_hit or is_google_sites)
        )

        # 住所シグナルがあり、法人TLD (.co.jp 等) ならドメイントークンが弱くても公式として許容する
        if allowed_tld and (address_strong or pref_hit_strong):
            if domain_match_score >= 2 or host_token_hit:
                return finalize(
                    True,
                    score=max(score, 4),
                    name_present=name_present_flag,
                    strong_domain=bool(domain_match_score >= 3 or host_token_hit),
                    address_match=address_hit,
                    prefecture_match=pref_hit,
                    postal_code_match=postal_hit,
                    name_match_ratio=name_match.ratio,
                    name_match_exact=name_match.exact,
                    name_match_partial_only=name_match.partial_only,
                    name_match_source=name_match.best_source,
                    domain_score=domain_match_score,
                    host_value=host,
                    blocked_host=False,
                    prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                    official_evidence_score=official_evidence_score,
                    official_evidence=official_evidence,
                    directory_like=directory_like,
                    directory_score=directory_score,
                    directory_reasons=directory_reasons,
                )
        if generic_tld and not whitelist_hit and not is_google_sites:
            name_ok = name_present_flag or domain_match_score >= 4 or host_token_hit
            strong_generic_ok = strong_domain_flag or (host_token_hit and domain_match_score >= 4)
            if not name_ok:
                return finalize(
                    False,
                    score=score,
                    name_present=name_present_flag,
                    strong_domain=strong_domain_flag,
                    address_match=address_hit,
                    prefecture_match=pref_hit,
                    postal_code_match=postal_hit,
                    name_match_ratio=name_match.ratio,
                    name_match_exact=name_match.exact,
                    name_match_partial_only=name_match.partial_only,
                    name_match_source=name_match.best_source,
                    domain_score=domain_match_score,
                    host_value=host,
                    blocked_host=False,
                    prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                    official_evidence_score=official_evidence_score,
                    official_evidence=official_evidence,
                    directory_like=directory_like,
                    directory_score=directory_score,
                    directory_reasons=directory_reasons,
                )
            if expected_address and not (address_hit or pref_hit or postal_hit) and not strong_generic_ok:
                strong_name_for_evidence = name_match.exact or (name_match.ratio >= 0.92 and not name_match.partial_only)
                if not (strong_name_for_evidence and official_evidence_score >= 9):
                    return finalize(
                        False,
                        score=score,
                        name_present=name_present_flag,
                        strong_domain=strong_domain_flag,
                        address_match=address_hit,
                        prefecture_match=pref_hit,
                        postal_code_match=postal_hit,
                        name_match_ratio=name_match.ratio,
                        name_match_exact=name_match.exact,
                        name_match_partial_only=name_match.partial_only,
                        name_match_source=name_match.best_source,
                        domain_score=domain_match_score,
                        host_value=host,
                        blocked_host=False,
                        prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                        official_evidence_score=official_evidence_score,
                        official_evidence=official_evidence,
                        directory_like=directory_like,
                        directory_score=directory_score,
                        directory_reasons=directory_reasons,
                    )

        if allowed_tld and host_token_hit and company_has_corp and score < 4:
            score = 4

        if expected_address and not (address_hit or pref_hit or postal_hit):
            strong_name_for_evidence = name_match.exact or (name_match.ratio >= 0.92 and not name_match.partial_only)
            if strong_name_for_evidence and official_evidence_score >= 9:
                # 住所が本文から取れないケースでも、タイトル/JSON-LD/フッター等の根拠が強ければ公式として残す
                pass
            else:
                return finalize(
                    False,
                    score=score,
                    name_present=name_present_flag,
                    strong_domain=strong_domain_flag,
                    address_match=address_hit,
                    prefecture_match=pref_hit,
                    postal_code_match=postal_hit,
                    name_match_ratio=name_match.ratio,
                    name_match_exact=name_match.exact,
                    name_match_partial_only=name_match.partial_only,
                    name_match_source=name_match.best_source,
                    domain_score=domain_match_score,
                    host_value=host,
                    prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                    official_evidence_score=official_evidence_score,
                    official_evidence=official_evidence,
                    directory_like=directory_like,
                    directory_score=directory_score,
                    directory_reasons=directory_reasons,
                )

        if (address_strong or pref_hit_strong) and (allowed_tld or whitelist_hit or is_google_sites):
            return finalize(
                True,
                score=score,
                name_present=name_present_flag,
                strong_domain=strong_domain_flag,
                address_match=address_hit,
                prefecture_match=pref_hit,
                postal_code_match=postal_hit,
                name_match_ratio=name_match.ratio,
                name_match_exact=name_match.exact,
                name_match_partial_only=name_match.partial_only,
                name_match_source=name_match.best_source,
                domain_score=domain_match_score,
                host_value=host,
                prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                official_evidence_score=official_evidence_score,
                official_evidence=official_evidence,
                directory_like=directory_like,
                directory_score=directory_score,
                directory_reasons=directory_reasons,
            )

        if allowed_tld and company_has_corp and (host_token_hit or domain_match_score >= 3 or name_present_flag):
            if not (content_name_ok or domain_only_ok) and not (address_hit or pref_hit or postal_hit) and not whitelist_hit and not is_google_sites:
                return finalize(
                    False,
                    score=score,
                    name_present=name_present_flag,
                    strong_domain=strong_domain_flag,
                    address_match=address_hit,
                    prefecture_match=pref_hit,
                    postal_code_match=postal_hit,
                    name_match_ratio=name_match.ratio,
                    name_match_exact=name_match.exact,
                    name_match_partial_only=name_match.partial_only,
                    name_match_source=name_match.best_source,
                    domain_score=domain_match_score,
                    host_value=host,
                    prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                    official_evidence_score=official_evidence_score,
                    official_evidence=official_evidence,
                    directory_like=directory_like,
                    directory_score=directory_score,
                    directory_reasons=directory_reasons,
                )
            return finalize(
                True,
                score=score,
                name_present=name_present_flag,
                strong_domain=strong_domain_flag,
                address_match=address_hit,
                prefecture_match=pref_hit,
                postal_code_match=postal_hit,
                name_match_ratio=name_match.ratio,
                name_match_exact=name_match.exact,
                name_match_partial_only=name_match.partial_only,
                name_match_source=name_match.best_source,
                domain_score=domain_match_score,
                host_value=host,
                prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                official_evidence_score=official_evidence_score,
                official_evidence=official_evidence,
                directory_like=directory_like,
                directory_score=directory_score,
                directory_reasons=directory_reasons,
            )

        if whitelist_hit and expected_address:
            if not (address_hit or pref_hit or postal_hit):
                address_hit = True
            return finalize(
                True,
                score=score,
                name_present=name_present_flag,
                strong_domain=strong_domain_flag,
                address_match=address_hit,
                prefecture_match=pref_hit,
                postal_code_match=postal_hit,
                name_match_ratio=name_match.ratio,
                name_match_exact=name_match.exact,
                name_match_partial_only=name_match.partial_only,
                name_match_source=name_match.best_source,
                domain_score=domain_match_score,
                host_value=host,
                prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                official_evidence_score=official_evidence_score,
                official_evidence=official_evidence,
                directory_like=directory_like,
                directory_score=directory_score,
                directory_reasons=directory_reasons,
            )

        # ドメインが弱いものは住所等の裏付けが無ければ公式扱いしない
        if not (address_hit or pref_hit or postal_hit) and domain_match_score < 2:
            if official_evidence_score >= 10 and (name_match.exact or name_match.ratio >= 0.92) and allowed_tld:
                # ドメインスコアが極端に低くても、JSON-LD/タイトル等の根拠が強い場合は残す
                pass
            else:
                return finalize(
                    False,
                    score=score,
                    name_present=name_present_flag,
                    strong_domain=False,
                    address_match=address_hit,
                    prefecture_match=pref_hit,
                    postal_code_match=postal_hit,
                    name_match_ratio=name_match.ratio,
                    name_match_exact=name_match.exact,
                    name_match_partial_only=name_match.partial_only,
                    name_match_source=name_match.best_source,
                    domain_score=domain_match_score,
                    host_value=host,
                    prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                    official_evidence_score=official_evidence_score,
                    official_evidence=official_evidence,
                    directory_like=directory_like,
                    directory_score=directory_score,
                    directory_reasons=directory_reasons,
                )

        name_present = name_present_flag
        strong_domain = strong_domain_flag
        if not (name_present or strong_domain):
            return finalize(
                False,
                score=score,
                name_present=name_present,
                strong_domain=strong_domain,
                address_match=address_hit,
                prefecture_match=pref_hit,
                postal_code_match=postal_hit,
                name_match_ratio=name_match.ratio,
                name_match_exact=name_match.exact,
                name_match_partial_only=name_match.partial_only,
                name_match_source=name_match.best_source,
                domain_score=domain_match_score,
                host_value=host,
                prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                official_evidence_score=official_evidence_score,
                official_evidence=official_evidence,
                directory_like=directory_like,
                directory_score=directory_score,
                directory_reasons=directory_reasons,
            )
        # ドメイン一致が弱く、名前も住所も見つからない場合は非公式扱い
        if not (name_present or address_hit or pref_hit or postal_hit) and domain_match_score < 4:
            return finalize(
                False,
                score=score,
                name_present=name_present,
                strong_domain=strong_domain,
                address_match=address_hit,
                prefecture_match=pref_hit,
                postal_code_match=postal_hit,
                name_match_ratio=name_match.ratio,
                name_match_exact=name_match.exact,
                name_match_partial_only=name_match.partial_only,
                name_match_source=name_match.best_source,
                domain_score=domain_match_score,
                host_value=host,
                prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
                official_evidence_score=official_evidence_score,
                official_evidence=official_evidence,
                directory_like=directory_like,
                directory_score=directory_score,
                directory_reasons=directory_reasons,
            )
        result = score >= 4
        return finalize(
            result,
            score=score,
            name_present=name_present,
            strong_domain=strong_domain,
            address_match=address_hit,
            prefecture_match=pref_hit,
            postal_code_match=postal_hit,
            name_match_ratio=name_match.ratio,
            name_match_exact=name_match.exact,
            name_match_partial_only=name_match.partial_only,
            name_match_source=name_match.best_source,
            domain_score=domain_match_score,
            host_value=host,
            prefecture_mismatch=(pref_mismatch or pref_mismatch_in_address),
            official_evidence_score=official_evidence_score,
            official_evidence=official_evidence,
            directory_like=directory_like,
            directory_score=directory_score,
            directory_reasons=directory_reasons,
        )

    @staticmethod
    def normalize_homepage_url(url: str, page_info: Optional[Dict[str, Any]] = None) -> str:
        """
        公式と判断したURLをホームページ向けに正規化する。
        - rel=canonical / og:url があれば優先
        - 問い合わせ／会員ページ等であればドメインルートに寄せる
        - クエリ／フラグメント／index.* を除去
        """
        if not url:
            return url
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return url
        if not parsed.scheme or not parsed.netloc:
            return url
        original_host = parsed.netloc.split(":")[0]
        host_compare = original_host.lower()
        if host_compare.startswith("www."):
            host_compare = host_compare[4:]
        base_root = f"{parsed.scheme}://{original_host}/"
        normalized = parsed._replace(netloc=original_host, query="", fragment="")

        html = ""
        if isinstance(page_info, dict):
            html = page_info.get("html") or ""
        if html:
            try:
                soup = BeautifulSoup(html, "html.parser")
            except Exception:
                soup = None
            if soup:
                canonical = soup.find("link", rel=lambda v: v and "canonical" in v.lower())
                if canonical and canonical.get("href"):
                    href = canonical.get("href")
                else:
                    og_url = soup.find("meta", attrs={"property": "og:url"}) or soup.find("meta", attrs={"name": "og:url"})
                    href = og_url.get("content") if og_url else None
                if href:
                    try:
                        resolved = urllib.parse.urljoin(url, href)
                        resolved_parsed = urllib.parse.urlparse(resolved)
                        resolved_host = resolved_parsed.netloc.split(":")[0]
                        resolved_host_cmp = resolved_host.lower()
                        if resolved_host_cmp.startswith("www."):
                            resolved_host_cmp = resolved_host_cmp[4:]
                        if resolved_parsed.netloc and resolved_host_cmp == host_compare:
                            normalized_path = normalized.path or "/"
                            resolved_path = resolved_parsed.path or "/"
                            if not (normalized_path != "/" and resolved_path == "/"):
                                normalized = resolved_parsed._replace(netloc=original_host, query="", fragment="")
                    except Exception:
                        pass

        # index.* → root
        if normalized.path.lower().endswith(("/index.html", "/index.htm", "/index.php", "/index.asp")):
            normalized = normalized._replace(path="/")

        # 明示的にクエリ/フラグメントを除去
        normalized = normalized._replace(query="", fragment="")

        segments = [seg.lower() for seg in normalized.path.strip("/").split("/") if seg]
        suspect_segments = {
            "contact", "inquiry", "toiawase", "otoiawase", "mailform", "mail", "form",
            "entry", "apply", "application", "register", "registration", "signup",
            "login", "member", "members", "mypage", "reserve", "reservation", "yoyaku",
            "cart", "shop_cart", "order", "questionnaire"
        }
        standalone_segments = {"top", "home"}

        if not segments:
            final = normalized._replace(path="/")
        elif segments[0] in suspect_segments or any(seg in suspect_segments for seg in segments):
            final = normalized._replace(path="/")
        elif len(segments) == 1 and segments[0] in standalone_segments:
            final = normalized._replace(path="/")
        else:
            final = normalized

        final_path = final.path or "/"
        if not final_path.endswith("/"):
            if final_path == "/":
                final = final._replace(path="/")
            else:
                # remove trailing slash for non-root
                final = final._replace(path=final_path.rstrip("/"))

        return urllib.parse.urlunparse(final) or base_root

    # ===== 高速化の肝：ブラウザを起動して使い回す =====
    async def start(self):
        if self.browser:
            return
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",  # /dev/shm不足でのクラッシュ回避
            ],
        )
        self.context = await self.browser.new_context(
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        # 軽量化：画像/フォント/メディア/スタイルをブロック
        await self.context.route("**/*", self._handle_route)

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            try:
                if self.browser:
                    await self.browser.close()
            finally:
                if self._pw:
                    await self._pw.stop()
        if self.http_session:
            try:
                self.http_session.close()
            except Exception:
                pass
        self._pw = None
        self.browser = None
        self.context = None
        self.http_session = None

    async def reset_context(self):
        try:
            await self.close()
        except Exception:
            pass
        try:
            await self.start()
        except Exception:
            log.warning("reset_context failed", exc_info=True)

    def _load_slow_hosts(self) -> None:
        try:
            path = self.slow_hosts_path
            if not path or not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = [p.strip() for p in line.strip().split(",") if p.strip()]
                    if not parts:
                        continue
                    host = parts[0]
                    ts = 0
                    count = 1
                    if len(parts) >= 2:
                        try:
                            ts = int(float(parts[1]))
                        except Exception:
                            ts = 0
                    if len(parts) >= 3:
                        try:
                            count = max(1, int(float(parts[2])))
                        except Exception:
                            count = 1
                    if not host:
                        continue
                    meta = self.slow_hosts.get(host)
                    if meta:
                        if len(parts) >= 3:
                            meta["count"] = max(meta.get("count", 1), count)
                        else:
                            meta["count"] = meta.get("count", 1) + 1
                        meta["last_ts"] = max(meta.get("last_ts", 0), ts)
                    else:
                        self.slow_hosts[host] = {"count": count, "last_ts": ts}
            self._prune_slow_hosts()
            self._persist_slow_hosts()
        except Exception:
            log.warning("failed to load slow hosts", exc_info=True)

    def _add_slow_host(self, host: str) -> None:
        if not host:
            return
        now_ts = int(time.time())
        meta = self.slow_hosts.get(host)
        if meta:
            meta["count"] = int(meta.get("count", 1)) + 1
            meta["last_ts"] = now_ts
        else:
            self.slow_hosts[host] = {"count": 1, "last_ts": now_ts}
        self._prune_slow_hosts()
        try:
            self._persist_slow_hosts()
        except Exception:
            log.debug("failed to persist slow host: %s", host, exc_info=True)

    def _persist_slow_hosts(self) -> None:
        path = self.slow_hosts_path
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for host, meta in sorted(self.slow_hosts.items()):
                count = int(meta.get("count", 1))
                last_ts = int(meta.get("last_ts", 0))
                f.write(f"{host},{last_ts},{count}\n")

    def _prune_slow_hosts(self) -> None:
        if self.slow_host_ttl_sec <= 0:
            return
        now_ts = int(time.time())
        expired = [
            host for host, meta in self.slow_hosts.items()
            if now_ts - int(meta.get("last_ts", 0)) > self.slow_host_ttl_sec
        ]
        for host in expired:
            self.slow_hosts.pop(host, None)

    def _is_slow_host(self, host: str) -> bool:
        if not host:
            return False
        meta = self.slow_hosts.get(host)
        if not meta:
            return False
        if self.slow_host_ttl_sec > 0:
            now_ts = int(time.time())
            last_ts = int(meta.get("last_ts", 0))
            if now_ts - last_ts > self.slow_host_ttl_sec:
                self.slow_hosts.pop(host, None)
                return False
        return int(meta.get("count", 1)) >= self.slow_host_hits

    async def _handle_route(self, route: Route):
        rtype = route.request.resource_type
        if rtype in {"image", "media", "font", "stylesheet"}:
            await route.abort()
        else:
            await route.continue_()

    # ===== 検索 =====
    @staticmethod
    def _decode_uddg(url: str) -> str:
        if not url:
            return url
        try:
            parsed = urlparse("https://duckduckgo.com" + url) if url.startswith("/l") else urlparse(url)
            if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l"):
                qs = parse_qs(parsed.query)
                if "uddg" in qs and qs["uddg"]:
                    return unquote(qs["uddg"][0])
        except Exception:
            pass
        return url

    def _prioritize(self, urls: List[str]) -> List[str]:
        def score(u: str) -> int:
            s = 0
            low = u.lower()
            if any(k in low for k in ("recruit", "採用", "ir", "faq", "support", "news")):
                s -= 3
            for k in self.CANDIDATE_PRIORITIES:
                if k.lower() in low:
                    s += 2
            if low.startswith("https://"):
                s += 1
            return s
        return sorted(urls, key=score, reverse=True)

    def _prioritize_paths(self, urls: List[str]) -> List[str]:
        def score(u: str) -> int:
            path = urllib.parse.urlparse(u).path.lower()
            total = 0
            for idx, marker in enumerate(self.PRIORITY_PATHS):
                if marker.lower() in path:
                    total += len(self.PRIORITY_PATHS) - idx
            return total

        return sorted(urls, key=score, reverse=True)

    @staticmethod
    def _phone_variants_regex(phone: str) -> re.Pattern:
        digits = re.sub(r"\D", "", phone or "")
        if not digits:
            return re.compile(r"$^")
        pattern = r"\D*".join(map(re.escape, digits))
        return re.compile(pattern)

    @staticmethod
    def _addr_key(addr: str) -> str:
        if not addr:
            return ""
        text = unicodedata.normalize("NFKC", addr)
        text = re.sub(r"[‐―－ーｰ-]+", "-", text)
        text = re.sub(r"\s+", "", text)
        return text.lower()

    @staticmethod
    def _extract_prefecture(address: str | None) -> str:
        if not address:
            return ""
        for pref in PREFECTURE_NAMES:
            if pref in address:
                return pref
        return ""

    @staticmethod
    def _extract_postal_code(address: str | None) -> str:
        if not address:
            return ""
        m = re.search(r"(\d{3})[-\s]?(\d{4})", address)
        if not m:
            return ""
        return f"{m.group(1)}{m.group(2)}"

    @staticmethod
    def _extract_city(address: str | None) -> str:
        if not address:
            return ""
        match = CITY_RE.search(address)
        return match.group(1) if match else ""

    @staticmethod
    def _address_matches(expected: str, candidate: str) -> bool:
        if not expected or not candidate:
            return False

        def norm(s: str) -> str:
            return CompanyScraper._addr_key(s)

        exp = norm(expected)
        cand = norm(candidate)
        if not exp or not cand:
            return False
        if exp in cand or cand in exp:
            return True

        digits_exp = re.sub(r"\D", "", exp)
        digits_cand = re.sub(r"\D", "", cand)
        if len(digits_exp) >= 7 and digits_exp[:7] in digits_cand:
            tail_exp = digits_exp[-4:] if len(digits_exp) >= 4 else ""
            if tail_exp and tail_exp in digits_cand:
                return True
        if len(digits_cand) >= 7 and digits_cand[:7] in digits_exp:
            tail_cand = digits_cand[-4:] if len(digits_cand) >= 4 else ""
            if tail_cand and tail_cand in digits_exp:
                return True

        ratio = SequenceMatcher(None, exp, cand).ratio()
        return ratio >= 0.55

    async def verify_on_site(
        self,
        base_url: str,
        phone: Optional[str],
        address: Optional[str],
        fetch_limit: int = 5,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "phone_ok": False,
            "address_ok": False,
            "phone_url": None,
            "address_url": None,
        }
        if not base_url:
            return result

        try:
            parsed = urllib.parse.urlparse(base_url)
        except Exception:
            return result

        if not parsed.scheme or not parsed.netloc:
            return result

        base_root = f"{parsed.scheme}://{parsed.netloc}"
        candidates: List[str] = [base_url]
        verify_priority_paths = [
            "/company", "/about", "/profile", "/corporate", "/overview",
            "/gaiyou", "/gaiyo", "/kaisya", "/info", "/information",
            "/contact", "/inquiry", "/access",
            "/会社案内",
        ]
        for path in verify_priority_paths + list(self.PRIORITY_PATHS):
            try:
                candidate = urllib.parse.urljoin(base_root, path)
            except Exception:
                continue
            candidates.append(candidate)

        seen: set[str] = set()
        targets: List[str] = []
        for url in candidates:
            parsed_candidate = urllib.parse.urlparse(url)
            if parsed_candidate.netloc != parsed.netloc:
                continue
            if url in seen:
                continue
            seen.add(url)
            targets.append(url)
            if len(targets) >= fetch_limit:
                break

        phone_pattern = self._phone_variants_regex(phone) if phone else None
        addr_key = self._addr_key(address) if address else ""

        verify_deadline = time.monotonic() + max(5.0, min(fetch_limit * max(self.page_timeout_ms / 1000.0, 1.0), 20.0))

        for idx, target in enumerate(targets):
            if time.monotonic() >= verify_deadline:
                break
            try:
                allow_slow = idx == 0
                if not allow_slow:
                    try:
                        parsed_target = urllib.parse.urlparse(target)
                        host = (parsed_target.netloc or "").lower().split(":")[0]
                    except Exception:
                        host = ""
                    if host and self.skip_slow_hosts and self._is_slow_host(host):
                        continue
                info = await self.get_page_info(target, allow_slow=allow_slow)
            except Exception:
                continue
            text = info.get("text", "") or ""
            if phone_pattern and not result["phone_ok"]:
                if phone_pattern.search(text):
                    result["phone_ok"] = True
                    result["phone_url"] = target
            if addr_key and not result["address_ok"]:
                text_key = self._addr_key(text)
                if addr_key and addr_key in text_key:
                    result["address_ok"] = True
                    result["address_url"] = target
            if result["phone_ok"] and result["address_ok"]:
                break

        return result

    async def search_company(self, company_name: str, address: str, num_results: int = 3) -> List[str]:
        """
        DuckDuckGoで検索し、候補URLを返す（「公式」「会社概要」の2クエリのみ）。
        """
        key = (
            unicodedata.normalize("NFKC", company_name or "").strip(),
            unicodedata.normalize("NFKC", address or "").strip(),
        )
        cached = self.search_cache.get(key)
        if cached:
            return list(cached)
        queries = self._build_company_queries(company_name, address)
        if not queries:
            return []

        def _unique_queries(qs: list[str]) -> list[str]:
            seen: set[str] = set()
            ordered: list[str] = []
            for q in qs:
                normalized = q.strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                ordered.append(normalized)
            return ordered

        max_candidates = max(1, num_results or 1)

        async def run_queries(qs: list[str]) -> list[Dict[str, Any]]:
            candidates: List[Dict[str, Any]] = []
            seen: set[str] = set()
            engines = self.search_engines or ["ddg"]

            async def run_engine(engine: str, q_idx: int, query: str) -> str:
                if engine == "bing":
                    return await self._fetch_bing(query)
                return await self._fetch_duckduckgo(query)

            for q_idx, query in enumerate(qs):
                for eng in engines:
                    try:
                        html = await run_engine(eng, q_idx, query)
                    except Exception:
                        continue
                    if not html:
                        continue
                    extractor = self._extract_bing_urls if eng == "bing" else self._extract_search_urls
                    for rank, url in enumerate(extractor(html)):
                        if url in seen:
                            continue
                        seen.add(url)
                        candidates.append({"url": url, "query_idx": q_idx, "rank": rank, "engine": eng})
                        if len(candidates) >= max_candidates:
                            return candidates
                if len(candidates) >= max_candidates:
                    break
            return candidates

        queries = _unique_queries(queries)
        candidates = await run_queries(queries)
        if not candidates:
            # フォールバック: 社名単独/社名+公式で再検索
            fallback_queries = []
            plain_name = unicodedata.normalize("NFKC", company_name or "").strip()
            if plain_name:
                fallback_queries.append(plain_name)
                fallback_queries.append(f"{plain_name} 公式")
            fallback_queries = _unique_queries(fallback_queries)
            candidates = await run_queries(fallback_queries) if fallback_queries else []
        if not candidates:
            return []

        company_tokens = self._company_tokens(company_name)
        scored: List[tuple[int, int, int, str]] = []
        for item in candidates:
            url = item["url"]
            score = self._domain_score(company_tokens, url)
            score += max(0, 6 - item["rank"])
            if item["query_idx"] == 0:
                score += 3
            score += self._path_priority_value(url)
            scored.append((score, item["query_idx"], item["rank"], url))

        scored.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))
        ordered: List[str] = []
        for _, _, _, url in scored:
            ordered.append(url)
        result = ordered[:max_candidates]
        self.search_cache[key] = list(result)
        return result

    # ===== ページ取得（HTTP優先＋ブラウザ再利用） =====
    async def _fetch_http_info(
        self,
        url: str,
        timeout_ms: int | None = None,
        allow_slow: bool = False,
    ) -> Dict[str, Any]:
        """
        軽量なHTTPリクエストで本文/HTMLのみ取得。失敗時は空を返す。
        """
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.netloc or "").lower().split(":")[0]
        except Exception:
            host = ""
        if host and (host in self.HARD_EXCLUDE_HOSTS or self._is_excluded(host)):
            return {"url": url, "text": "", "html": ""}
        # 公式候補ホストはスキップ対象から除外するため、上位層で呼び分ける
        if host and self.skip_slow_hosts and self._is_slow_host(host) and not allow_slow:
            log.info("[http] skip slow host %s url=%s", host, url)
            return {"url": url, "text": "", "html": ""}

        eff_timeout_ms = self.http_timeout_ms if timeout_ms is None else max(500, int(timeout_ms))
        timeout_sec = max(2, eff_timeout_ms / 1000)
        started = time.monotonic()
        try:
            resp = await asyncio.to_thread(
                self._session_get,
                url,
                timeout=(timeout_sec, timeout_sec),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            raw = resp.content or b""
            encoding = self._detect_html_encoding(resp, raw)
            decoded = raw.decode(encoding, errors="replace") if raw else ""
            html = decoded or ""
            text = self._clean_text_from_html(html, fallback_text=decoded or "")
            elapsed_ms = (time.monotonic() - started) * 1000
            if self.slow_page_threshold_ms > 0 and elapsed_ms > self.slow_page_threshold_ms and host:
                self._add_slow_host(host)
                log.info("[http] mark slow host (%.0f ms) %s", elapsed_ms, host)
            return {"url": url, "text": text, "html": html}
        except Exception:
            elapsed_ms = (time.monotonic() - started) * 1000
            if self.slow_page_threshold_ms > 0 and elapsed_ms > self.slow_page_threshold_ms and host:
                self._add_slow_host(host)
                log.warning("[http] timeout/slow (%.0f ms) -> skip host next time: %s", elapsed_ms, host or "")
            return {"url": url, "text": "", "html": ""}

    # ===== ページ取得（ブラウザ再利用＋軽いリトライ） =====
    async def get_page_info(self, url: str, timeout: int | None = None, need_screenshot: bool = False, allow_slow: bool = False) -> Dict[str, Any]:
        """
        対象URLの本文テキストとフルページスクショを取得（2回まで再試行）
        """
        cached = self.page_cache.get(url)
        cached_shot = bool(cached and cached.get("screenshot"))
        if cached and (cached_shot or not need_screenshot):
            return cached

        http_fallback: Dict[str, Any] | None = None
        # まずHTTPで軽量取得を試す（スクショ不要の場合）
        if self.use_http_first and not need_screenshot:
            http_info = await self._fetch_http_info(url)
            text_len = len((http_info.get("text") or "").strip())
            html_len = len(http_info.get("html") or "")
            http_fallback = {
                "url": url,
                "text": http_info.get("text", "") or "",
                "html": http_info.get("html", "") or "",
                "screenshot": b"",
            }
            # 軽量取得で十分な本文/HTMLが取れた場合のみ即返す。薄い場合はブラウザで再取得。
            if text_len >= 200 or html_len >= 1800:
                self.page_cache[url] = {
                    "url": url,
                    "text": http_info.get("text", "") or "",
                    "html": http_info.get("html", "") or "",
                    "screenshot": b"",
                }
                return self.page_cache[url]

        if not self.context:
            if self.browser_disabled:
                if http_fallback is not None:
                    return http_fallback
                if cached:
                    return cached
                return {"url": url, "text": "", "html": "", "screenshot": b""}
            try:
                await self.start()
            except Exception:
                self.browser_disabled = True
                self.browser_disabled_reason = "start_failed"
                # ブラウザ起動に失敗した場合は HTTP 取得結果（同一呼び出し内）にフォールバックする
                if http_fallback is not None:
                    return http_fallback
                if cached:
                    return cached
                return {"url": url, "text": "", "html": "", "screenshot": b""}

        eff_timeout = timeout or self.page_timeout_ms
        if self.slow_page_threshold_ms > 0:
            eff_timeout = min(eff_timeout, self.slow_page_threshold_ms)
        total_deadline = time.monotonic() + max(2, eff_timeout) / 1000

        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.netloc or "").lower().split(":")[0]
        except Exception:
            host = ""
        if host and (host in self.HARD_EXCLUDE_HOSTS or self._is_excluded(host)):
            return {"url": url, "text": "", "html": "", "screenshot": b""}

        # 公式候補などで明示的に許可された場合は skip_slow_hosts を無視できるようにする
        # 上位で allow_slow=True をセットする呼び出しを追加する。
        if host and self.skip_slow_hosts and self._is_slow_host(host) and not allow_slow:
            log.info("[page] skip slow host %s url=%s", host, url)
            fallback = {"url": url, "text": "", "html": "", "screenshot": b""}
            if cached:
                fallback["text"] = cached.get("text", "")
                fallback["html"] = cached.get("html", "")
                fallback["screenshot"] = cached.get("screenshot", b"") or b""
            return fallback

        browser_sem = self._get_browser_sem()
        for attempt in range(2):
            remaining_ms = int((total_deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            attempt_timeout = min(eff_timeout, remaining_ms)
            started = time.monotonic()
            page: Page | None = None
            goto_ms = 0.0
            network_idle_ms = 0.0
            marked_slow = False
            try:
                async with browser_sem:
                    page = await self.context.new_page()
                    page.set_default_timeout(attempt_timeout)
                    goto_started = time.monotonic()
                    await page.goto(url, timeout=attempt_timeout, wait_until="domcontentloaded")
                    goto_ms = (time.monotonic() - goto_started) * 1000
                    if self.network_idle_timeout_ms > 0:
                        net_started = time.monotonic()
                        try:
                            await page.wait_for_load_state(
                                "networkidle",
                                timeout=min(self.network_idle_timeout_ms, attempt_timeout),
                            )
                        except Exception:
                            pass
                        network_idle_ms = (time.monotonic() - net_started) * 1000
                    try:
                        text = await page.inner_text("body", timeout=5000)
                    except Exception:
                        try:
                            await page.wait_for_load_state(
                                "load",
                                timeout=min(self.load_wait_timeout_ms, attempt_timeout),
                            )
                        except Exception:
                            pass
                        text = await page.inner_text("body") if await page.locator("body").count() else ""
                    if text and len(text.strip()) < 40:
                        try:
                            await page.wait_for_timeout(1200)
                            text = await page.inner_text("body")
                        except Exception:
                            pass
                    try:
                        html = await page.content()
                    except Exception:
                        html = ""
                    # JS生成で本文が薄い場合の最小フォールバック（待機のみ。追加fetchはしない）
                    if (not text or len(text.strip()) < 80) and html:
                        try:
                            spa_hint = bool(
                                ("__NEXT_DATA__" in html)
                                or ("id=\"app\"" in html)
                                or ("id=\"root\"" in html)
                                or ("data-reactroot" in html)
                                or ("nuxt" in html.lower())
                            )
                            if spa_hint:
                                await page.wait_for_selector("table,dl,footer,address", timeout=min(1500, attempt_timeout))
                                await page.wait_for_timeout(250)
                                text = await page.inner_text("body")
                                html = await page.content()
                        except Exception:
                            pass
                    screenshot: bytes = b""
                    if need_screenshot:
                        screenshot = await page.screenshot(full_page=True)
                cleaned_text = self._clean_text_from_html(html, fallback_text=text or "")
                result = {"url": url, "text": cleaned_text, "html": html, "screenshot": screenshot}
                if cached and not screenshot:
                    # 再訪時にスクショなしなら旧データを活かす
                    if cached.get("screenshot"):
                        result["screenshot"] = cached["screenshot"]
                    if not text:
                        result["text"] = cached.get("text", "")
                    if not html:
                        result["html"] = cached.get("html", "")
                elapsed_ms = (time.monotonic() - started) * 1000
                effective_elapsed_ms = max(0.0, elapsed_ms - network_idle_ms)
                if elapsed_ms >= eff_timeout:
                    log.info(
                        "[page] slow fetch (elapsed=%.0f ms goto=%.0f ms net_idle=%.0f ms) -> host=%s url=%s",
                        elapsed_ms,
                        goto_ms,
                        network_idle_ms,
                        host,
                        url,
                    )
                if self.slow_page_threshold_ms > 0 and effective_elapsed_ms > self.slow_page_threshold_ms:
                    if host:
                        self._add_slow_host(host)
                        marked_slow = True
                    log.info(
                        "[page] mark slow host (elapsed=%.0f ms goto=%.0f ms net_idle=%.0f ms) %s",
                        effective_elapsed_ms,
                        goto_ms,
                        network_idle_ms,
                        host or "",
                    )
                self.page_cache[url] = result
                return result

            except PlaywrightTimeoutError:
                # 軽く待ってリトライ
                await asyncio.sleep(0.7 * (attempt + 1))
            except Exception:
                # 予期せぬ例外も1回だけ再試行
                await asyncio.sleep(0.7 * (attempt + 1))
            finally:
                elapsed_ms = (time.monotonic() - started) * 1000
                effective_elapsed_ms = max(0.0, elapsed_ms - network_idle_ms)
                if (
                    self.slow_page_threshold_ms > 0
                    and effective_elapsed_ms > self.slow_page_threshold_ms
                    and not marked_slow
                ):
                    if host:
                        self._add_slow_host(host)
                    log.warning(
                        "[page] timeout/slow (elapsed=%.0f ms goto=%.0f ms net_idle=%.0f ms) -> skip host next time: %s",
                        effective_elapsed_ms,
                        goto_ms,
                        network_idle_ms,
                        host or "",
                    )
                if page:
                    await page.close()

        fallback = {"url": url, "text": "", "html": "", "screenshot": b""}
        if cached:
            fallback["text"] = cached.get("text", "")
            fallback["html"] = cached.get("html", "")
            fallback["screenshot"] = cached.get("screenshot", b"") or b""
        elif http_fallback is not None:
            fallback["text"] = http_fallback.get("text", "")
            fallback["html"] = http_fallback.get("html", "")
        self.page_cache[url] = fallback
        return fallback

    # ===== 同一ドメイン内を浅く探索 =====
    FOCUS_KEYWORD_MAP = {
        "phone": {
            "anchor": (
                "お問い合わせ", "お問合せ", "問合せ", "contact", "contact us", "電話", "tel", "連絡先",
                "窓口", "カスタマー", "サポート", "support",
            ),
            "path": (
                "/contact", "/inquiry", "/support", "/contact-us", "/toiawase", "/otoiawase",
            ),
        },
        "address": {
            "anchor": (
                "所在地", "本社", "本店", "アクセス", "地図", "map",
                "会社概要", "会社案内", "会社情報", "企業情報", "法人概要", "corporate", "about", "profile", "overview",
            ),
            "path": (
                "/access", "/map", "/company", "/about", "/corporate", "/profile", "/overview", "/gaiyo", "/gaiyou",
            ),
        },
        "rep": {
            "anchor": (
                "役員", "代表", "代表者", "代表取締役", "代表理事", "理事長",
                "社長", "CEO", "ceo", "代表挨拶", "トップメッセージ", "メッセージ", "役員紹介", "経営陣", "役員一覧",
                "会社概要", "会社案内", "会社情報", "企業情報", "法人概要", "profile", "corporate",
            ),
            "path": (
                "/company", "/about", "/profile", "/corporate", "/overview",
                "/message", "/greeting", "/president", "/ceo", "/executive", "/leadership", "/management",
            ),
        },
        "contact": {
            "anchor": (
                "お問い合わせ", "お問合せ", "問合せ", "contact", "アクセス", "電話", "tel", "連絡先",
                "アクセスマップ", "map", "所在地", "本社", "本店"
            ),
            "path": (
                "/contact", "/inquiry", "/access", "/support", "/contact-us"
            ),
        },
        "finance": {
            "anchor": (
                "IR", "ir", "investor", "投資家", "財務", "決算", "disclosure", "financial", "決算短信",
                "開示", "事業報告", "annual report"
            ),
            "path": (
                "/ir", "/investor", "/financial", "/disclosure", "/ir-library", ".pdf"
            ),
        },
        "profile": {
            "anchor": (
                "会社概要", "会社案内", "企業情報", "法人概要", "事業紹介", "about", "profile", "corporate", "沿革",
                "組織図", "会社案内", "overview", "company"
            ),
            "path": (
                "/company", "/about", "/profile", "/corporate", "/overview", "/gaiyo", "/gaiyou"
            ),
        },
        "overview": {
            "anchor": (
                "公式サイト", "公式", "企業理念", "事業内容", "事業目的", "企業概要", "会社紹介",
                "business", "services", "solutions", "official"
            ),
            "path": (
                "/official", "/official-site", "/company-profile", "/company-overview", "/business",
                "/services", "/about-us", "/corporate-profile", "/gaiyou", "/company"
            ),
        },
    }

    def _rank_links(self, base: str, html: str, *, focus: Optional[set[str]] = None) -> List[str]:
        base_host = urlparse(base).netloc
        candidates: List[tuple[int, int, int, str]] = []
        fallback_links: List[str] = []
        seen_links: set[str] = set()
        focus = focus or set()

        try:
            soup = BeautifulSoup(html or "", "html.parser")
            anchors = soup.find_all("a", href=True)
        except Exception:
            anchors = []

        raw_links: List[tuple[str, str]] = []
        if anchors:
            for anchor in anchors:
                href = anchor.get("href")
                if not href:
                    continue
                text = anchor.get_text(separator=" ", strip=True) or ""
                title = anchor.get("title") or ""
                anchor_text = text or title
                raw_links.append((href, anchor_text))
        else:
            for href in re.findall(r'href=["\']([^"\']+)["\']', html or "", flags=re.I):
                raw_links.append((href, ""))

        focus_anchor_words: set[str] = set()
        focus_path_words: set[str] = set()
        for key in focus:
            mapping = self.FOCUS_KEYWORD_MAP.get(key)
            if not mapping:
                continue
            focus_anchor_words.update(mapping.get("anchor", ()))
            focus_path_words.update(mapping.get("path", ()))

        for href, anchor_text in raw_links:
            url = urljoin(base, href)
            parsed = urlparse(url)
            if not parsed.netloc or parsed.netloc != base_host:
                continue

            normalized_url = url.lower()
            path = parsed.path or "/"
            path_lower = path.lower()
            anchor_text = anchor_text.strip()
            anchor_lower = anchor_text.lower()

            score = 0

            for kw in PRIORITY_PATHS:
                kw_lower = kw.lower()
                if kw in path or kw_lower in path_lower:
                    score += 12

            for word in PRIO_WORDS:
                word_lower = word.lower()
                if word in normalized_url or word_lower in normalized_url:
                    score += 6

            for word in ANCHOR_PRIORITY_WORDS:
                word_lower = word.lower()
                if word and (word in anchor_text or word_lower in anchor_lower):
                    score += 8

            if focus_anchor_words:
                for word in focus_anchor_words:
                    if not word:
                        continue
                    if word.lower() in anchor_lower:
                        score += 6
                        break
            if focus_path_words:
                for word in focus_path_words:
                    if word and word in path_lower:
                        score += 4
                        break

            if score > 0:
                path_depth = max(parsed.path.count("/"), 1)
                text_len = max(len(anchor_text), 1)
                candidates.append((score, path_depth, text_len, url))
            else:
                if url not in seen_links and len(fallback_links) < 8:
                    fallback_links.append(url)
                    seen_links.add(url)

        if not candidates:
            return fallback_links

        candidates.sort(key=lambda x: (-x[0], x[1], -x[2], x[3]))
        ordered: List[str] = []
        seen: set[str] = set()
        for _, _, _, url in candidates:
            if url not in seen:
                ordered.append(url)
                seen.add(url)
            if len(ordered) >= 20:
                break
        return ordered

    def _find_priority_links(self, base: str, html: str, max_links: int = 4, target_types: Optional[list[str]] = None) -> List[str]:
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
        base_host = urlparse(base).netloc
        scored: List[tuple[int, int, int, str]] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href")
            if not href:
                continue
            url = urljoin(base, href)
            parsed = urlparse(url)
            if not parsed.netloc or parsed.netloc != base_host:
                continue
            token = " ".join([
                anchor.get_text(separator=" ", strip=True) or "",
                anchor.get("title") or "",
                href or "",
            ]).lower()
            score = 0
            # 会社概要導線は deep の最優先（URL文字列に頼らず、リンクテキスト/タイトルも重視）
            profile_text_keywords = (
                "会社概要", "会社情報", "企業情報", "企業概要", "会社案内", "法人概要", "概要",
                "profile", "outline", "overview", "company profile", "corporate profile",
            )
            business_text_keywords = (
                "事業内容", "事業紹介", "サービス", "Business", "Service", "Products", "Solutions",
            )
            token_low = token.lower()
            profile_hit = any(kw.lower() in token_low for kw in profile_text_keywords)
            business_hit = (not profile_hit) and any(kw.lower() in token_low for kw in business_text_keywords)

            # 欠損フィールドに応じて対象カテゴリを限定（Noneなら全部）
            include_contact = (not target_types) or ("contact" in target_types)
            include_about = (not target_types) or ("about" in target_types)
            include_finance = (not target_types) or ("finance" in target_types)

            if profile_hit:
                score += 20
            elif business_hit:
                score += 8
            if include_about:
                for kw in PRIORITY_SECTION_KEYWORDS:
                    if kw in token:
                        score += 6
            if include_contact:
                for kw in PRIORITY_CONTACT_KEYWORDS:
                    if kw in token:
                        score += 4
            if include_finance:
                if any(kw in token for kw in ("ir", "investor", "financial", "決算", "ディスクロージャー")):
                    score += 5
            if not score:
                continue
            for path_kw in PRIORITY_PATHS:
                if path_kw.lower() in (parsed.path or "").lower():
                    # /company /about 等の「会社概要系」パスは強めに加点
                    if path_kw.lower() in ("/company", "/about", "/corporate", "/profile", "/overview", "/outline"):
                        score += 8
                    else:
                        score += 2
                    break
            depth = parsed.path.count("/")
            text_len = len(anchor.get_text(strip=True) or "")
            if url not in seen:
                scored.append((score, depth, -text_len, url))
                seen.add(url)

        scored.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))
        return [url for _, _, _, url in scored[:max_links]]

    def _allow_slow_for_priority_link(self, url: str, target_types: Optional[list[str]] = None) -> bool:
        if not url:
            return False
        if target_types:
            has_about = "about" in target_types
            has_contact = "contact" in target_types
            if not (has_about or has_contact):
                return False
        try:
            path = urllib.parse.urlparse(url).path.lower()
        except Exception:
            return False
        allow_segments = (
            "/company", "/about", "/profile", "/overview", "/outline", "/corporate",
            "/contact", "/contactus", "/inquiry", "/toiawase", "/access", "/form",
            "/companyinfo", "/info",
        )
        return any(seg in path for seg in allow_segments)


    def _fallback_priority_links(
        self,
        base_url: str,
        target_types: Optional[list[str]] = None,
    ) -> list[str]:
        if not base_url:
            return []
        try:
            parsed = urllib.parse.urlparse(base_url)
            base_root = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return []
        if not base_root:
            return []
        types = target_types or ["about", "contact"]
        fallback_map = {
            "about": [
                "/company",
                "/about",
                "/profile",
                "/overview",
                "/outline",
                "/corporate",
                "/company/outline",
                "/company/profile",
                "/company/overview",
                "/company.html",
                "/about.html",
                "/company.php",
                "/about.php",
            ],
            "contact": [
                "/contact",
                "/contactus",
                "/inquiry",
                "/contact.html",
                "/contact.php",
                "/inquiry.html",
                "/form",
            ],
            "finance": [
                "/ir",
                "/ir/financial",
                "/ir/finance",
                "/ir/library",
                "/ir.html",
            ],
        }
        ordered: list[str] = []
        seen: set[str] = set()
        for t in types:
            for path in fallback_map.get(t, []):
                try:
                    url = urllib.parse.urljoin(base_root + "/", path.lstrip("/"))
                except Exception:
                    continue
                if not url or url in seen:
                    continue
                seen.add(url)
                ordered.append(url)
        return ordered

    async def fetch_priority_documents(
        self,
        base_url: str,
        base_html: Optional[str] = None,
        max_links: int = 4,
        concurrency: int = 3,
        target_types: Optional[list[str]] = None,
        *,
        allow_slow: bool = False,
        exclude_urls: Optional[set[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        docs: Dict[str, Dict[str, Any]] = {}
        if not base_url:
            return docs
        concurrency = max(1, concurrency)
        html = base_html or ""
        initial_info: Optional[Dict[str, Any]] = None
        if not html:
            try:
                initial_info = await self.get_page_info(base_url, allow_slow=allow_slow)
                html = initial_info.get("html", "")
            except Exception:
                html = ""
        links = self._find_priority_links(base_url, html, max_links=max_links, target_types=target_types)
        if exclude_urls:
            links = [url for url in links if url not in exclude_urls]
        if len(links) < max_links:
            fallback_links = self._fallback_priority_links(base_url, target_types=target_types)
            if exclude_urls:
                fallback_links = [url for url in fallback_links if url not in exclude_urls]
            for url in fallback_links:
                if url in links:
                    continue
                links.append(url)
                if len(links) >= max_links:
                    break
        if not links:
            return docs

        sem = asyncio.Semaphore(concurrency)

        async def fetch(link: str):
            async with sem:
                allow_slow_link = bool(allow_slow and self._allow_slow_for_priority_link(link, target_types))
                info = await self.get_page_info(link, allow_slow=allow_slow_link)
            return link, info

        tasks = [asyncio.create_task(fetch(link)) for link in links]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception) or not res:
                continue
            link, info = res
            docs[link] = {
                "text": info.get("text", "") or "",
                "html": info.get("html", "") or "",
            }
        return docs

    async def crawl_related(
        self,
        homepage: str,
        need_phone: bool,
        need_addr: bool,
        need_rep: bool,
        max_pages: int = 4,
        max_hops: int = 1,
        *,
        need_listing: bool = False,
        need_capital: bool = False,
        need_revenue: bool = False,
        need_profit: bool = False,
        need_fiscal: bool = False,
        need_founded: bool = False,
        need_description: bool = False,
        initial_info: Optional[Dict[str, Any]] = None,
        expected_address: Optional[str] = None,
        return_meta: bool = False,
        allow_slow: bool = False,
    ) -> Dict[str, Dict[str, Any]] | tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        meta: Dict[str, Any] = {
            "max_pages_requested": int(max_pages or 0),
            "max_hops_requested": int(max_hops or 0),
            "pages_visited": 0,
            "fetch_count": 0,
            "fetch_failures": 0,
            "skipped_pref_mismatch": 0,
            "urls_visited": [],
            "stop_reason": "",
        }
        if not homepage:
            meta["stop_reason"] = "no_homepage"
            return (results, meta) if return_meta else results
        if not (
            need_phone or need_addr or need_rep or need_listing or need_capital
            or need_revenue or need_profit or need_fiscal or need_founded or need_description
        ):
            meta["stop_reason"] = "no_missing_fields"
            return (results, meta) if return_meta else results
        # 欠損が少ない場合は探索幅を縮小する
        missing_contact = int(need_phone) + int(need_addr) + int(need_rep)
        missing_extra = int(need_listing) + int(need_capital) + int(need_revenue) + int(need_profit) + int(need_fiscal) + int(need_founded) + int(need_description)
        if missing_contact == 0 and missing_extra == 0:
            meta["stop_reason"] = "no_missing_fields"
            return (results, meta) if return_meta else results
        # 必要最低限のページ/ホップに調整
        if missing_contact == 0 and missing_extra <= 2:
            max_pages = min(max_pages, 1)
            max_hops = min(max_hops, 1)
        elif missing_contact == 0 and missing_extra <= 4:
            max_pages = min(max_pages, 2)
            max_hops = min(max_hops, 1)
        else:
            max_pages = max_pages
            max_hops = max_hops

        # 探索の上限は軽め（2〜3ページ）に抑える
        max_pages_cap = 6 if (need_rep or need_description) else 4
        max_hops_cap = 4 if (need_rep or need_description) else 3
        max_pages = max(0, min(int(max_pages or 0), max_pages_cap))
        max_hops = max(0, min(int(max_hops or 0), max_hops_cap))
        meta["max_pages"] = int(max_pages)
        meta["max_hops"] = int(max_hops)
        if max_pages <= 0:
            meta["stop_reason"] = "max_pages=0"
            return (results, meta) if return_meta else results

        expected_pref = self._extract_prefecture(expected_address or "")
        visited: set[str] = {homepage}
        concurrency = max(1, min(4, max_pages))
        sem = asyncio.Semaphore(concurrency)

        async def fetch_info(target: str) -> Optional[Dict[str, Any]]:
            meta["fetch_count"] += 1
            async with sem:
                try:
                    return await self.get_page_info(target, allow_slow=allow_slow)
                except Exception:
                    meta["fetch_failures"] += 1
                    return None

        queue: deque[tuple[int, str, Optional[Any]]] = deque()
        if initial_info:
            queue.append((0, homepage, initial_info))
        else:
            queue.append((0, homepage, None))

        pending_tasks: list[asyncio.Task] = []

        while queue and len(results) < max_pages:
            hop, url, payload = queue.popleft()
            info = payload
            if isinstance(info, asyncio.Task):
                pending_tasks.append(info)
                try:
                    info = await info
                except Exception:
                    info = None
            if info is None:
                info = await fetch_info(url)
            if not info:
                continue

            results[url] = {
                "text": info.get("text", "") or "",
                "screenshot": info.get("screenshot"),
                "html": info.get("html", ""),
            }

            if hop >= max_hops:
                continue

            missing: List[str] = []
            if need_phone:
                missing.append("phone")
            if need_addr:
                missing.append("addr")
            if need_rep:
                missing.append("rep")
            if need_listing:
                missing.append("listing")
            if need_capital or need_revenue or need_profit or need_fiscal or need_founded:
                missing.append("finance")
            if need_description:
                missing.append("description")
            if not missing:
                continue

            html = info.get("html", "") or ""
            if not html:
                continue
            focus_targets: set[str] = set()
            if need_phone:
                focus_targets.update({"phone", "contact"})
            if need_addr:
                focus_targets.update({"address", "contact", "profile"})
            if need_rep:
                focus_targets.update({"rep", "profile"})
            if need_listing or need_capital or need_revenue or need_profit or need_fiscal or need_founded:
                focus_targets.add("finance")
            if need_description:
                focus_targets.update({"profile", "overview"})
            elif (need_listing or need_capital or need_revenue or need_profit or need_fiscal or need_founded):
                focus_targets.add("overview")
            ranked_links = self._rank_links(url, html, focus=focus_targets)
            priority_types: list[str] = []
            if need_phone or need_addr:
                priority_types.append("contact")
            if need_rep or need_description:
                priority_types.append("about")
            if need_listing or need_capital or need_revenue or need_profit or need_fiscal or need_founded:
                priority_types.append("finance")
            priority_links: list[str] = []
            if priority_types:
                priority_links = self._find_priority_links(url, html, max_links=3, target_types=priority_types)
                if len(priority_links) < 3:
                    fallback_links = self._fallback_priority_links(url, target_types=priority_types)
                    for link in fallback_links:
                        if link not in priority_links:
                            priority_links.append(link)
                        if len(priority_links) >= 3:
                            break
            if priority_links:
                priority_set = set(priority_links)
                ranked_links = priority_links + [link for link in ranked_links if link not in priority_set]
            # 入力住所の都道府県があるのに、ページ側の都道府県が明確に不一致なら深掘りを縮小する。
            # ただし「会社概要/企業情報」導線が欲しい場合が多いので profile/overview を狙う場合は継続する。
            if expected_pref and ("profile" not in focus_targets) and ("overview" not in focus_targets):
                found_prefs = set(PREFECTURE_NAME_RE.findall(results[url]["text"] or ""))
                if 0 < len(found_prefs) <= 3 and expected_pref not in found_prefs:
                    meta["skipped_pref_mismatch"] += 1
                    continue
            for child in ranked_links:
                if child in visited:
                    continue
                visited.add(child)
                task = asyncio.create_task(fetch_info(child))
                queue.append((hop + 1, child, task))
                if len(queue) + len(results) >= max_pages + concurrency:
                    break

        # キューに残ったTask（max_pages到達などで未回収）を必ず回収して例外ログを抑制する
        leftover_tasks: list[asyncio.Task] = []
        for _, _, payload in queue:
            if isinstance(payload, asyncio.Task):
                leftover_tasks.append(payload)
        leftover_tasks.extend([t for t in pending_tasks if isinstance(t, asyncio.Task)])
        for task in leftover_tasks:
            if not task.done():
                task.cancel()
        if leftover_tasks:
            await asyncio.gather(*leftover_tasks, return_exceptions=True)
        meta["pages_visited"] = len(results)
        meta["urls_visited"] = list(results.keys())
        if not meta.get("stop_reason"):
            if len(results) >= max_pages:
                meta["stop_reason"] = "max_pages_reached"
            elif not queue:
                meta["stop_reason"] = "queue_empty"
            else:
                meta["stop_reason"] = "unknown"
        return (results, meta) if return_meta else results

    def classify_page_type(self, url: str, text: str = "", html: str = "") -> Dict[str, Any]:
        """
        AI禁止の軽量ページ分類。
        COMPANY_PROFILE / ACCESS_CONTACT / BASES_LIST / DIRECTORY_DB / OTHER
        """
        url = url or ""
        text_nfkc = unicodedata.normalize("NFKC", text or "")
        text_low = text_nfkc.lower()

        directory = self._detect_directory_like(url, text=text_nfkc, html=html or "")
        if bool(directory.get("is_directory_like")):
            return {
                "page_type": "DIRECTORY_DB",
                "score": int(directory.get("directory_score") or 0),
                "reason": "directory_like",
                "directory_reasons": list(directory.get("directory_reasons") or [])[:8],
            }

        title = ""
        headings = ""
        has_table_or_dl = False
        try:
            soup = BeautifulSoup(html or "", "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            hs: list[str] = []
            for tag in ("h1", "h2", "h3"):
                for node in soup.find_all(tag)[:12]:
                    t = node.get_text(" ", strip=True)
                    if t:
                        hs.append(t)
            headings = " ".join(hs)
            has_table_or_dl = bool(soup.find("table") or soup.find("dl"))
            if not text_nfkc and html:
                try:
                    text_nfkc = unicodedata.normalize("NFKC", soup.get_text(" ", strip=True) or "")
                    text_low = text_nfkc.lower()
                except Exception:
                    pass
        except Exception:
            pass

        head_all = f"{title}\n{headings}".strip()
        head_low = unicodedata.normalize("NFKC", head_all).lower()

        profile_kw = (
            "会社概要", "会社情報", "企業情報", "会社案内", "法人概要", "企業概要",
            "outline", "profile", "overview", "company profile", "corporate profile",
        )
        contact_kw = (
            "お問い合わせ", "お問合せ", "問合せ", "contact", "inquiry",
            "アクセス", "access", "所在地", "location",
        )
        bases_kw = (
            "拠点一覧", "営業所一覧", "店舗一覧", "事業所一覧", "支店一覧", "工場一覧",
            "センター一覧", "拠点", "営業所", "事業所", "支店", "店舗", "工場", "センター",
        )

        label_hits = 0
        for kw in (
            "本社所在地", "本店所在地", "所在地", "住所", "電話", "TEL", "代表", "代表取締役",
            "設立", "創業", "資本金", "従業員", "事業内容", "許認可",
        ):
            if kw.lower() in text_low:
                label_hits += 1
        rep_label_hit = bool(
            re.search(r"(代表取締役|代表理事長|代表理事|代表者|社長|会長|CEO)", text_nfkc)
        )

        is_profile_heading = any(kw in head_low for kw in (k.lower() for k in profile_kw))
        is_profile_path = any(seg in (url.lower()) for seg in ("/company", "/about", "/corporate", "/profile", "/overview", "/outline"))
        has_hq_marker = ("本社所在地" in text_nfkc) or ("本店所在地" in text_nfkc) or ("本社" in text_nfkc) or ("本店" in text_nfkc)

        # BASES_LIST: 住所/拠点が多数並ぶページ（電話番号由来の誤検知を除外）
        phone_spans = [(m.start(), m.end()) for m in PHONE_RE.finditer(text_nfkc)]
        zip_hits = 0
        for m in re.finditer(r"\d{3}[-‐―－ー]?\d{4}", text_nfkc):
            if any(s <= m.start() < e for s, e in phone_spans):
                continue
            zip_hits += 1
        zip_count = zip_hits
        branch_hits = sum(1 for kw in bases_kw if kw.lower() in text_low)
        pref_count = len(set(PREFECTURE_NAME_RE.findall(text_nfkc)))
        is_bases_like = ("拠点一覧" in head_all) or ("店舗一覧" in head_all) or ("営業所一覧" in head_all) or zip_count >= 3 or pref_count >= 6 or branch_hits >= 6
        if is_bases_like:
            # 拠点一覧に見えても「会社概要(本社所在地など)」が同ページに強く載っている場合はプロフィール扱いに寄せる
            if (is_profile_heading or is_profile_path) and has_table_or_dl and label_hits >= 4 and has_hq_marker:
                return {"page_type": "COMPANY_PROFILE", "score": label_hits, "reason": "bases_list_with_profile_signals"}
            # 拠点一覧でも代表者ラベルが強く出ている場合はプロフィール扱いに寄せる
            if (
                has_table_or_dl
                and rep_label_hit
                and (is_profile_heading or is_profile_path or has_hq_marker)
                and label_hits >= 3
            ):
                return {"page_type": "COMPANY_PROFILE", "score": label_hits, "reason": "bases_list_with_rep_label"}
            return {"page_type": "BASES_LIST", "score": max(zip_count, pref_count, branch_hits), "reason": "bases_list_signals"}
        if (is_profile_heading or is_profile_path) and has_table_or_dl and label_hits >= 4:
            return {"page_type": "COMPANY_PROFILE", "score": label_hits, "reason": "profile_heading+labels"}
        if is_profile_path and label_hits >= 4 and (has_hq_marker or rep_label_hit):
            return {"page_type": "COMPANY_PROFILE", "score": label_hits, "reason": "profile_path+labels"}
        if is_profile_heading and label_hits >= 3:
            return {"page_type": "COMPANY_PROFILE", "score": label_hits, "reason": "profile_heading"}

        is_contact_heading = any(kw in head_low for kw in (k.lower() for k in contact_kw))
        if is_contact_heading or any(seg in url.lower() for seg in ("/contact", "/inquiry", "/access", "/toiawase")):
            return {"page_type": "ACCESS_CONTACT", "score": label_hits, "reason": "contact_heading_or_path"}

        return {"page_type": "OTHER", "score": label_hits, "reason": "default"}

    # ===== 抽出 =====
    def extract_candidates(self, text: str, html: Optional[str] = None) -> Dict[str, List[str]]:
        phones: List[str] = []
        addrs: List[str] = []
        reps: List[str] = []
        label_reps: List[str] = []
        rep_from_label = False

        def _normalize_label_text(raw: str) -> tuple[str, str]:
            cleaned = unicodedata.normalize("NFKC", raw or "")
            cleaned = cleaned.replace("\u200b", "").strip()
            if cleaned.startswith("・"):
                cleaned = cleaned.lstrip("・").strip()
            cleaned = cleaned.rstrip(":：").strip()
            compact = re.sub(r"[\s\u3000]+", "", cleaned)
            compact = compact.replace("・", "").replace("･", "")
            compact = compact.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
            compact = compact.replace("：", "").replace(":", "")
            return cleaned, compact

        def _label_matches(label_text: str, keyword: str) -> bool:
            cleaned, compact = _normalize_label_text(label_text)
            kw_cleaned, kw_compact = _normalize_label_text(keyword)
            if not kw_cleaned:
                return False
            if cleaned == kw_cleaned or cleaned.startswith(kw_cleaned) or kw_cleaned in cleaned:
                return True
            if compact and kw_compact and (compact == kw_compact or compact.startswith(kw_compact) or kw_compact in compact):
                return True
            return False

        def _field_for_label(label_text: str) -> str | None:
            for field, keywords in TABLE_LABEL_MAP.items():
                if any(_label_matches(label_text, kw) for kw in keywords):
                    return field
            return None

        def _is_value_for_field(field: str, raw_text: str) -> bool:
            cleaned = self._clean_text_value(raw_text)
            if not cleaned:
                return False
            if field == "rep_names":
                cand = self.clean_rep_name(cleaned)
                if not cand:
                    return False
                if len(cleaned) > 40 or re.search(r"[。．!?]", cleaned) or "、" in cleaned:
                    return False
                return bool(self._looks_like_person_name(cand))
            if field == "phone_numbers":
                return bool(PHONE_RE.search(cleaned))
            if field == "addresses":
                norm = self._normalize_address_candidate(cleaned)
                if norm and self._looks_like_full_address(norm):
                    return True
                return self.looks_like_address(cleaned)
            if field in {"capitals", "revenues", "profits"}:
                return self._is_amount_like(cleaned)
            if field == "fiscal_months":
                return bool(FISCAL_RE.search(cleaned))
            if field == "founded_years":
                return bool(self._parse_founded_year(cleaned))
            if field == "listing":
                return bool(
                    SECURITIES_CODE_RE.search(cleaned)
                    or MARKET_CODE_RE.search(cleaned)
                    or any(term in cleaned for term in LISTING_KEYWORDS)
                )
            return False

        for p in PHONE_RE.finditer(text or ""):
            cand = _normalize_phone_strict(p.group(0))
            if not cand:
                continue
            ctx = (text or "")[max(0, p.start() - 12):p.start()]
            fax_hint = bool(re.search(r"(FAX|Fax|fax|ファックス|ﾌｧｯｸｽ|ＦＡＸ)", ctx))
            tel_hint = bool(re.search(r"(TEL|Tel|tel|電話)", ctx))
            if fax_hint and not tel_hint:
                phones.append(f"[FAX]{cand}")
            else:
                phones.append(cand)

        def _extract_addrs_from_text() -> None:
            added_any = False
            for zm in ZIP_RE.finditer(text or ""):
                zip_code = f"〒{zm.group(1).replace('〒', '').strip()}-{zm.group(2)}"
                cursor = zm.end()
                snippet = (text or "")[cursor:cursor + 200]
                snippet = snippet.replace("\n", " ").replace("\r", " ").replace("\u3000", " ")
                snippet = re.split(
                    r"(地図アプリ|地図で見る|マップ|Google\s*マップ|地図|map|アクセス|ルート|拡大地図|gac?\.push|gtag|_gaq)",
                    snippet,
                    maxsplit=1,
                )[0]
                if ADDR_HINT.search(snippet):
                    cleaned = re.split(r"[。．、,，;；｜|/]", snippet, maxsplit=1)[0]
                    cleaned = re.sub(r"\s+", " ", cleaned)
                    parts = [
                        part.strip(" ：:・-‐―－ー〜~()（）[]{}<>")
                        for part in re.split(r"[ \t]+", cleaned)
                        if part.strip(" ：:・-‐―－ー〜~()（）[]{}<>")
                    ]
                    if parts:
                        seg = " ".join(parts[:8]).strip()
                        if seg:
                            norm_addr = self._normalize_address_candidate(f"{zip_code} {seg}")
                            if norm_addr and self._looks_like_full_address(norm_addr):
                                addrs.append(f"[TEXT]{norm_addr}")
                                added_any = True

            if not added_any:
                for cand in ADDR_FALLBACK_RE.findall(text or ""):
                    norm = self._normalize_address_candidate(cand)
                    if norm and self._looks_like_full_address(norm):
                        addrs.append(f"[TEXT]{norm}")
                        added_any = True

            # ZIP行と近傍行を縦持ちでも拾うフォールバック
            if not added_any:
                lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
                for idx, ln in enumerate(lines):
                    m = ZIP_RE.search(ln)
                    if not m:
                        continue
                    zip_code = f"〒{m.group(1).replace('〒', '').strip()}-{m.group(2)}"
                    # 直後1〜2行を結合して住所本体とみなす
                    body_parts: list[str] = []
                    for offset in (1, 2):
                        if idx + offset < len(lines):
                            body_parts.append(lines[idx + offset])
                    if not body_parts and idx > 0:
                        body_parts.append(lines[idx - 1])  # 前行も一応参照
                    body = " ".join(body_parts).strip()
                    if not body:
                        continue
                    cand_addr = f"{zip_code} {body}".strip()
                    norm = self._normalize_address_candidate(cand_addr)
                    if norm and self._looks_like_full_address(norm):
                        addrs.append(f"[TEXT]{norm}")
                        added_any = True

        if not self.rep_strict_sources:
            for rm in REP_RE.finditer(text or ""):
                cleaned = self.clean_rep_name(rm.group(1))
                if cleaned and self._looks_like_person_name(cleaned):
                    reps.append(cleaned)

            # 追加の代表者抽出: キーワードの近傍にある漢字氏名を拾う
            if not reps:
                rep_kw_pattern = re.compile(
                    r"(代表取締役社長|代表取締役会長|代表取締役|代表理事長|代表理事|代表者|社長|会長|理事長|組合長|院長|学長|園長|校長)"
                    r"[^\n\r]{0,20}?"
                    r"([一-龥]{1,3}(?:[・･ \u3000]{0,1}[一-龥]{1,3})+)"
                )
                for m in rep_kw_pattern.finditer(text or ""):
                    name_cand = m.group(2)
                    cleaned = self.clean_rep_name(name_cand)
                    if cleaned:
                        reps.append(cleaned)

            # 追加の代表者抽出: 氏名 → 役職 の並び（カードUI等）に対応
            if not reps:
                lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
                role_re = re.compile(
                    r"(代表取締役社長|代表執行役社長|代表取締役会長|代表取締役|代表理事長|代表理事|代表社員|代表者|代表|社長|会長|CEO)",
                    flags=re.I,
                )
                for idx in range(len(lines) - 1):
                    cand_line = lines[idx]
                    role_line = lines[idx + 1]
                    if not role_re.search(role_line):
                        continue
                    cleaned = self.clean_rep_name(cand_line)
                    if cleaned and self._looks_like_person_name(cleaned):
                        reps.append(cleaned)
                        break

        listings: List[str] = []
        capitals: List[str] = []
        revenues: List[str] = []
        profits: List[str] = []
        fiscal_months: List[str] = []
        founded_years: List[str] = []

        soup = None
        pair_values: List[tuple[str, str, bool]] = []  # (label, value, is_table_pair)
        sequential_texts: List[str] = []

        if html:
            try:
                soup = BeautifulSoup(html, "html.parser")
            except Exception:
                soup = None
            if soup:
                # フッター/隅の情報に住所だけが載っているケース対策:
                # table/dl のラベル抽出に乗らない住所を <footer>/<address> 等から拾う
                try:
                    extra_blobs: list[str] = []
                    # header/footer は本文ノイズになりやすいので text には落とさないが、
                    # 代表電話や所在地がここにしか出ないサイトが多いため、候補抽出では参照する
                    for node in soup.find_all(["address", "footer", "header"]):
                        txt = node.get_text(separator=" ", strip=True)
                        if txt:
                            extra_blobs.append(txt)
                    for role in ("contentinfo", "banner"):
                        for node in soup.find_all(attrs={"role": role}):
                            txt = node.get_text(separator=" ", strip=True)
                            if txt:
                                extra_blobs.append(txt)
                    # microdata (itemprop) のPostalAddress断片も拾う
                    parts: list[str] = []
                    for prop in ("postalCode", "addressRegion", "addressLocality", "streetAddress"):
                        for node in soup.find_all(attrs={"itemprop": prop}):
                            txt = node.get_text(separator=" ", strip=True)
                            if txt:
                                parts.append(txt)
                    if parts:
                        extra_blobs.append(" ".join(parts))

                    # tel: リンクは本文から落ちやすいので、href からも拾う
                    try:
                        for a in soup.find_all("a", href=True):
                            href = (a.get("href") or "").strip()
                            if not href:
                                continue
                            if not href.lower().startswith("tel:"):
                                continue
                            cand = _normalize_phone_strict(href)
                            if cand:
                                phones.append(f"[TEL]{cand}")
                    except Exception:
                        pass

                    # alt/title/aria-label 等に電話が載るケースを拾う（画像ボタン等）
                    try:
                        for node in soup.find_all(["a", "img", "button"]):
                            for attr in ("alt", "title", "aria-label"):
                                val = node.get(attr)
                                if not isinstance(val, str) or not val:
                                    continue
                                for p in PHONE_RE.finditer(val):
                                    cand = _normalize_phone_strict(p.group(0))
                                    if cand:
                                        phones.append(cand)
                    except Exception:
                        pass

                    for blob in extra_blobs:
                        for p in PHONE_RE.finditer(blob or ""):
                            cand = _normalize_phone_strict(p.group(0))
                            if not cand:
                                continue
                            ctx = (blob or "")[max(0, p.start() - 12):p.start()]
                            fax_hint = bool(re.search(r"(FAX|Fax|fax|ファックス|ﾌｧｯｸｽ|ＦＡＸ)", ctx))
                            tel_hint = bool(re.search(r"(TEL|Tel|tel|電話)", ctx))
                            if fax_hint and not tel_hint:
                                phones.append(f"[FAX]{cand}")
                            else:
                                phones.append(cand)
                        for zm in ZIP_RE.finditer(blob or ""):
                            zip_code = f"〒{zm.group(1).replace('〒', '').strip()}-{zm.group(2)}"
                            cursor = zm.end()
                            snippet = (blob or "")[cursor:cursor + 220]
                            snippet = snippet.replace("\n", " ").replace("\r", " ").replace("\u3000", " ")
                            snippet = re.split(
                                r"(地図アプリ|地図で見る|マップ|Google\s*マップ|地図|map|アクセス|ルート|拡大地図|gac?\.push|gtag|_gaq)",
                                snippet,
                                maxsplit=1,
                            )[0]
                            if ADDR_HINT.search(snippet):
                                cleaned = re.split(r"[。．、,，;；｜|/]", snippet, maxsplit=1)[0]
                                cleaned = re.sub(r"\s+", " ", cleaned)
                                seg = " ".join(cleaned.split()[:10]).strip()
                                if seg:
                                    norm_addr = self._normalize_address_candidate(f"{zip_code} {seg}")
                                    if norm_addr and self._looks_like_full_address(norm_addr):
                                        addrs.append(f"[FOOTER]{norm_addr}")
                        for cand in ADDR_FALLBACK_RE.findall(blob or ""):
                            norm = self._normalize_address_candidate(cand)
                            if norm and self._looks_like_full_address(norm):
                                addrs.append(f"[FOOTER]{norm}")
                except Exception:
                    pass
                for table in soup.find_all("table"):
                    for row in table.find_all("tr"):
                        cells = row.find_all(["th", "td"])
                        if len(cells) < 2:
                            continue
                        label = cells[0].get_text(separator=" ", strip=True)
                        value = cells[1].get_text(separator=" ", strip=True)
                        if len(cells) >= 3:
                            sep = cells[1].get_text(separator=" ", strip=True)
                            third = cells[2].get_text(separator=" ", strip=True)
                            sep_norm = sep.replace("\uff1a", ":").strip()
                            if third and (sep_norm in {":", ""}):
                                value = third
                        if label and value:
                            # 代表者ラベルは優先度を上げるため先頭に積む
                            if any(_label_matches(label, rep_kw) for rep_kw in TABLE_LABEL_MAP["rep_names"]):
                                pair_values.insert(0, (label, value, True))
                            else:
                                pair_values.append((label, value, True))

                for dl in soup.find_all("dl"):
                    dts = dl.find_all("dt")
                    dds = dl.find_all("dd")
                    for dt, dd in zip(dts, dds):
                        label = dt.get_text(separator=" ", strip=True)
                        value = dd.get_text(separator=" ", strip=True)
                        if label and value:
                            if any(_label_matches(label, rep_kw) for rep_kw in TABLE_LABEL_MAP["rep_names"]):
                                pair_values.insert(0, (label, value, True))
                            else:
                                pair_values.append((label, value, True))

                try:
                    def _is_nav_like_node(node: Any) -> bool:
                        try:
                            current = node
                            while current is not None:
                                name = (current.name or "").lower()
                                if name in {"nav", "header"}:
                                    return True
                                role = (current.get("role") or "").lower()
                                if role in {"navigation", "menubar"}:
                                    return True
                                classes = " ".join(current.get("class") or []).lower()
                                if re.search(r"\b(nav|menu|breadcrumb|gnav|global-nav)\b", classes):
                                    return True
                                current = current.parent
                        except Exception:
                            return False
                        return False

                    for block in soup.find_all(["p", "li", "span", "div"]):
                        if _is_nav_like_node(block):
                            continue
                        text = block.get_text(separator=" ", strip=True)
                        text = text.replace("\u200b", "")
                        text = re.sub(r"\s+", " ", text)
                        if not text:
                            continue
                        if "<" in text or "class=" in text or "svg" in text:
                            continue
                        if any(k in text.lower() for k in ("menu", "nav", "sitemap", "breadcrumb")):
                            continue
                        sequential_texts.append(text)
                except Exception:
                    sequential_texts = []

        if not sequential_texts:
            sequential_texts = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


        role_terms = (
            "(?:\u4ee3\u8868\\s*\u53d6\u7de0\u5f79(?:\u793e\u9577|\u4f1a\u9577|\u526f\u4f1a\u9577|\u526f\u793e\u9577)?"
            "|\u4ee3\u8868\\s*\u57f7\u884c\u5f79\u793e\u9577"
            "|\u4ee3\u8868\\s*\u57f7\u884c\u5f79"
            "|\u4ee3\u8868\\s*\u7406\u4e8b\u9577"
            "|\u4ee3\u8868\\s*\u7406\u4e8b"
            "|\u4ee3\u8868\\s*\u793e\u54e1"
            "|\u4ee3\u8868\u8005|\u4ee3\u8868|\u793e\u9577|\u4f1a\u9577|CEO)"
        )
        role_name_re = re.compile(
            f"({role_terms})"
            "(?:\\s*(?:\u57f7\u884c\u5f79(?:\u54e1)?|\u793e\u9577\u57f7\u884c\u5f79(?:\u54e1)?))?"
            "[\\s\u3000:\uff1a\u30fb]{0,4}"
            "([\u4e00-\u9fa5]{1,3}(?:[\u30fb\uff65\\s\u3000]{0,1}[\u4e00-\u9fa5]{1,3})+?)"
            f"(?=(?:[\\s\u3000:\uff1a\u30fb]*{role_terms}|$))"
        )
        if self.rep_strict_sources and sequential_texts:
            added_roles = 0
            for line in sequential_texts:
                for m in role_name_re.finditer(line):
                    role = re.sub(r"\s+", "", m.group(1))
                    name = m.group(2)
                    cleaned = self.clean_rep_name(name) or self.clean_rep_name(f"{role} {name}")
                    if cleaned and self._looks_like_person_name(cleaned):
                        reps.append(f"[ROLE]{cleaned}")
                        added_roles += 1
                        if added_roles >= 2:
                            break
                if added_roles >= 2:
                    break

        def _looks_like_label(text: str) -> tuple[bool, str]:
            if not text:
                return False, ""
            cleaned, compact = _normalize_label_text(text)
            if not cleaned:
                return False, ""
            if len(cleaned) > 20 and (not compact or len(compact) > 20):
                return False, ""
            for keywords in TABLE_LABEL_MAP.values():
                if any(_label_matches(cleaned, kw) for kw in keywords):
                    return True, cleaned
            return False, ""

        # 1行内のラベル:値（コロン/スペース区切り）を優先して抽出
        for text in sequential_texts:
            cleaned_line = text.replace("\u200b", "").strip()
            if not cleaned_line or len(cleaned_line) > 80:
                continue
            m = re.match(r"^(.{1,20}?)[\s：:・…]+(.{1,80})$", cleaned_line)
            if not m:
                continue
            label_candidate = m.group(1).strip()
            value_candidate = m.group(2).strip()
            is_label, normalized_label = _looks_like_label(label_candidate)
            if not is_label:
                continue
            if not value_candidate or len(value_candidate) > 120:
                continue
            pair_values.append((normalized_label, value_candidate, False))

        # 長い1行に複数ラベルが並ぶケースを分割して抽出
        for text in sequential_texts:
            cleaned_line = text.replace("\u200b", "").strip()
            if not cleaned_line or len(cleaned_line) < 120:
                continue
            tokens = [t for t in cleaned_line.split(" ") if t]
            if len(tokens) < 6:
                continue
            label_positions: list[tuple[int, str]] = []
            for idx, token in enumerate(tokens):
                is_label, normalized_label = _looks_like_label(token)
                if is_label:
                    label_positions.append((idx, normalized_label))
            if len(label_positions) < 2:
                continue
            label_positions.append((len(tokens), ""))
            for pos_idx in range(len(label_positions) - 1):
                label_idx, normalized_label = label_positions[pos_idx]
                next_idx, _ = label_positions[pos_idx + 1]
                if not normalized_label:
                    continue
                field = _field_for_label(normalized_label)
                if not field:
                    continue
                value_text = " ".join(tokens[label_idx + 1:next_idx]).strip()
                if not value_text:
                    continue
                max_len = 160 if field == "addresses" else 120
                if len(value_text) > max_len:
                    continue
                if not _is_value_for_field(field, value_text):
                    continue
                pair_values.append((normalized_label, value_text, False))

        for idx in range(len(sequential_texts) - 1):
            is_label, normalized = _looks_like_label(sequential_texts[idx])
            if not is_label:
                continue
            value_text = ""
            next_idx = idx + 1
            if next_idx < len(sequential_texts):
                candidate = sequential_texts[next_idx].replace("\u200b", "").strip()
                if candidate and not candidate.startswith("・"):
                    looks_like_next, _ = _looks_like_label(candidate)
                    if not looks_like_next:
                        value_text = candidate
            if not value_text:
                field = _field_for_label(normalized)
                if field:
                    max_offset_default = 4
                    if field in {"rep_names", "addresses"}:
                        max_offset_default = 6
                    max_offset = min(max_offset_default, len(sequential_texts) - idx - 1)
                    for offset in range(1, max_offset + 1):
                        candidate = sequential_texts[idx + offset].replace("\u200b", "").strip()
                        if not candidate or candidate.startswith("・"):
                            continue
                        looks_like_next, _ = _looks_like_label(candidate)
                        if looks_like_next:
                            continue
                        if field == "addresses":
                            if _is_value_for_field(field, candidate):
                                value_text = candidate
                                break
                            if ZIP_RE.search(candidate):
                                next_line = ""
                                if idx + offset + 1 < len(sequential_texts):
                                    next_line = sequential_texts[idx + offset + 1].replace("\u200b", "").strip()
                                if next_line and not _looks_like_label(next_line)[0]:
                                    combined = f"{candidate} {next_line}".strip()
                                    if _is_value_for_field(field, combined):
                                        value_text = combined
                                        break
                            continue
                        if _is_value_for_field(field, candidate):
                            value_text = candidate
                            break
            if not value_text or len(value_text) > 120:
                continue
            pair_values.append((normalized, value_text, False))

        # 役職→氏名の並びではなく、氏名→役職の並び（カードUI等）を構造として扱う
        if self.rep_strict_sources and sequential_texts:
            role_re = re.compile(
                r"(代表取締役社長|代表執行役社長|代表取締役会長|代表取締役|代表理事長|代表理事|代表社員|代表者|代表|社長|会長|CEO)",
                flags=re.I,
            )
            for idx in range(len(sequential_texts) - 1):
                name_line = sequential_texts[idx].replace("\u200b", "").strip()
                role_line = sequential_texts[idx + 1].replace("\u200b", "").strip()
                if not name_line or not role_line:
                    continue
                _, role_compact = _normalize_label_text(role_line)
                if not (role_re.search(role_line) or (role_compact and role_re.search(role_compact))):
                    continue
                cleaned = self.clean_rep_name(name_line)
                if cleaned and self._looks_like_person_name(cleaned):
                    pair_values.append((role_line, name_line, False))
                    break

        for label, value, is_table_pair in pair_values:
            norm_label = label.replace("：", ":").strip()
            _, compact_label = _normalize_label_text(norm_label)
            raw_value = self._clean_text_value(value)
            if not raw_value:
                continue
            if (
                ("顧問" in norm_label or "顧問" in compact_label)
                or ("弁護士" in norm_label or "弁護士" in compact_label)
                or ("社労士" in norm_label or "社労士" in compact_label)
            ):
                continue
            # ニュース/人事系のラベルはスキップ
            label_block = ("退任", "就任", "人事", "異動", "お知らせ", "ニュース", "採用")
            if any((b in norm_label) or (b in compact_label) for b in label_block):
                continue
            matched = False
            for field, keywords in TABLE_LABEL_MAP.items():
                if any(_label_matches(norm_label, keyword) for keyword in keywords):
                    if field == "rep_names":
                        def _pick_best_rep_name(val: str) -> tuple[str | None, bool]:
                            if not val:
                                return None, False
                            if len(val) > 60 or re.search(r"[。．!?]", val) or "、" in val:
                                return None, False
                            # 複数列挙（例: 代表取締役会長A、代表取締役社長B）から強い役職を優先して拾う
                            role_priority = {
                                "\u4ee3\u8868\u53d6\u7de0\u5f79\u793e\u9577": 100,
                                "\u4ee3\u8868\u53d6\u7de0\u5f79\u4f1a\u9577": 96,
                                "\u4ee3\u8868\u53d6\u7de0\u5f79\u526f\u4f1a\u9577": 92,
                                "\u4ee3\u8868\u53d6\u7de0\u5f79\u526f\u793e\u9577": 88,
                                "CEO": 95,
                                "\u4ee3\u8868\u57f7\u884c\u5f79\u793e\u9577": 90,
                                "\u4ee3\u8868\u53d6\u7de0\u5f79": 85,
                                "\u4ee3\u8868\u7406\u4e8b\u9577": 80,
                                "\u4ee3\u8868\u7406\u4e8b": 75,
                                "\u793e\u9577": 70,
                                "\u4f1a\u9577": 55,
                                "\u4ee3\u8868\u793e\u54e1": 50,
                                "\u4ee3\u8868\u8005": 45,
                                "\u4ee3\u8868": 40,
                            }
                            best: tuple[int, str] | None = None
                            for m in role_name_re.finditer(val):
                                role = re.sub(r"\s+", "", m.group(1))
                                name = m.group(2)
                                cleaned_name = self.clean_rep_name(name) or self.clean_rep_name(f"{role} {name}")
                                if not cleaned_name or not self._looks_like_person_name(cleaned_name):
                                    continue
                                prio = int(role_priority.get(role, 0))
                                if prio == 0 and "代表取締役" in role:
                                    if "副会長" in role:
                                        prio = 92
                                    elif "副社長" in role:
                                        prio = 88
                                    elif "社長" in role:
                                        prio = 100
                                    elif "会長" in role:
                                        prio = 96
                                    else:
                                        prio = 85
                                elif prio == 0 and role == "代表":
                                    prio = 40
                                cand = (prio, cleaned_name)
                                if best is None or cand[0] > best[0]:
                                    best = cand
                            if best is not None:
                                strong = best[0] >= 70
                                return best[1], strong
                            cleaned = self.clean_rep_name(val)
                            if cleaned and self._looks_like_person_name(cleaned):
                                strong = bool(re.search(r"(代表取締役|代表執行役|代表理事|社長|CEO)", val))
                                return cleaned, strong
                            return None, False

                        cleaned, strong_role = _pick_best_rep_name(raw_value)
                        if cleaned:
                            if (not strong_role) and (
                                ("役員" in norm_label)
                                or any(term in norm_label for term in ("一覧", "組織図", "メッセージ", "挨拶", "紹介"))
                            ):
                                matched = True
                                continue
                            prefix = "[TABLE]" if is_table_pair else "[LABEL]"
                            normalized_rep = f"{prefix}{cleaned}"
                            label_reps.append(normalized_rep)
                            reps.append(normalized_rep)
                            rep_from_label = True
                            matched = True
                    elif field == "phone_numbers":
                        label_text = unicodedata.normalize("NFKC", norm_label or "")
                        label_has_tel = bool(re.search(r"(TEL|電話)", label_text, re.I))
                        label_has_fax = bool(re.search(r"(FAX|ファックス|ﾌｧｯｸｽ)", label_text, re.I))
                        if label_has_fax and not label_has_tel:
                            # FAX専用行は代表電話候補として扱わない
                            continue
                        value_nfkc = unicodedata.normalize("NFKC", raw_value)
                        # 値側に "FAX" が混在するケースが多いので、ラベルにFAXが無くても分割して捨てられるようにする
                        fax_pos = re.search(r"(FAX|ファックス|ﾌｧｯｸｽ)", value_nfkc, re.I)
                        tel_part = value_nfkc[: fax_pos.start()] if fax_pos else value_nfkc
                        fax_part = value_nfkc[fax_pos.start():] if fax_pos else ""
                        def _emit(part: str, is_fax: bool) -> None:
                            nonlocal matched
                            for p in PHONE_RE.finditer(part or ""):
                                cand = _normalize_phone_strict(f"{p.group(1)}-{p.group(2)}-{p.group(3)}")
                                if not cand:
                                    continue
                                prefix = ""
                                if is_table_pair:
                                    prefix += "[TABLE]"
                                if is_fax:
                                    prefix += "[FAX]"
                                phones.append(f"{prefix}{cand}")
                                matched = True
                        _emit(tel_part, False)
                        if fax_part:
                            _emit(fax_part, True)
                    elif field == "addresses":
                        norm_addr = self._normalize_address_candidate(raw_value)
                        if norm_addr and self.looks_like_address(norm_addr):
                            prefix = "[TABLE]" if is_table_pair else "[LABEL]"
                            hq_labels = ("本社", "本店", "本社所在地", "本店所在地", "所在地(本社)")
                            value_nfkc = unicodedata.normalize("NFKC", raw_value)
                            if any(h in norm_label for h in hq_labels) or bool(re.search(r"(本社|本店)\s*[:：]?", value_nfkc)):
                                prefix += "[HQ]"
                            addrs.append(f"{prefix}{norm_addr}")
                            matched = True
                    elif field == "listing":
                        listings.append(raw_value if not is_table_pair else f"[TABLE]{raw_value}")
                        matched = True
                    elif field == "capitals":
                        if self._is_amount_like(raw_value):
                            capitals.append(raw_value if not is_table_pair else f"[TABLE]{raw_value}")
                            matched = True
                    elif field == "revenues":
                        if self._is_amount_like(raw_value):
                            revenues.append(raw_value if not is_table_pair else f"[TABLE]{raw_value}")
                            matched = True
                    elif field == "profits":
                        if self._is_amount_like(raw_value):
                            profits.append(raw_value if not is_table_pair else f"[TABLE]{raw_value}")
                            matched = True
                    elif field == "fiscal_months":
                        fiscal_months.append(raw_value if not is_table_pair else f"[TABLE]{raw_value}")
                        matched = True
                    elif field == "founded_years":
                        founded_years.append(raw_value if not is_table_pair else f"[TABLE]{raw_value}")
                        matched = True
            if matched:
                continue
            if not matched and (self._is_exec_title(norm_label) or self._is_exec_title(compact_label)):
                cleaned = self.clean_rep_name(raw_value)
                if cleaned and not rep_from_label and self._looks_like_person_name(cleaned):
                    normalized_rep = f"[LABEL]{cleaned}"
                    label_reps.append(normalized_rep)
                    reps.append(normalized_rep)
                    rep_from_label = True

        if soup:
            def walk_ld(entity: Any) -> None:
                if isinstance(entity, dict):
                    types = entity.get("@type") or entity.get("type")
                    type_list = []
                    if isinstance(types, str):
                        type_list = [types.lower()]
                    elif isinstance(types, list):
                        type_list = [str(t).lower() for t in types]
                    is_org = any(t in {
                        "organization", "localbusiness", "corporation", "educationalorganization",
                        "ngo", "governmentoffice", "medicalorganization", "hotel", "lodgingbusiness",
                    } for t in type_list)
                    if is_org:
                        tel = entity.get("telephone")
                        if isinstance(tel, str):
                            for p in PHONE_RE.finditer(tel):
                                cand = _normalize_phone_strict(f"{p.group(1)}-{p.group(2)}-{p.group(3)}")
                                if cand:
                                    phones.append(cand)
                        contact_points = entity.get("contactPoint") or entity.get("contactPoints") or []
                        if isinstance(contact_points, dict):
                            contact_points = [contact_points]
                        if isinstance(contact_points, list):
                            for cp in contact_points:
                                if not isinstance(cp, dict):
                                    continue
                                cp_tel = cp.get("telephone")
                                if isinstance(cp_tel, str):
                                    for p in PHONE_RE.finditer(cp_tel):
                                        cand = _normalize_phone_strict(f"{p.group(1)}-{p.group(2)}-{p.group(3)}")
                                        if cand:
                                            phones.append(cand)
                        addr = entity.get("address")
                        if isinstance(addr, dict):
                            parts = [addr.get(k, "") for k in ("postalCode", "addressRegion", "addressLocality", "streetAddress")]
                            joined = " ".join([p for p in parts if p])
                            if joined:
                                norm_addr = self._normalize_address_candidate(joined)
                                if norm_addr:
                                    addrs.append(f"[JSONLD]{norm_addr}")
                        founder = entity.get("founder")
                        founders = entity.get("founders")
                        founder_vals: List[str] = []
                        if isinstance(founder, str):
                            founder_vals.append(founder)
                        elif isinstance(founder, dict):
                            name = founder.get("name")
                            if isinstance(name, str):
                                founder_vals.append(name)
                        if isinstance(founders, list):
                            for f in founders:
                                if isinstance(f, str):
                                    founder_vals.append(f)
                                elif isinstance(f, dict):
                                    name = f.get("name")
                                    if isinstance(name, str):
                                        founder_vals.append(name)
                        for name in founder_vals:
                            cleaned = self.clean_rep_name(name)
                            if cleaned and not rep_from_label and self._looks_like_person_name(cleaned):
                                reps.append(f"[JSONLD]{cleaned}")

                        founding_date = entity.get("foundingDate") or entity.get("foundingYear")
                        if isinstance(founding_date, str):
                            m = re.search(r"(\d{4})", founding_date)
                            if m:
                                founded_years.append(m.group(1))
                    for v in entity.values():
                        walk_ld(v)
                elif isinstance(entity, list):
                    for item in entity:
                        walk_ld(item)

            for script in soup.find_all("script"):
                t = script.get("type", "") or ""
                if "ld+json" not in t and "@context" not in (script.string or ""):
                    continue
                try:
                    data = json.loads(script.string or "")
                except Exception:
                    continue
                walk_ld(data)

        # 住所は「構造抽出」を優先し、強い候補（JSON-LD/テーブル/ラベル）が無いときのみテキスト拾いを許可する
        if not addrs:
            _extract_addrs_from_text()
        else:
            has_strong = any(
                isinstance(a, str) and (a.startswith("[JSONLD]") or a.startswith("[TABLE]") or a.startswith("[LABEL]"))
                for a in addrs
            )
            if not has_strong:
                _extract_addrs_from_text()

        for lm in LISTING_RE.finditer(text or ""):
            val = lm.group(1).strip()
            val = re.split(r"[、。\s/|]", val)[0]
            if val:
                listings.append(val)
        for sm in SECURITIES_CODE_RE.finditer(text or ""):
            code = sm.group(1).strip()
            if code:
                listings.append(f"証券コード{code}")
        for mm in MARKET_CODE_RE.finditer(text or ""):
            code = mm.group(1).strip()
            if code:
                listings.append(f"証券コード{code}")
        if not listings:
            lowered = (text or "").lower()
            for term in LISTING_KEYWORDS:
                if term in text or term.lower() in lowered:
                    listings.append(term)
                    break

        if not self.rep_strict_sources:
            for m in REP_RE.finditer(text or ""):
                cand = self.clean_rep_name(m.group(1))
                if cand and not rep_from_label and self._looks_like_person_name(cand):
                    reps.append(cand)

        capitals.extend(m.group(1).strip() for m in CAPITAL_RE.finditer(text or ""))
        revenues.extend(m.group(1).strip() for m in REVENUE_RE.finditer(text or ""))
        profits.extend(m.group(1).strip() for m in PROFIT_RE.finditer(text or ""))
        fiscal_months.extend(m.group(1).strip() for m in FISCAL_RE.finditer(text or ""))
        for fm in FOUNDED_RE.finditer(text or ""):
            val = fm.group(1).strip()
            parsed = self._parse_founded_year(val)
            if parsed:
                founded_years.append(parsed)

        def dedupe(seq: List[str]) -> List[str]:
            seen: set[str] = set()
            out: List[str] = []
            for item in seq:
                if item and item not in seen:
                    seen.add(item)
                    out.append(item)
            return out

        # ラベル由来の代表者があればそれを優先
        if label_reps:
            reps = label_reps + [r for r in reps if "[JSONLD]" in r]

        if self.rep_strict_sources:
            reps = [
                r for r in reps
                if any(tag in r for tag in ("[TABLE]", "[LABEL]", "[JSONLD]", "[ROLE]"))
            ]

        return {
            "phone_numbers": dedupe(phones),
            "addresses": dedupe(addrs),
            "rep_names": dedupe(reps),
            "listings": dedupe(listings),
            "capitals": dedupe(capitals),
            "revenues": dedupe(revenues),
            "profits": dedupe(profits),
            "fiscal_months": dedupe(fiscal_months),
            "founded_years": dedupe(founded_years),
        }
