# main.py
import asyncio
import os
import csv
import logging
import json
import re
import random
import time
import html as html_mod
import threading
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict
from urllib.parse import urlparse
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:  # type: ignore
        return None
from bs4 import BeautifulSoup

from src.database_manager import DatabaseManager
from src.company_scraper import CompanyScraper, CITY_RE, NAME_CHUNK_RE, KANA_NAME_RE
from src.ai_verifier import (
    AIVerifier,
    DEFAULT_MODEL as AI_MODEL_NAME,
    AI_CALL_TIMEOUT_SEC,
    _normalize_amount as ai_normalize_amount,
)
from src.homepage_policy import apply_provisional_homepage_policy
from src.reference_checker import ReferenceChecker
from src.jp_number import normalize_kanji_numbers

class HardTimeout(Exception):
    """Raised when the per-company hard time limit is exceeded."""
    pass


class SkipCompany(Exception):
    """Raised to skip remaining processing for the current company."""
    pass

# --------------------------------------------------
# ロギング設定
# --------------------------------------------------
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# .env 読み込み
load_dotenv()

# --------------------------------------------------
# 実行オプション（.env）
# --------------------------------------------------
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
USE_AI = os.getenv("USE_AI", "true").lower() == "true"
# AI公式判定。デフォルトは候補URLを広く判定し、公式採用の最終判断をAI優先にする。
USE_AI_OFFICIAL = os.getenv("USE_AI_OFFICIAL", "true").lower() == "true"
# AI公式判定の対象範囲（true: 候補を広く / false: 従来どおり上位3件のみ）
AI_OFFICIAL_ALL_CANDIDATES = os.getenv("AI_OFFICIAL_ALL_CANDIDATES", "true").lower() == "true"
# AI公式判定が使える場合、公式採用の最終判断をAI優先にする
AI_OFFICIAL_PRIMARY = os.getenv("AI_OFFICIAL_PRIMARY", "true").lower() == "true"
# AI公式判定の候補数上限（0以下で無制限）。デフォルトは3件。
AI_OFFICIAL_CANDIDATE_LIMIT = int(os.getenv("AI_OFFICIAL_CANDIDATE_LIMIT", "3"))
# AI公式判定の同時実行数（API/モデル負荷対策）。デフォルトは3。
AI_OFFICIAL_CONCURRENCY = max(1, int(os.getenv("AI_OFFICIAL_CONCURRENCY", "3")))
# description はAIで常時生成（verify_infoで同時生成）。追加の説明専用AI呼び出しを有効にしたい場合のみ true。
USE_AI_DESCRIPTION = os.getenv("USE_AI_DESCRIPTION", "false").lower() == "true"
AI_FINAL_WITH_OFFICIAL = os.getenv("AI_FINAL_WITH_OFFICIAL", "false").lower() == "true"
WORKER_ID = os.getenv("WORKER_ID", "w1")  # 並列識別子
COMPANIES_DB_PATH = os.getenv("COMPANIES_DB_PATH", "data/companies.db")

MAX_ROWS = int(os.getenv("MAX_ROWS", "0"))
ID_MIN = int(os.getenv("ID_MIN", "0"))
ID_MAX = int(os.getenv("ID_MAX", "0"))
AI_COOLDOWN_SEC = float(os.getenv("AI_COOLDOWN_SEC", "0"))
SLEEP_BETWEEN_SEC = float(os.getenv("SLEEP_BETWEEN_SEC", "0"))
JITTER_RATIO = float(os.getenv("JITTER_RATIO", "0.30"))
REFERENCE_CSVS = [p.strip() for p in os.getenv("REFERENCE_CSVS", "").split(",") if p.strip()]
FETCH_CONCURRENCY = max(1, int(os.getenv("FETCH_CONCURRENCY", "3")))
PROFILE_FETCH_CONCURRENCY = max(1, int(os.getenv("PROFILE_FETCH_CONCURRENCY", "3")))
SEARCH_CANDIDATE_LIMIT = max(1, int(os.getenv("SEARCH_CANDIDATE_LIMIT", "3")))
# 検索フェーズ全体の早期タイムアウト（0で無効）
SEARCH_PHASE_TIMEOUT_SEC = float(os.getenv("SEARCH_PHASE_TIMEOUT_SEC", "30"))
# 深掘りページ/ホップを環境変数で制御（デフォルトは軽め）
RELATED_BASE_PAGES = max(0, int(os.getenv("RELATED_BASE_PAGES", "1")))
RELATED_EXTRA_PHONE = max(0, int(os.getenv("RELATED_EXTRA_PHONE", "1")))
RELATED_MAX_HOPS_BASE = max(1, int(os.getenv("RELATED_MAX_HOPS_BASE", "2")))
RELATED_MAX_HOPS_PHONE = max(1, int(os.getenv("RELATED_MAX_HOPS_PHONE", "2")))
# 全体のタイムアウトは使わず、フェーズ別で管理する（デフォルトを短めにし停滞を防止）
TIME_LIMIT_SEC = float(os.getenv("TIME_LIMIT_SEC", "60"))
TIME_LIMIT_FETCH_ONLY = float(os.getenv("TIME_LIMIT_FETCH_ONLY", "10"))  # 公式未確定で候補取得フェーズ（0で無効）
TIME_LIMIT_WITH_OFFICIAL = float(os.getenv("TIME_LIMIT_WITH_OFFICIAL", "40"))  # 公式確定後、主要項目未充足（0で無効）
TIME_LIMIT_DEEP = float(os.getenv("TIME_LIMIT_DEEP", "45"))  # 深掘り専用の上限（公式確定後）（0で無効）
# 会社ごとの絶対上限（この時間を超えたら部分保存して次へ）
ABSOLUTE_COMPANY_DEADLINE_SEC = float(os.getenv("ABSOLUTE_COMPANY_DEADLINE_SEC", "60"))
# 全体のハード上限（デフォルト60秒で次ジョブへスキップ）
GLOBAL_HARD_TIMEOUT_SEC = float(os.getenv("GLOBAL_HARD_TIMEOUT_SEC", "60"))
# 単社処理のハード上限（candidate取得で固まるのを避けるための保険、0で無効）
COMPANY_HARD_TIMEOUT_SEC = float(os.getenv("COMPANY_HARD_TIMEOUT_SEC", "60"))
DEEP_PHASE_TIMEOUT_SEC = float(os.getenv("DEEP_PHASE_TIMEOUT_SEC", "120"))
OFFICIAL_AI_USE_SCREENSHOT = os.getenv("OFFICIAL_AI_USE_SCREENSHOT", "true").lower() == "true"
# 住所はAIの出力も取り込みつつ、DB側で都道府県不一致の上書きを厳格に制限する
AI_ADDRESS_ENABLED = os.getenv("AI_ADDRESS_ENABLED", "true").lower() == "true"
SECOND_PASS_ENABLED = os.getenv("SECOND_PASS_ENABLED", "false").lower() == "true"
SECOND_PASS_RETRY_STATUSES = [s.strip() for s in os.getenv("SECOND_PASS_RETRY_STATUSES", "review,no_homepage,error").split(",") if s.strip()]
LONG_PAGE_TIMEOUT_MS = int(os.getenv("LONG_PAGE_TIMEOUT_MS", os.getenv("PAGE_TIMEOUT_MS", "9000")))
LONG_SLOW_PAGE_THRESHOLD_MS = int(os.getenv("LONG_SLOW_PAGE_THRESHOLD_MS", os.getenv("SLOW_PAGE_THRESHOLD_MS", "9000")))
LONG_TIME_LIMIT_FETCH_ONLY = float(os.getenv("LONG_TIME_LIMIT_FETCH_ONLY", os.getenv("TIME_LIMIT_FETCH_ONLY", "30")))
LONG_TIME_LIMIT_WITH_OFFICIAL = float(os.getenv("LONG_TIME_LIMIT_WITH_OFFICIAL", os.getenv("TIME_LIMIT_WITH_OFFICIAL", "45")))
LONG_TIME_LIMIT_DEEP = float(os.getenv("LONG_TIME_LIMIT_DEEP", os.getenv("TIME_LIMIT_DEEP", "60")))
AI_MIN_REMAINING_SEC = float(os.getenv("AI_MIN_REMAINING_SEC", "2.5"))
AI_VERIFY_MIN_CONFIDENCE = float(os.getenv("AI_VERIFY_MIN_CONFIDENCE", "0.65"))
# url_flags に保存された「AI非公式」判定を、候補URL取得前に強制スキップするための閾値。
# 低confidenceのAI判定は誤爆しやすいので、一定以上のみハードに扱う（それ未満は再評価対象）。
AI_SKIP_NEGATIVE_FLAG_MIN_CONFIDENCE = float(os.getenv("AI_SKIP_NEGATIVE_FLAG_MIN_CONFIDENCE", "0.85"))
AI_CLEAR_NEGATIVE_FLAGS = os.getenv("AI_CLEAR_NEGATIVE_FLAGS", "false").lower() == "true"

# 暫定URL（provisional_*）を homepage として保存するかどうか。
# - false の場合でも provisional_homepage/final_homepage には記録する。
SAVE_PROVISIONAL_HOMEPAGE = os.getenv("SAVE_PROVISIONAL_HOMEPAGE", "true").lower() == "true"
DEFAULT_TIME_LIMIT_FETCH_ONLY = TIME_LIMIT_FETCH_ONLY
DEFAULT_TIME_LIMIT_WITH_OFFICIAL = TIME_LIMIT_WITH_OFFICIAL
DEFAULT_TIME_LIMIT_DEEP = TIME_LIMIT_DEEP

MIRROR_TO_CSV = os.getenv("MIRROR_TO_CSV", "false").lower() == "true"
OUTPUT_CSV_PATH = os.getenv("OUTPUT_CSV_PATH", "data/output.csv")
CSV_FIELDNAMES = [
    "id", "company_name", "address", "employee_count",
    "homepage", "phone", "found_address", "rep_name", "description",
    "listing", "revenue", "profit", "capital", "fiscal_month", "founded_year"
]
PHASE_METRICS_PATH = os.getenv("PHASE_METRICS_PATH", "logs/phase_metrics.csv")
NO_OFFICIAL_LOG_PATH = os.getenv("NO_OFFICIAL_LOG_PATH", "logs/no_official.jsonl")

REFERENCE_CHECKER: ReferenceChecker | None = None
if REFERENCE_CSVS:
    try:
        REFERENCE_CHECKER = ReferenceChecker.from_csvs(REFERENCE_CSVS)
        log.info("Reference data loaded: %s rows", len(REFERENCE_CHECKER))
    except Exception:
        log.exception("Reference data loading failed")

ZIP_CODE_RE = re.compile(r"(\d{3}-\d{4})")
JAPANESE_RE = re.compile(r"[ぁ-んァ-ン一-龥]")
MOJIBAKE_LATIN_RE = re.compile(r"[ÃÂãâæçïðñöøûüÿ]")
ADDRESS_JS_NOISE_RE = re.compile(
    r"(window\.\w+|dataLayer\s*=|gtm\.|googletagmanager|nr-data\.net|newrelic|bam\.nr-data\.net|function\s*\(|<script|</script>)",
    re.IGNORECASE,
)
ADDRESS_FORM_NOISE_RE = re.compile(
    # フォーム由来のラベル/説明文だけを狙う（単語の素朴な出現は弾かない）
    r"("
    r"住所検索|郵便番号\s*[（(]?\s*半角|マンション・?ビル名|市区町村・番地|"
    r"都道府県\b|都道府県\s*(?:選択|入力|[:：])|市区町村\s*(?:選択|入力|[:：])|"
    r"住所\s*(?:を)?\s*(?:入力|選択)\b|番地\s*(?:を)?\s*入力|建物(?:名)?\s*(?:を)?\s*入力|"
    r"(?:必須|入力してください|例[:：]|記入例)"
    r")",
    re.IGNORECASE,
)
KANJI_TOKEN_RE = re.compile(r"[一-龥]{2,}")
LISTING_ALLOWED_KEYWORDS = [
    "上場", "未上場", "非上場", "東証", "名証", "札証", "福証", "JASDAQ",
    "TOKYO PRO", "マザーズ", "グロース", "スタンダード", "プライム",
    "Nasdaq", "NYSE"
]
AMOUNT_ALLOWED_UNITS = ("億円", "万円", "千円", "円")
DESCRIPTION_HINTS = (
    "会社概要", "法人概要", "団体概要", "組合概要", "企業情報", "基本情報",
    "事業内容", "事業紹介", "沿革", "理念", "ごあいさつ", "ご挨拶",
    "私たちについて", "about", "会社紹介", "法人紹介", "概要"
)
GENERIC_DESCRIPTION_TERMS = {
    "会社概要", "企業情報", "事業概要", "法人概要", "団体概要",
    "トップメッセージ", "ご挨拶", "メッセージ", "沿革", "理念",
}
DESCRIPTION_MIN_LEN = max(6, int(os.getenv("DESCRIPTION_MIN_LEN", "10")))
DESCRIPTION_MAX_LEN = max(DESCRIPTION_MIN_LEN, int(os.getenv("DESCRIPTION_MAX_LEN", "200")))
DESCRIPTION_BIZ_KEYWORDS = (
    "事業", "製造", "開発", "販売", "提供", "サービス", "運営", "支援", "施工", "設計", "製作",
    "物流", "建設", "工事", "コンサル", "consulting", "solution", "ソリューション",
    "product", "製品", "プロダクト", "システム", "プラント", "加工", "レンタル", "運送",
    "IT", "デジタル", "ITソリューション", "プロジェクト", "アウトソーシング", "研究", "技術",
    "人材", "教育", "ヘルスケア", "医療", "食品", "エネルギー", "不動産", "金融", "EC", "通販",
    "プラットフォーム", "クラウド", "SaaS", "DX", "AI", "データ分析", "セキュリティ", "インフラ",
    "基盤", "ソフトウェア", "ハードウェア", "ロボット", "IoT", "モビリティ", "物流DX",
)

BUSINESS_LABELS = (
    "事業内容",
    "業務内容",
    "事業概要",
    "事業案内",
    "事業紹介",
    "主な事業",
    "主要事業",
    "事業領域",
    "事業分野",
    "サービス内容",
    "業種",
    "業態",
    "取扱品目",
    "主要取扱品目",
    "営業品目",
)
BUSINESS_LABEL_RE = re.compile("|".join(re.escape(k) for k in BUSINESS_LABELS))
BUSINESS_LABEL_EXCLUDE_RE = re.compile(r"(事業所|事業部|事業課|事業計画)")
BUSINESS_VALUE_PLACEHOLDERS = {
    "-",
    "ー",
    "―",
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
BUSINESS_VALUE_CUT_RE = re.compile(
    r"(お問い合わせ|お問合せ|問合せ|採用情報|求人情報|TEL|電話|FAX|メール|E-mail|住所|所在地|https?://|@)",
    re.IGNORECASE,
)

def looks_mojibake(text: str | None) -> bool:
    if not text:
        return False
    if "\ufffd" in text:
        return True
    s = str(text)
    if JAPANESE_RE.search(s):
        # Japanese is present; only reject if obvious mojibake markers are also present.
        return bool(MOJIBAKE_LATIN_RE.search(s)) and s.count("\ufffd") >= 1
    latin_count = sum(1 for ch in s if "\u00c0" <= ch <= "\u00ff")
    if latin_count >= 3 and latin_count / max(len(s), 1) >= 0.15:
        return True
    return bool(MOJIBAKE_LATIN_RE.search(s) and latin_count >= 2)

# --------------------------------------------------
# 正規化 & 一致判定
# --------------------------------------------------
def normalize_phone(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"(内線|ext|extension)\s*[:：]?\s*\d+$", "", s, flags=re.I)
    # ハイフン類を統一
    s = re.sub(r"[‐―－ー–—]+", "-", s)
    # 文字列上の区切りがある場合は、それを優先して分割（03-1234-5678 等の誤分割を防ぐ）
    m_sep = re.search(r"(0\d{1,4})\D+?(\d{1,4})\D+?(\d{3,4})", s)
    if m_sep:
        return f"{m_sep.group(1)}-{m_sep.group(2)}-{m_sep.group(3)}"
    digits = re.sub(r"\D", "", s)
    if digits.startswith("81") and len(digits) >= 10:
        digits = "0" + digits[2:]
    # 国内番号は0始まりで10〜11桁のみ許容
    if not digits.startswith("0") or len(digits) not in (10, 11):
        return None
    m = re.search(r"^(0\d{1,4})(\d{2,4})(\d{3,4})$", digits)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m2 = re.search(r"^(0\d{1,4})-?(\d{2,4})-?(\d{3,4})$", s)
    return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}" if m2 else None

def clean_homepage_url(url: str | None) -> str:
    if not url:
        return ""
    raw = re.sub(r"\s+", "", str(url))
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("mailto:", "tel:", "javascript:")):
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme:
        candidate = f"https://{raw}"
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.netloc:
            raw = candidate
        else:
            return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.netloc:
        return ""
    return raw.split("#", 1)[0]

def normalize_address(s: str | None) -> str | None:
    if not s:
        return None
    if looks_mojibake(s):
        return None
    def _strip_address_label(text: str) -> str:
        out = text.strip()
        for _ in range(3):
            out2 = re.sub(r"^(?:【\s*)?(?:本社|本店)?(?:所在地|住所)(?:】\s*)?\s*[:：]?\s*", "", out)
            if out2 == out:
                break
            out = out2.strip()
        return out

    def _cut_trailing_non_address(text: str) -> str:
        out = text
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
        m_tail = tail_re.search(out)
        if m_tail:
            out = out[: m_tail.start()].strip()
        out = out.strip(" 　\t,，;；。．|｜/／・-‐―－ー:：")
        return out
    s = s.strip().replace("　", " ")
    s = re.sub(r"<[^>]+>", " ", s)
    # タグが壊れている/途中で切れている場合の残骸を軽く除去（div/nav 等）
    s = s.replace("<", " ").replace(">", " ")
    s = re.sub(r"\b(?:div|nav|footer|header|main|section|article|span|ul|li|br|href|class|id|style)\b", " ", s, flags=re.I)
    s = re.sub(r"=\s*(?:\"[^\"]*\"|'[^']*'|\\\"[^\\\"]*\\\")", " ", s)
    s = re.sub(r"\s*=\s*", " ", s)
    # CSSスタイル断片の除去（スクレイプ時に混入する background: などを落とす）
    s = re.sub(r"(background|color|font-family|font-size|display|position)\s*:\s*[^;]+;?", " ", s, flags=re.I)
    # JSやトラッキング断片をカット（window.dataLayer 等が混入するケース対策）
    m_noise = ADDRESS_JS_NOISE_RE.search(s)
    if m_noise:
        s = s[: m_noise.start()]
    # 連絡先や地図系キーワードが混入した場合はそれ以降をカット
    contact_pattern = re.compile(r"(TEL|電話|☎|℡|FAX|ファックス|メール|E[-\s]?mail)", re.IGNORECASE)
    contact_match = contact_pattern.search(s)
    if contact_match:
        s = s[: contact_match.start()]
    map_pattern = re.compile(r"(地図アプリ|地図で見る|マップ|Google\s*マップ|アクセス|アクセスマップ|ルート|経路|Route|Directions|行き方)", re.IGNORECASE)
    map_match = map_pattern.search(s)
    if map_match:
        s = s[: map_match.start()]
    arrow_idx = min([idx for idx in (s.find("→"), s.find("⇒")) if idx >= 0], default=-1)
    if arrow_idx >= 0:
        s = s[:arrow_idx]
    # 全角英数字・記号を半角に寄せる
    s = s.translate(str.maketrans("０１２３４５６７８９－ー―‐／", "0123456789----/"))
    # 漢数字を簡易的に算用数字へ
    def convert_kanji_numbers(text: str) -> str:
        digit_map = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        unit_map = {"十": 10, "百": 100, "千": 1000}

        def repl(match: re.Match) -> str:
            chars = match.group(0)
            total = 0
            current = 0
            for ch in chars:
                if ch in unit_map:
                    base = current if current > 0 else 1
                    total += base * unit_map[ch]
                    current = 0
                else:
                    current = current * 10 + digit_map.get(ch, 0)
            total += current
            return str(total)

        # 丁目/番地/号などの直前に現れる数のみ変換し、地名（千代田/三郷など）を壊さない
        pattern = re.compile(r"[〇零一二三四五六七八九十百千]+(?=(丁目|番地|番|号|条|[-‐―ー−/0-9]))")
        return pattern.sub(repl, text)
    s = convert_kanji_numbers(s)
    s = re.sub(r"[‐―－ー]+", "-", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^〒\s*", "〒", s)
    # （市区町村コード:12208）等の「住所ではない補助情報」を除去（フォームノイズ判定に引っかかるのを防ぐ）
    # ※ 正規化で「コード」の長音が '-' になるケースがあるので両対応
    s = re.sub(r"[（(]\s*(?:市区町村|自治体)コ[-ー]ド\s*[:：]\s*\d+\s*[)）]", "", s)
    s = re.sub(r"(?:市区町村|自治体)コ[-ー]ド\s*[:：]\s*\d+", "", s)
    s = _strip_address_label(s)
    s = _cut_trailing_non_address(s)
    # Cut at first illegal symbol for address
    illegal_re = re.compile(r"[<>\uFF1C\uFF1E\{\}\uFF5B\uFF5D\[\]\uFF3B\uFF3D\(\)\uFF08\uFF09\u300C\u300D\u300E\u300F\u3010\u3011\u3014\u3015\u3008\u3009\u300A\u300B\"'=\uFF1D+\uFF0B*\uFF0A\^\uFF3E$\uFF04#\uFF03@\uFF20&\uFF06|\uFF5C\\\\\uFF3C~\uFF5E`!\uFF01?\uFF1F;\uFF1B:\uFF1A/\uFF0F\u203B\u2605\u2606\u25CF\u25A0\u25C6\u2022\u2026]")
    m_illegal = illegal_re.search(s)
    if m_illegal:
        s = s[: m_illegal.start()]
    # 住所入力フォームのラベル/候補一覧が混入したケースを除外
    if ADDRESS_FORM_NOISE_RE.search(s):
        return None
    if ("郵便番号" in s) and not ZIP_CODE_RE.search(s):
        return None
    try:
        pref_hits = sum(1 for pref in CompanyScraper.PREFECTURE_NAMES if pref in s)
    except Exception:
        pref_hits = 0
    if pref_hits >= 3:
        return None
    m = re.search(r"(\d{3}-\d{4})\s*(.*)", s)
    if m:
        body = _cut_trailing_non_address(m.group(2).strip())
        # 郵便番号だけの場合は住所とみなさない
        if not body:
            return None
        return f"〒{m.group(1)} {body}"
    return s if s else None


def is_prefecture_only_address(text: str | None) -> bool:
    if not text:
        return False
    s = unicodedata.normalize("NFKC", str(text))
    s = re.sub(r"\s+", "", s)
    if not s:
        return False
    s = re.sub(r"^〒?\d{3}[-\s]?\d{4}", "", s)
    s = s.strip(" 　\t,，;；。．|｜/／・-‐―－ー:：")
    if not s:
        return False
    try:
        return s in CompanyScraper.PREFECTURE_NAMES
    except Exception:
        return bool(re.fullmatch(r".+(都|道|府|県)", s)) and len(s) <= 4


def is_address_verifiable(text: str | None) -> bool:
    """
    verify_on_site の検証対象にできる程度に、住所が具体的かどうか。
    - 都道府県だけ等の低品質住所は False（マッチが容易すぎて誤判定を招く）
    """
    normalized = normalize_address(text)
    if not normalized:
        return False
    if is_prefecture_only_address(normalized):
        return False
    if ZIP_CODE_RE.search(normalized):
        return True
    if CITY_RE.search(normalized):
        return True
    if re.search(r"\d", normalized) and re.search(r"(丁目|番地|番|号)", normalized):
        return True
    return False


def sanitize_text_block(text: str | None) -> str:
    """
    軽量なサニタイズ: HTMLタグ除去・制御文字除去・空白圧縮。
    住所や説明など、DBに入れる前に通してノイズを落とす。
    """
    if not text:
        return ""
    t = html_mod.unescape(str(text))
    t = t.replace("[TABLE]", "")
    t = re.sub(r"<[^>]+>", " ", t)
    # スタイル/コメント断片は早めに除去
    t = re.sub(r"(?is)<style.*?>.*?</style>", " ", t)
    t = re.sub(r"(?is)<!--.*?-->", " ", t)
    t = re.sub(r"\bbr\s*/?\b", " ", t, flags=re.I)
    t = re.sub(r'\b(?:class|id|style|data-[\w-]+)\s*=\s*"[^"]*"', " ", t, flags=re.I)
    t = re.sub(r'\b(?:width|height|alt|href|src|title|rel)\s*=\s*"[^"]*"', " ", t, flags=re.I)
    t = re.sub(r"\b(?:width|height|alt|href|src|title|rel)\s*=\s*'[^']*'", " ", t, flags=re.I)
    t = t.replace(">", " ").replace("<", " ")
    t = t.replace("|", " ").replace("｜", " ")
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
    t = re.sub(r"\s+", " ", t)
    t = t.strip()
    # 典型的なUTF-8モジバケを検知したら破棄
    if looks_mojibake(t):
        return ""
    # 「地図/マップ/アクセス」やスクリプト断片が出たらそこまででカット
    map_noise_re = re.compile(
        r"(地図アプリ|地図で見る|マップ|Google\s*マップ|map|アクセス|ルート|拡大地図|gac?\.push|gtag|_gaq|googletagmanager|<script|function\s*\()",
        re.I,
    )
    m_map = map_noise_re.search(t)
    if m_map:
        t = t[: m_map.start()].strip()
    if not t:
        return ""
    return t


def looks_like_address(text: str | None) -> bool:
    """
    明らかに住所ではない文字列（例: 「企業理念」「企業紹介映像」など）を弾くための軽い判定。
    - 郵便番号 or 都道府県名が含まれていれば住所らしいとみなす
    """
    if not text:
        return False
    s = (text or "").strip()
    if not s:
        return False
    if ADDRESS_FORM_NOISE_RE.search(s):
        return False
    if ("郵便番号" in s) and not ZIP_CODE_RE.search(s):
        return False
    try:
        pref_hits = sum(1 for pref in CompanyScraper.PREFECTURE_NAMES if pref in s)
    except Exception:
        pref_hits = 0
    if pref_hits >= 3:
        return False
    if ZIP_CODE_RE.search(s):
        return True
    has_pref = False
    try:
        has_pref = any(pref in s for pref in CompanyScraper.PREFECTURE_NAMES)
    except Exception:
        has_pref = False
    has_city = bool(CITY_RE.search(s))
    if has_pref and has_city:
        return True

    if (has_pref or has_city) and re.search(r"(丁目|番地|号)", s) and re.search(r"\d", s):
        return True
    if (has_pref or has_city) and re.search(r"(ビル|マンション)", s) and re.search(r"\d", s):
        return True
    return False

def addr_compatible(input_addr: str, found_addr: str) -> bool:
    input_addr = normalize_address(input_addr)
    found_addr = normalize_address(found_addr)
    if not input_addr or not found_addr:
        return True
    return input_addr[:8] in found_addr or found_addr[:8] in input_addr


FREE_HOST_SUFFIXES = (
    ".wixsite.com", ".ameblo.jp", ".fc2.com", ".jimdo.com", ".blogspot.com",
    ".note.jp", ".hatena.ne.jp", ".weebly.com", ".wordpress.com", ".tumblr.com",
)


def _is_free_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host.endswith(suf) for suf in FREE_HOST_SUFFIXES)

def should_skip_by_url_flag(flag_info: dict | None) -> bool:
    """
    url_flags の「非公式」判定を候補URLの事前スキップに使うかどうか。
    - ルール由来（directory_like 等）や高confidenceのAIはハードに扱う
    - 低confidenceのAI非公式は誤爆しやすいので再評価対象としてスキップしない
    """
    if not flag_info:
        return False
    if flag_info.get("is_official") is not False:
        return False
    source = (flag_info.get("judge_source") or "").strip().lower()
    if source.startswith("ai_provisional"):
        return False
    if source.startswith("ai_conflict"):
        return False
    conf = flag_info.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else None
    except Exception:
        conf_f = None
    if source == "ai" and (conf_f is None or conf_f < AI_SKIP_NEGATIVE_FLAG_MIN_CONFIDENCE):
        return False
    return True


def _official_signal_ok(
    *,
    host_token_hit: bool,
    strong_domain_host: bool,
    domain_score: int,
    name_hit: bool,
    address_ok: bool,
    official_evidence_score: int = 0,
) -> bool:
    strong_domain = host_token_hit or strong_domain_host or domain_score >= 4
    name_or_addr = name_hit or address_ok or official_evidence_score >= 9
    return bool(strong_domain and name_or_addr)

def pick_best_address(expected_addr: str | None, candidates: list[str]) -> str | None:
    def _parse_source(raw: str) -> tuple[str, bool, str]:
        if not raw:
            return "OTHER", False, ""
        s = str(raw).strip()
        tags: list[str] = []
        rest = s
        while True:
            m = re.match(r"^\[([A-Z_]+)\]", rest)
            if not m:
                break
            tags.append(m.group(1))
            rest = rest[m.end():].lstrip()
        source = next((t for t in tags if t in {"JSONLD", "TABLE", "LABEL", "FOOTER", "TEXT"}), "OTHER")
        is_hq = "HQ" in tags or "HEADQUARTERS" in tags
        # remove any remaining bracket tags that might slip through
        rest = re.sub(r"^\[[A-Z_]+\]\s*", "", rest)
        return source, is_hq, rest.strip()

    source_bonus = {
        "JSONLD": 6.0,
        "TABLE": 5.0,
        "LABEL": 4.0,
        "FOOTER": 3.0,
        "TEXT": 0.0,
        "OTHER": 0.0,
    }

    normalized_candidates: list[tuple[str, str, bool]] = []  # (normalized, source, is_hq)
    for cand in candidates:
        source, is_hq, raw_val = _parse_source(cand)
        norm = normalize_address(raw_val)
        if norm:
            normalized_candidates.append((norm, source, is_hq))
    if not normalized_candidates:
        return None
    # dedupe while keeping the best source bonus for the same normalized address
    best_by_norm: dict[str, tuple[str, bool]] = {}
    for norm, src, is_hq in normalized_candidates:
        if norm not in best_by_norm:
            best_by_norm[norm] = (src, is_hq)
            continue
        prev_src, prev_hq = best_by_norm[norm]
        prev_score = source_bonus.get(prev_src, 0.0) + (4.0 if prev_hq else 0.0)
        cur_score = source_bonus.get(src, 0.0) + (4.0 if is_hq else 0.0)
        if cur_score > prev_score:
            best_by_norm[norm] = (src, is_hq)
    normalized_candidates = [(n, s, hq) for n, (s, hq) in best_by_norm.items()]
    if not expected_addr:
        # 郵便番号/市区町村/丁目を多く含むものを優先
        def _score(addr: str) -> int:
            score = 0
            if ZIP_CODE_RE.search(addr):
                score += 6
            if CITY_RE.search(addr):
                score += 4
            if re.search(r"丁目|番地|号", addr):
                score += 2
            if re.search(r"(ビル|マンション)", addr):
                score += 1
            return score
        normalized_candidates.sort(
            key=lambda pair: (_score(pair[0]) + source_bonus.get(pair[1], 0.0) + (4.0 if pair[2] else 0.0), len(pair[0])),
            reverse=True,
        )
        return normalized_candidates[0][0]

    expected_norm = normalize_address(expected_addr)
    if not expected_norm:
        normalized_candidates.sort(key=lambda pair: (source_bonus.get(pair[1], 0.0) + (4.0 if pair[2] else 0.0)), reverse=True)
        return normalized_candidates[0][0]

    expected_pref = CompanyScraper._extract_prefecture(expected_norm)
    expected_key = CompanyScraper._addr_key(expected_norm)
    expected_zip_match = ZIP_CODE_RE.search(expected_norm)
    expected_zip = expected_zip_match.group(1) if expected_zip_match else ""
    expected_tokens = KANJI_TOKEN_RE.findall(expected_norm)

    best = normalized_candidates[0][0]
    best_score = float("-inf")
    for cand, src, is_hq in normalized_candidates:
        key = CompanyScraper._addr_key(cand)
        score = 0.0
        if is_hq:
            score += 6.0
        if expected_pref:
            if expected_pref in cand:
                score += 3.0
            else:
                # 都道府県不一致は採用リスクが高いので強めに減点（ただし候補としては保持する）
                score -= 10.0
        cand_zip_match = ZIP_CODE_RE.search(cand)
        if expected_zip and cand_zip_match and cand_zip_match.group(1) == expected_zip:
            score += 8
        elif not expected_zip and cand_zip_match:
            score += 1
        if CITY_RE.search(cand):
            score += 3
        if re.search(r"(丁目|番地|号)", cand):
            score += 2
        if expected_key and key:
            score += SequenceMatcher(None, expected_key, key).ratio() * 6
        for token in expected_tokens:
            if token and token in cand:
                score += min(len(token), 4)
                break
        score += source_bonus.get(src, 0.0)
        if score > best_score:
            best_score = score
            best = cand
    return best

def _strip_leading_tags(value: str) -> str:
    out = value or ""
    while True:
        m = re.match(r"^\[[A-Z_]+\]", out)
        if not m:
            break
        out = out[m.end():].lstrip()
    return out

def _candidate_address_norms(candidates: list[str]) -> list[str]:
    norms: list[str] = []
    for raw in candidates:
        if not raw:
            continue
        norm = normalize_address(_strip_leading_tags(str(raw)))
        if norm:
            norms.append(norm)
    return norms

def _has_hq_tag_for_address(candidates: list[str], target_norm: str) -> bool:
    for raw in candidates:
        if isinstance(raw, str) and "[HQ]" in raw:
            norm = normalize_address(_strip_leading_tags(raw))
            if norm and norm == target_norm:
                return True
    return False

def _address_candidate_ok(
    candidate_norm: str,
    candidates: list[str],
    page_type: str,
    input_addr: str,
    ai_official_selected: bool,
) -> tuple[bool, str]:
    if not candidate_norm:
        return False, "no_valid_address"
    if ai_official_selected:
        return True, ""
    has_hq_tag = _has_hq_tag_for_address(candidates, candidate_norm)
    if has_hq_tag:
        return True, ""
    addr_match = bool(input_addr) and addr_compatible(input_addr, candidate_norm)
    has_zip = bool(ZIP_CODE_RE.search(candidate_norm))
    has_city = bool(CITY_RE.search(candidate_norm))
    only_one = len(set(_candidate_address_norms(candidates))) == 1
    page_ok = page_type in {"COMPANY_PROFILE", "ACCESS_CONTACT"}
    if addr_match and (page_ok or has_zip or has_city):
        return True, ""
    if page_ok and only_one and (has_zip or has_city):
        return True, ""
    return False, "no_hq_marker"

def _strip_rep_tags(value: str) -> tuple[str, list[str]]:
    out = value or ""
    tags: list[str] = []
    while True:
        m = re.match(r"^\[([A-Z_]+)\]", out)
        if not m:
            break
        tags.append(m.group(1))
        out = out[m.end():].lstrip()
    out = re.sub(r"\s+", " ", out).strip()
    return out, tags

def _rep_candidate_meta(candidates: list[str], chosen: str) -> dict[str, bool]:
    chosen_norm = re.sub(r"\s+", " ", chosen or "").strip()
    if not chosen_norm:
        return {"low_role": False, "table": False, "label": False, "role": False, "jsonld": False}
    for raw in candidates:
        base, tags = _strip_rep_tags(str(raw))
        if base == chosen_norm:
            tag_set = set(tags)
            return {
                "low_role": "LOWROLE" in tag_set,
                "table": "TABLE" in tag_set,
                "label": "LABEL" in tag_set,
                "role": "ROLE" in tag_set,
                "jsonld": "JSONLD" in tag_set,
            }
    return {"low_role": False, "table": False, "label": False, "role": False, "jsonld": False}

def _is_profile_like_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False
    return any(seg in path for seg in ("/company", "/about", "/corporate", "/profile", "/overview", "/gaiyo", "/gaiyou"))

def _rep_candidate_ok(
    chosen: str | None,
    candidates: list[str],
    page_type: str,
    source_url: str | None,
) -> tuple[bool, str]:
    if not chosen:
        return False, "no_rep"
    meta = _rep_candidate_meta(candidates, chosen)
    low_role = meta.get("low_role", False)
    profile_like = _is_profile_like_url(source_url)
    strong_source = (
        meta.get("table", False)
        or meta.get("label", False)
        or meta.get("role", False)
        or meta.get("jsonld", False)
    )
    if low_role:
        return False, "low_role"
    if page_type == "COMPANY_PROFILE":
        return True, ""
    if page_type == "ACCESS_CONTACT":
        if low_role:
            return False, "low_role_contact"
        if profile_like or strong_source:
            return True, ""
        return False, "contact_not_profile"
    if page_type == "BASES_LIST":
        if low_role:
            return False, "low_role_bases"
        if profile_like or strong_source:
            return True, ""
        return False, "bases_not_profile"
    if page_type == "OTHER":
        if low_role:
            return False, "low_role_other"
        if profile_like or strong_source:
            return True, ""
        return False, "other_not_profile"
    return False, f"not_profile:{page_type}"

def pick_best_phone(candidates: list[str]) -> str | None:
    best: str | None = None
    best_is_table = False
    for cand in candidates:
        if not cand:
            continue
        raw = str(cand)
        is_table = "[TABLE]" in raw
        is_fax = "[FAX]" in raw
        is_tel = "[TEL]" in raw
        if is_fax and not is_tel:
            continue
        raw_for_norm = re.sub(r"\[(?:TABLE|FAX|TEL|TEXT)\]", "", raw)
        norm = normalize_phone(raw_for_norm)
        if not norm:
            continue
        norm_val = norm
        # prefer non-navi numbers over 0120/0570
        if best is None:
            best = norm_val
            best_is_table = is_table
            continue
        if re.match(r"^(0120|0570)", norm_val) and not re.match(r"^(0120|0570)", best):
            continue
        if re.match(r"^(0120|0570)", best) and not re.match(r"^(0120|0570)", norm_val):
            best = norm_val
            best_is_table = is_table
            continue
        # prefer table-derived
        if is_table and not best_is_table:
            best = norm_val
            best_is_table = True
            continue
        # prefer standard 12-13 chars (including hyphens)
        if len(norm_val) == len(best):
            continue
        if abs(len(norm_val) - 12) < abs(len(best) - 12):
            best = norm_val
            best_is_table = is_table
    return best


def ai_official_hint_from_judge(ai_judge: dict[str, Any] | None, min_conf: float) -> bool:
    """
    AIが公式可能性が高いと返した候補を「除外」ではなく暫定候補として扱うための判定。
    """
    if not isinstance(ai_judge, dict):
        return False
    is_official_site = ai_judge.get("is_official_site")
    if is_official_site is None:
        is_official_site = ai_judge.get("is_official")
    conf = ai_judge.get("official_confidence")
    if conf is None:
        conf = ai_judge.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else 0.0
    except Exception:
        conf_f = 0.0
    return bool(is_official_site is True and conf_f >= float(min_conf or 0.0))

def is_over_deep_limit(total_elapsed: float, homepage: str | None, official_phase_end: float, time_limit_deep: float) -> bool:
    if time_limit_deep <= 0 or not homepage:
        return False
    if official_phase_end > 0:
        return (total_elapsed - official_phase_end) > time_limit_deep
    return total_elapsed > time_limit_deep

def pick_best_rep(names: list[str], source_url: str | None = None) -> str | None:
    role_keywords = ("代表", "取締役", "社長", "理事長", "会長", "院長", "学長", "園長", "代表社員", "CEO", "COO")
    blocked = ("スタッフ", "紹介", "求人", "採用", "ニュース", "退任", "就任", "人事", "異動", "お知らせ", "プレス", "取引")
    rep_noise_words = (
        "社是",
        "社訓",
        "スローガン",
        "理念",
        "方針",
        "ビジョン",
        "ミッション",
        "バリュー",
        "利用者",
        "お客様",
        "皆様",
        "方々",
        "の方",
    )
    url_bonus = 3 if source_url and any(seg in source_url for seg in ("/company", "/about", "/corporate")) else 0
    best = None
    best_score = float("-inf")
    for raw in names:
        if not raw:
            continue
        cleaned_raw = str(raw).strip()
        cleaned, tags = _strip_rep_tags(cleaned_raw)
        tag_set = set(tags)
        is_table = "TABLE" in tag_set
        is_label = "LABEL" in tag_set
        low_role = "LOWROLE" in tag_set
        if not cleaned:
            continue
        if low_role:
            continue
        if not tag_set and not any(k in cleaned for k in role_keywords):
            continue
        if not (NAME_CHUNK_RE.search(cleaned) or KANA_NAME_RE.search(cleaned)):
            continue
        if any(w in cleaned for w in rep_noise_words):
            continue
        if any(b in cleaned for b in blocked):
            continue
        score = len(cleaned) + url_bonus
        if is_table:
            score += 5
        if is_label:
            score += 3
        if any(k in cleaned for k in role_keywords):
            score += 8
        token_count = len([t for t in cleaned.split() if t])
        if token_count >= 3:
            score -= 6
        if score > best_score:
            best_score = score
            best = cleaned
    return best

def _score_amount_for_choice(val: str) -> int:
    if not val:
        return -10
    # prefer larger units/longer numbers up to 20 chars
    score = min(len(val), 20)
    if "兆" in val:
        score += 6
    if "億" in val:
        score += 4
    if "万" in val:
        score += 2
    return score

def pick_best_amount(candidates: list[str]) -> str | None:
    best = None
    best_score = float("-inf")
    for cand in candidates:
        is_table = False
        value = cand
        if isinstance(cand, str) and cand.startswith("[TABLE]"):
            is_table = True
            value = cand.replace("[TABLE]", "", 1)
        cleaned = clean_amount_value(value)
        if not cleaned:
            continue
        score = _score_amount_for_choice(cleaned)
        if is_table:
            score += 3
        if score > best_score:
            best_score = score
            best = cleaned
    return best

def pick_best_listing(candidates: list[str]) -> str | None:
    best = None
    best_len = -1
    for cand in candidates:
        is_table = False
        value = cand
        if isinstance(cand, str) and cand.startswith("[TABLE]"):
            is_table = True
            value = cand.replace("[TABLE]", "", 1)
        cleaned = clean_listing_value(value)
        if not cleaned:
            continue
        # prefer shorter market labels / 4-digit codes
        effective_len = len(cleaned) - (2 if is_table else 0)
        if best is None or effective_len < best_len:
            best = cleaned
            best_len = effective_len
    return best

def select_relevant_paragraphs(text: str, limit: int = 3) -> str:
    """
    説明抽出用に、事業系キーワードを含む上位段落を抽出する。
    入力テキスト全体をAIに渡さずに短縮し、時間を抑える。
    """
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"[\r\n]+", text) if p.strip()]
    if not paragraphs:
        return ""
    biz_keywords = (
        "事業", "サービス", "製造", "開発", "販売", "提供", "運営", "支援",
        "ソリューション", "product", "製品", "システム", "物流", "建設", "工事",
        "コンサル", "研究", "技術", "教育", "医療", "食品", "エネルギー", "不動産",
    )
    scored: list[tuple[int, str]] = []
    for para in paragraphs:
        score = 0
        for kw in biz_keywords:
            if kw.lower() in para.lower():
                score += 2
        score += min(len(para), 200) // 50  # 長さで軽くスコア
        scored.append((score, para))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [p for _, p in scored[:limit]]
    return "\n".join(top)

def clean_listing_value(val: str) -> str:
    text = (val or "").strip().replace("　", " ")
    if not text:
        return ""
    if re.search(r"[。！？!?\n]", text):
        return ""
    text = re.sub(r"\s+", "", text)
    if len(text) > 15:
        return ""
    lowered = text.lower()
    if any(keyword.lower() in lowered for keyword in LISTING_ALLOWED_KEYWORDS):
        return text
    if re.fullmatch(r"(?:上場|未上場|非上場)", text):
        return text
    if re.fullmatch(r"[0-9]{4}", text):  # 証券コードのみ
        return text
    return ""

def clean_amount_value(val: str) -> str:
    raw = (val or "").strip()
    raw = normalize_kanji_numbers(raw)
    if not raw:
        return ""
    # 従業員数など人員系の表記を除外
    if re.search(r"(従業員|社員|職員|スタッフ)\s*[0-9０-９]+", raw):
        return ""
    if re.search(r"[0-9０-９]+\s*(名|人)\b", raw):
        return ""
    # まず AI 側と同等の金額正規化ロジックを流用して数値＋「円」に統一を試みる
    try:
        normalized = ai_normalize_amount(raw)
    except Exception:
        normalized = None
    if isinstance(normalized, str) and normalized.strip():
        return normalized.strip()[:40]

    # フォールバック: 単位付きの金額部分だけを抽出して軽くクレンジング
    text = raw.replace("　", " ")
    m = re.search(r"([0-9０-９,\.]+(?:兆|億|百|十)?万円?|[0-9０-９,\.]+円)", text)
    if m:
        text = m.group(1)
    if not re.search(r"[0-9０-９]", text):
        return ""
    if not any(unit in text for unit in AMOUNT_ALLOWED_UNITS):
        return ""
    text = re.sub(r"[()（）]", "", text)
    text = re.sub(r"\s+", "", text)
    if len(text) > 40:
        text = text[:40]
    return text


def _truncate_description(text: str) -> str:
    if len(text) <= DESCRIPTION_MAX_LEN:
        return text
    truncated = text[:DESCRIPTION_MAX_LEN]
    truncated = re.sub(r"[、。．,;]+$", "", truncated)
    trimmed = re.sub(r"\s+\S*$", "", truncated).strip()
    return trimmed if len(trimmed) >= DESCRIPTION_MIN_LEN else truncated.rstrip()


def _normalize_business_label(label: str) -> str:
    cleaned = unicodedata.normalize("NFKC", label or "")
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned.strip("・:：")


def _is_business_label(label: str) -> bool:
    cleaned = _normalize_business_label(label)
    if not cleaned:
        return False
    if BUSINESS_LABEL_EXCLUDE_RE.search(cleaned):
        return False
    return bool(BUSINESS_LABEL_RE.search(cleaned))


def _clean_business_value(value: str) -> str:
    text = html_mod.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("・-—‐－:：/|")
    if not text:
        return ""
    m = BUSINESS_VALUE_CUT_RE.search(text)
    if m:
        text = text[: m.start()].strip()
    if not text:
        return ""
    text = re.sub(r"(?:他|ほか|外|等)\s*\d*(?:名|人)?\s*$", "", text).strip()
    if not text:
        return ""
    lower = text.lower()
    if text in BUSINESS_VALUE_PLACEHOLDERS or lower in BUSINESS_VALUE_PLACEHOLDERS:
        return ""
    return text


def _build_description_from_business_value(value: str) -> str:
    cleaned = _clean_business_value(value)
    if not cleaned:
        return ""
    direct = clean_description_value(cleaned)
    if direct:
        return direct
    if len(cleaned) < 3:
        return ""
    sentence = f"{cleaned}を主な事業としています"
    return clean_description_value(sentence) or ""


def extract_business_description(text: str | None, html: str | None) -> str:
    candidates: list[str] = []
    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            soup = None
        if soup:
            for tr in soup.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                if len(cells) < 2:
                    continue
                label = cells[0].get_text(separator=" ", strip=True)
                value = cells[1].get_text(separator=" ", strip=True)
                if label and value and _is_business_label(label):
                    candidates.append(value)
            for dl in soup.find_all("dl"):
                dts = dl.find_all("dt")
                dds = dl.find_all("dd")
                for dt, dd in zip(dts, dds):
                    label = dt.get_text(separator=" ", strip=True)
                    value = dd.get_text(separator=" ", strip=True)
                    if label and value and _is_business_label(label):
                        candidates.append(value)

    if text:
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
        for line in lines:
            if ":" in line or "：" in line:
                label, value = re.split(r"[:：]", line, 1)
                if _is_business_label(label):
                    candidates.append(value)
        for idx in range(len(lines) - 1):
            if _is_business_label(lines[idx]):
                candidates.append(lines[idx + 1])

    for value in candidates:
        desc = _build_description_from_business_value(value)
        if desc:
            return desc
    return ""


def clean_description_value(val: str) -> str:
    text = html_mod.unescape((val or "").strip())
    text = re.sub(r"<[^>]+>", " ", text)
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    stripped = text.strip("・-—‐－ー")
    if "<" in stripped or "class=" in stripped or "svg" in stripped:
        return ""
    # 企業DB/まとめサイトの定型文を除外
    if ("サイト" in stripped or "ページ" in stripped) and any(
        w in stripped
        for w in (
            "データベース",
            "登録企業",
            "掲載",
            "企業詳細",
            "会社情報を掲載",
            "企業情報を掲載",
            "口コミ",
            "評判",
            "ランキング",
        )
    ):
        return ""
    # URL/メール/TEL系は説明にしない
    if re.search(r"https?://|mailto:|@|＠|tel[:：]|電話|ＴＥＬ|ＴＥＬ：", stripped, flags=re.I):
        return ""
    if any(term in stripped for term in ("お問い合わせ", "お問合せ", "アクセス", "予約", "営業時間")):
        return ""
    policy_blocks = (
        "方針",
        "ポリシー",
        "理念",
        "ビジョン",
        "挨拶",
        "ご挨拶",
        "メッセージ",
        "品質",
        "環境",
        "安全",
        "コンプライアンス",
        "情報セキュリティ",
    )
    if any(word in stripped for word in policy_blocks):
        return ""
    if stripped in GENERIC_DESCRIPTION_TERMS:
        return ""
    if len(stripped) < DESCRIPTION_MIN_LEN:
        return ""
    if re.fullmatch(r"(会社概要|事業概要|法人概要|沿革|会社案内|企業情報)", stripped):
        return ""
    # 複数文の場合、使える1文だけを拾う（末尾に採用/問い合わせ等が付いても落としすぎない）
    candidates = [stripped]
    if "。" in stripped or "．" in stripped:
        parts = [p.strip() for p in re.split(r"[。．]", stripped) if p.strip()]
        candidates = parts or candidates

    for cand in candidates:
        if cand in GENERIC_DESCRIPTION_TERMS:
            continue
        if len(cand) < DESCRIPTION_MIN_LEN:
            continue
        if any(term in cand for term in ("お問い合わせ", "お問合せ", "アクセス", "予約", "営業時間")):
            continue
        if any(word in cand for word in policy_blocks):
            continue
        if ("サイト" in cand or "ページ" in cand) and any(
            w in cand
            for w in (
                "データベース",
                "登録企業",
                "掲載",
                "企業詳細",
                "会社情報を掲載",
                "企業情報を掲載",
                "口コミ",
                "評判",
                "ランキング",
            )
        ):
            continue
        if re.search(r"https?://|mailto:|@|＠|tel[:：]|電話|ＴＥＬ|ＴＥＬ：", cand, flags=re.I):
            continue
        # 事業内容を示すキーワードが全く無い場合だけ除外
        if not any(k in cand for k in DESCRIPTION_BIZ_KEYWORDS):
            continue
        return _truncate_description(cand)
    return ""

def clean_fiscal_month(val: str) -> str:
    text = (val or "").strip().replace("　", " ")
    if not text:
        return ""
    text = text.replace("期", "月").replace("末", "月")
    if re.fullmatch(r"[Qq][1-4]", text):
        qmap = {"Q1": "3月", "Q2": "6月", "Q3": "9月", "Q4": "12月"}
        return qmap.get(text.upper(), "")
    m = re.search(r"(1[0-2]|0?[1-9])\s*月", text)
    if m:
        return f"{int(m.group(1))}月"
    m = re.search(r"(1[0-2]|0?[1-9])", text)
    if m:
        return f"{int(m.group(1))}月"
    return ""


def extract_description_snippet(text: str | None) -> str | None:
    if not text:
        return None
    paragraphs = [p.strip() for p in re.split(r"[\r\n]+", text) if p.strip()]
    if not paragraphs:
        return None
    # ノイズになる段落を除外（ニュース・採用・日付行など）
    noise_patterns = (
        r"採用", r"求人", r"募集", r"ニュース", r"お知らせ", r"新着", r"イベント",
        r"\d{4}\s*年\s*\d{1,2}\s*月", r"\d{4}/\d{1,2}/\d{1,2}", r"\d{4}-\d{1,2}-\d{1,2}",
        r"会社概要", r"事業概要", r"法人概要", r"沿革", r"アクセス", r"お問い合わせ", r"お問合せ", r"営業時間"
    )
    cleaned_paragraphs: list[str] = []
    for para in paragraphs:
        lowered = para.lower()
        if any(re.search(pat, para) for pat in noise_patterns):
            continue
        if "news" in lowered or "recruit" in lowered or "採用" in para:
            continue
        cleaned_paragraphs.append(para)
    if cleaned_paragraphs:
        paragraphs = cleaned_paragraphs

    lowered = [p.lower() for p in paragraphs]
    for idx, para in enumerate(paragraphs):
        if any(hint.lower() in lowered[idx] for hint in DESCRIPTION_HINTS):
            cleaned = clean_description_value(para)
            if cleaned:
                return cleaned
    for para in paragraphs:
        cleaned = clean_description_value(para)
        if cleaned:
            return cleaned
    return None

def extract_meta_description(html: str | None) -> str | None:
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    for attr in ("description", "og:description"):
        node = soup.find("meta", attrs={"name": attr}) or soup.find("meta", attrs={"property": attr})
        if node:
            content = node.get("content") or ""
            cleaned = clean_description_value(content)
            if cleaned:
                return cleaned
    return None

def extract_lead_description(html: str | None) -> str | None:
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    candidates: list[str] = []
    noise_re = re.compile(r"(お問い合わせ|お問合せ|アクセス|採用|求人|募集|news|menu|nav|http|https|tel[:：]|電話)", re.I)
    for tag in soup.find_all(["h1", "h2", "p"], limit=8):
        text = tag.get_text(separator=" ", strip=True)
        if noise_re.search(text):
            continue
        cleaned = clean_description_value(text)
        if cleaned:
            candidates.append(cleaned)
    return candidates[0] if candidates else None

def extract_description_from_payload(payload: dict[str, Any]) -> str:
    text = payload.get("text", "") or ""
    html = payload.get("html", "") or ""
    biz_desc = extract_business_description(text, html)
    if biz_desc:
        return biz_desc
    snippet = extract_description_snippet(text)
    if snippet:
        return snippet
    meta = extract_meta_description(html)
    if meta:
        return meta
    lead = extract_lead_description(html)
    if lead:
        return lead
    return ""

def _sanitize_ai_text_block(text: str | None) -> str:
    if not text:
        return ""
    cleaned_lines: list[str] = []
    nav_keywords = (
        "copyright", "all rights reserved", "privacy policy", "サイトマップ", "sitemap",
        "recruit", "求人", "採用", "お問い合わせ", "アクセスマップ"
    )
    DIGIT_RE = re.compile(r"[0-9０-９]")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(keyword in lowered for keyword in nav_keywords):
            continue
        if re.search(r"https?://|mailto:|@|＠|tel[:：]|電話|ＴＥＬ|ＴＥＬ：", line, flags=re.I):
            continue
        cleaned_lines.append(line)
    result = " ".join(cleaned_lines)
    result = re.sub(r"\s+", " ", result).strip()
    if len(result) > 3500:
        result = result[:3500]
    return result

def build_ai_text_payload(*blocks: str) -> str:
    payloads = []
    for block in blocks:
        cleaned = _sanitize_ai_text_block(block)
        if cleaned:
            payloads.append(cleaned)
    joined = "\n\n".join(payloads)
    return joined[:4000]

def append_jsonl(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("append_jsonl failed: %s", path, exc_info=True)

def build_official_ai_text(text: str, html: str, signals: dict[str, Any] | None = None) -> str:
    """
    AI公式判定向けに、1回fetch済みの text/html から根拠を落としにくい形で短文化する。
    追加fetchはしない（CPUのみ）。
    """
    parts: list[str] = []
    if text:
        parts.append(str(text))
    if html:
        try:
            parts.append(CompanyScraper._meta_strings(html))
        except Exception:
            pass
        try:
            soup = BeautifulSoup(html, "html.parser")
            h1 = soup.find("h1")
            if h1:
                parts.append(f"[H1] {h1.get_text(' ', strip=True)}")
            header = soup.find("header")
            if header:
                header_text = header.get_text(" ", strip=True)
                if header_text:
                    parts.append(f"[HEADER] {header_text[:200]}")
                logo_hints: list[str] = []
                for node in header.find_all(["img", "a", "span", "div"]):
                    for attr in ("alt", "aria-label", "title"):
                        val = node.get(attr)
                        if not isinstance(val, str):
                            continue
                        val = val.strip()
                        if not val or len(val) > 40:
                            continue
                        if val not in logo_hints:
                            logo_hints.append(val)
                    if len(logo_hints) >= 5:
                        break
                if logo_hints:
                    parts.append(f"[LOGO] {' / '.join(logo_hints)}")
            footer = soup.find("footer")
            if footer:
                ft = footer.get_text(" ", strip=True)
                if ft:
                    parts.append(f"[FOOTER] {ft}")
        except Exception:
            pass
    if signals:
        try:
            keys = (
                "page_type",
                "domain_score",
                "host_token_hit",
                "name_match_ratio",
                "name_match_exact",
                "name_match_partial_only",
                "name_match_source",
                "official_evidence_score",
                "official_evidence",
                "title_match",
                "h1_match",
                "og_site_name_match",
                "directory_like",
            )
            sig_parts = []
            for key in keys:
                if key not in signals or signals[key] is None:
                    continue
                val = signals[key]
                if isinstance(val, bool):
                    val_str = "true" if val else "false"
                elif isinstance(val, float):
                    val_str = f"{val:.2f}"
                elif isinstance(val, (list, tuple, set)):
                    items = [str(x).strip() for x in val if str(x).strip()]
                    if not items:
                        continue
                    val_str = ",".join(items[:12])
                else:
                    val_str = str(val)
                if val_str:
                    sig_parts.append(f"{key}={val_str}")
            if sig_parts:
                parts.append(f"[SIGNALS] {' '.join(sig_parts)}")
        except Exception:
            pass
    joined = "\n".join([p for p in parts if p and str(p).strip()])
    try:
        joined = CompanyScraper._filter_noise_lines(joined)
    except Exception:
        pass
    return joined[:4000]

def clean_founded_year(val: str) -> str:
    text = (val or "").strip()
    if not text:
        return ""
    m = re.search(r"(18|19|20)\d{2}", text)
    if m:
        return m.group(0)
    if text.isdigit() and len(text) == 4:
        return text
    return ""


def record_needs_official_ai(record: dict[str, Any]) -> bool:
    if record.get("ai_judge"):
        return False
    if record.get("ai_checked"):
        return False
    if AI_OFFICIAL_ALL_CANDIDATES:
        rule_details = record.get("rule") or {}
        # 企業DB/ディレクトリ臭が強いものはAIコストを掛けない
        if rule_details.get("directory_like"):
            return False
        return True
    if record.get("force_ai_official"):
        return True
    rule_details = record.get("rule") or {}
    if rule_details.get("is_official"):
        return False
    domain_score = int(record.get("domain_score") or 0)
    host_token_hit = bool(record.get("host_token_hit"))
    if domain_score >= 4 and host_token_hit:
        return False
    if record.get("strong_domain_host"):
        return False
    score = float(rule_details.get("score") or 0.0)
    if rule_details.get("strong_domain") and score >= 4:
        return False
    return True


async def ensure_info_has_screenshot(
    scraper: CompanyScraper,
    url: str,
    info: dict[str, Any] | None,
    need_screenshot: bool = True,
) -> dict[str, Any]:
    info = info or {}
    if info.get("screenshot") or not need_screenshot:
        return info
    try:
        refreshed = await scraper.get_page_info(url, need_screenshot=True)
    except Exception:
        return info
    if not refreshed:
        return info
    merged = dict(info)
    for key in ("text", "html", "url", "screenshot"):
        if key in refreshed and refreshed[key]:
            merged[key] = refreshed[key]
    return merged

async def ensure_info_text(
    scraper: CompanyScraper,
    url: str,
    info: dict[str, Any] | None,
    allow_slow: bool = False,
) -> dict[str, Any]:
    """
    テキスト/HTMLのみ不足している場合に軽量に再取得する（スクショは撮らない）。
    """
    info = info or {}
    if info.get("text") and info.get("html"):
        return info
    try:
        host = ""
        if allow_slow:
            try:
                host = urlparse(url).netloc.lower().split(":")[0]
            except Exception:
                host = ""
        is_slow = bool(host and allow_slow and scraper._is_slow_host(host))  # type: ignore[attr-defined]
        if is_slow:
            timeout_ms = min(getattr(scraper, "http_timeout_ms", 6000), 2500)
            refreshed = await scraper._fetch_http_info(url, timeout_ms=timeout_ms, allow_slow=True)
        else:
            refreshed = await scraper.get_page_info(url, need_screenshot=False, allow_slow=allow_slow)
        if refreshed:
            if refreshed.get("text"):
                info["text"] = refreshed.get("text", "")
            if refreshed.get("html"):
                info["html"] = refreshed.get("html", "")
    except Exception:
        pass
    return info

def log_phase_metric(
    company_id: int,
    phase: str,
    elapsed_sec: float,
    status: str,
    homepage: str,
    error_code: str,
) -> None:
    if not PHASE_METRICS_PATH:
        return
    try:
        os.makedirs(os.path.dirname(PHASE_METRICS_PATH) or ".", exist_ok=True)
        file_exists = os.path.exists(PHASE_METRICS_PATH)
        with open(PHASE_METRICS_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["id", "phase", "elapsed_sec", "status", "homepage", "worker", "error_code"])
            writer.writerow([company_id, phase, f"{elapsed_sec:.3f}", status, homepage or "", WORKER_ID, error_code or ""])
    except Exception:
        log.debug("phase metrics write skipped", exc_info=True)

def is_ambiguous_company_name(name: str) -> bool:
    base = CompanyScraper._normalize_company_name(name)
    if not base:
        return True
    # ほぼ固有名詞がそのまま入っているとみなし、極端に短い場合のみ曖昧扱い
    if len(base) <= 2:
        return True
    tokens = CompanyScraper._company_tokens(name)
    return len(tokens) == 0

def should_skip_company(name: str) -> bool:
    """
    明らかに法人ではない/店舗・支店のみの名称をスキップする。
    - 都道府県名そのもの、庁・役所・役場を含む自治体名
    - コンビニ店舗（セブン/ファミマ/ローソン等）で末尾が「店」
    - 法人格を含まない支店/営業所/出張所のみの名称
    """
    base = (name or "").strip()
    if not base:
        return False
    norm = CompanyScraper._normalize_company_name(base)
    if norm in CompanyScraper.PREFECTURE_NAMES:
        return True
    if re.search(r"(県庁|市役所|区役所|町役場|村役場)$", base):
        return True
    konbini_keywords = ("セブン-イレブン", "セブンイレブン", "7-11", "7－11", "7–11", "ファミリーマート", "ファミマ", "ローソン", "ミニストップ", "セイコーマート", "デイリーヤマザキ")
    if any(kw in base for kw in konbini_keywords) and base.endswith("店"):
        return True
    has_corp = any(tag in base for tag in ("株式会社", "有限会社", "合同会社", "Inc", "Co.", "Corporation", "Company", "Ltd"))
    if not has_corp and re.search(r"(支店|営業所|出張所)$", base):
        return True
    return False

# --------------------------------------------------
# 内部: 次ジョブ取得
# --------------------------------------------------
def claim_next(manager: DatabaseManager) -> dict | None:
    if hasattr(manager, "claim_next_company"):
        return manager.claim_next_company(WORKER_ID)
    return manager.get_next_company()

# --------------------------------------------------
# ユーティリティ：ジッター付きスリープ秒
# --------------------------------------------------
def jittered_seconds(base: float, ratio: float) -> float:
    if base <= 0 or ratio <= 0:
        return max(0.0, base)
    low = max(0.0, base * (1.0 - ratio))
    high = base * (1.0 + ratio)
    return random.uniform(low, high)

# --------------------------------------------------
# メイン処理（ワーカー）
# --------------------------------------------------
async def process():
    global TIME_LIMIT_FETCH_ONLY, TIME_LIMIT_WITH_OFFICIAL, TIME_LIMIT_DEEP
    log.info(
        "=== Runner started (worker=%s) === HEADLESS=%s USE_AI=%s MAX_ROWS=%s "
        "ID_MIN=%s ID_MAX=%s AI_COOLDOWN_SEC=%s SLEEP_BETWEEN_SEC=%s JITTER_RATIO=%.2f "
        "MIRROR_TO_CSV=%s",
        WORKER_ID, HEADLESS, USE_AI, MAX_ROWS, ID_MIN, ID_MAX,
        AI_COOLDOWN_SEC, SLEEP_BETWEEN_SEC, JITTER_RATIO, MIRROR_TO_CSV
    )

    scraper = CompanyScraper(headless=HEADLESS)
    base_page_timeout_ms = getattr(scraper, "page_timeout_ms", 9000) or 9000
    normal_page_timeout_ms = getattr(scraper, "page_timeout_ms", base_page_timeout_ms)
    normal_slow_page_threshold_ms = getattr(scraper, "slow_page_threshold_ms", base_page_timeout_ms)
    PAGE_FETCH_TIMEOUT_SEC = max(5.0, (base_page_timeout_ms / 1000.0) + 5.0)
    # Playwright起動は get_page_info 内で必要時のみ行う（未導入環境での起動失敗や待ちを避ける）

    verifier = AIVerifier() if USE_AI else None
    manager = DatabaseManager(db_path=COMPANIES_DB_PATH, worker_id=WORKER_ID)
    if AI_CLEAR_NEGATIVE_FLAGS:
        cleared = manager.clear_ai_negative_url_flags()
        log.info("Cleared %s AI negative url_flags before evaluation.", cleared)

    csv_file = None
    csv_writer = None
    try:
        if MIRROR_TO_CSV:
            os.makedirs(os.path.dirname(OUTPUT_CSV_PATH) or ".", exist_ok=True)
            file_exists = os.path.exists(OUTPUT_CSV_PATH) and os.path.getsize(OUTPUT_CSV_PATH) > 0
            csv_file = open(OUTPUT_CSV_PATH, mode="a", newline="", encoding="utf-8")
            csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
            if not file_exists:
                csv_writer.writeheader()
                csv_file.flush()
            log.info("CSV mirror enabled -> %s", OUTPUT_CSV_PATH)

        processed = 0
        second_pass = False
        original_retry_statuses = list(manager.retry_statuses)
        if SECOND_PASS_ENABLED:
            manager.retry_statuses = []
        timeouts_extended = False

        while True:
            if MAX_ROWS and processed >= MAX_ROWS:
                log.info("MAX_ROWS=%s に到達。", MAX_ROWS)
                break

            company = claim_next(manager)
            if not company:
                if SECOND_PASS_ENABLED and not second_pass:
                    # セカンドパス: 長めのタイムアウトで retry_statuses を再処理
                    log.info("キューが空です。セカンドパス開始（遅延許容モード）。")
                    second_pass = True
                    manager.retry_statuses = SECOND_PASS_RETRY_STATUSES
                    # タイムアウトを緩和
                    TIME_LIMIT_FETCH_ONLY = LONG_TIME_LIMIT_FETCH_ONLY
                    TIME_LIMIT_WITH_OFFICIAL = LONG_TIME_LIMIT_WITH_OFFICIAL
                    TIME_LIMIT_DEEP = LONG_TIME_LIMIT_DEEP
                    try:
                        scraper.page_timeout_ms = LONG_PAGE_TIMEOUT_MS
                        scraper.slow_page_threshold_ms = LONG_SLOW_PAGE_THRESHOLD_MS
                    except Exception:
                        pass
                    timeouts_extended = True
                    continue
                log.info("キューが空です。終了。")
                break

            cid = company.get("id")
            name = (company.get("company_name") or "").strip()
            addr_raw = (company.get("address") or "").strip()
            company["csv_address"] = addr_raw
            addr = normalize_address(addr_raw) or ""
            input_addr_has_zip = bool(ZIP_CODE_RE.search(addr))
            input_addr_has_city = bool(CITY_RE.search(addr))
            input_addr_has_pref = any(pref in addr for pref in CompanyScraper.PREFECTURE_NAMES) if addr else False
            input_addr_pref_only = bool(input_addr_has_pref and not input_addr_has_city and not input_addr_has_zip)

            if (ID_MIN and cid < ID_MIN) or (ID_MAX and cid > ID_MAX):
                log.info("[skip] id=%s はレンジ外 -> skipped (worker=%s)", cid, WORKER_ID)
                manager.update_status(cid, "skipped")
                continue
            if should_skip_company(name):
                log.info("[skip] 法人でない名称のためスキップ: id=%s name=%s", cid, name)
                manager.update_status(cid, "skipped")
                continue

            log.info("[%s] %s の処理開始 (worker=%s)", cid, name, WORKER_ID)

            started_at = time.monotonic()
            timed_out = False
            company_has_corp = any(suffix in name for suffix in CompanyScraper.CORP_SUFFIXES)
            hard_timeout_candidates = [t for t in (GLOBAL_HARD_TIMEOUT_SEC, COMPANY_HARD_TIMEOUT_SEC, TIME_LIMIT_SEC) if t > 0]
            hard_timeout_sec = min(hard_timeout_candidates) if hard_timeout_candidates else 60.0
            if ABSOLUTE_COMPANY_DEADLINE_SEC > 0:
                hard_timeout_sec = min(hard_timeout_sec, ABSOLUTE_COMPANY_DEADLINE_SEC)
            hard_deadline = started_at + hard_timeout_sec
            timeout_stage = ""
            timeout_saved = False

            def elapsed() -> float:
                return time.monotonic() - started_at

            def over_time_limit() -> bool:
                if TIME_LIMIT_SEC > 0 and elapsed() > TIME_LIMIT_SEC:
                    return True
                if COMPANY_HARD_TIMEOUT_SEC > 0 and elapsed() > COMPANY_HARD_TIMEOUT_SEC:
                    return True
                if GLOBAL_HARD_TIMEOUT_SEC > 0 and elapsed() > GLOBAL_HARD_TIMEOUT_SEC:
                    return True
                return False

            def over_hard_deadline() -> bool:
                return time.monotonic() > hard_deadline

            def over_fetch_limit() -> bool:
                return TIME_LIMIT_FETCH_ONLY > 0 and not homepage and elapsed() > TIME_LIMIT_FETCH_ONLY

            def over_after_official() -> bool:
                return TIME_LIMIT_WITH_OFFICIAL > 0 and homepage and elapsed() > TIME_LIMIT_WITH_OFFICIAL

            deep_phase_deadline: float | None = None

            def over_deep_limit() -> bool:
                # 深掘りフェーズの専用上限（公式確定後）
                return is_over_deep_limit(elapsed(), homepage, official_phase_end, TIME_LIMIT_DEEP)

            def over_deep_phase_deadline() -> bool:
                return deep_phase_deadline is not None and time.monotonic() > deep_phase_deadline

            hard_timeout_logged = False
            ai_call_timeout = AI_CALL_TIMEOUT_SEC or 20.0

            def raise_hard_timeout(stage: str) -> None:
                nonlocal timed_out, hard_timeout_logged, timeout_stage
                timed_out = True
                timeout_stage = stage or timeout_stage
                if not company.get("error_code"):
                    company["error_code"] = "timeout"
                if not hard_timeout_logged:
                    log.info("[%s] hard timeout reached (%s) at %.1fs", cid, stage, elapsed())
                    hard_timeout_logged = True
                raise HardTimeout(stage)

            def ensure_global_time(stage: str = "") -> None:
                if over_hard_deadline():
                    raise_hard_timeout(stage or "global_deadline")

            def clamp_timeout(desired: float) -> float:
                remaining = hard_deadline - time.monotonic()
                if remaining <= 0:
                    raise_hard_timeout("global_deadline")
                if desired <= 0:
                    return max(0.1, remaining)
                return max(0.1, min(desired, remaining))

            def remaining_time_budget() -> float:
                return hard_deadline - time.monotonic()

            def has_time_for_ai() -> bool:
                return remaining_time_budget() > AI_MIN_REMAINING_SEC

            # タイムアウト時に「分かっている範囲」を確実に保存するためのデフォルト初期化
            urls: list[str] = []
            candidate_records: list[dict[str, Any]] = []
            homepage = (company.get("homepage") or "").strip()
            info: dict[str, Any] | None = None
            primary_cands: dict[str, list[str]] = {}
            fallback_cands: list[tuple[str, dict[str, list[str]]]] = []
            homepage_official_flag = int(company.get("homepage_official_flag") or 0)
            homepage_official_source = company.get("homepage_official_source", "") or ""
            homepage_official_score = float(company.get("homepage_official_score") or 0.0)
            ai_official_selected = False
            ai_time_spent = 0.0
            chosen_domain_score = 0
            search_phase_end = 0.0
            official_phase_end = 0.0
            deep_phase_end = 0.0
            deep_pages_visited = int(company.get("deep_pages_visited") or 0)
            deep_fetch_count = int(company.get("deep_fetch_count") or 0)
            deep_fetch_failures = int(company.get("deep_fetch_failures") or 0)
            deep_skip_reason = company.get("deep_skip_reason", "") or ""
            deep_urls_visited: list[str] = []
            deep_phone_candidates = int(company.get("deep_phone_candidates") or 0)
            deep_address_candidates = int(company.get("deep_address_candidates") or 0)
            deep_rep_candidates = int(company.get("deep_rep_candidates") or 0)

            phone = (company.get("phone") or "").strip()
            found_address = (company.get("found_address") or "").strip()
            rep_name_val = (scraper.clean_rep_name(company.get("rep_name")) or "").strip()
            description_val = (company.get("description") or "").strip()
            listing_val = clean_listing_value(company.get("listing") or "")
            revenue_val = clean_amount_value(company.get("revenue") or "")
            profit_val = clean_amount_value(company.get("profit") or "")
            capital_val = clean_amount_value(company.get("capital") or "")
            fiscal_val = clean_fiscal_month(company.get("fiscal_month") or "")
            founded_val = clean_founded_year(company.get("founded_year") or "")
            phone_source = company.get("phone_source", "") or "none"
            address_source = company.get("address_source", "") or "none"
            ai_used = int(company.get("ai_used") or 0)
            ai_model = company.get("ai_model", "") or ""
            src_phone = company.get("source_url_phone", "") or ""
            src_addr = company.get("source_url_address", "") or ""
            src_rep = company.get("source_url_rep", "") or ""
            verify_result: dict[str, Any] = {"phone_ok": False, "address_ok": False}
            verify_result_source = "none"
            force_review = False
            confidence = float(company.get("extract_confidence") or 0.0)
            address_ai_confidence: float | None = company.get("address_confidence")
            address_ai_evidence: str | None = company.get("address_evidence")
            rule_phone: str | None = None
            rule_address: str | None = None
            rule_rep: str | None = None

            def save_partial(reason: str) -> None:
                nonlocal timeout_saved
                if timeout_saved:
                    return
                try:
                    if not (company.get("error_code") or "").strip():
                        company["error_code"] = reason or "timeout"
                    if not deep_skip_reason and (reason or timeout_stage):
                        company["deep_skip_reason"] = f"{reason or 'timeout'}:{timeout_stage}".strip(":")
                    # 正規化（軽量）だけ実施して保存する
                    normalized_found_address = normalize_address(found_address) if found_address else ""
                    company.update({
                        "homepage": homepage or "",
                        "phone": phone or "",
                        "found_address": normalized_found_address,
                        "rep_name": rep_name_val or "",
                        "description": description_val or "",
                        "listing": listing_val or "",
                        "revenue": revenue_val or "",
                        "profit": profit_val or "",
                        "capital": capital_val or "",
                        "fiscal_month": fiscal_val or "",
                        "founded_year": founded_val or "",
                        "phone_source": phone_source or "",
                        "address_source": address_source or "",
                        "ai_used": int(ai_used or 0),
                        "ai_model": ai_model or "",
                        "extract_confidence": confidence,
                        "source_url_phone": src_phone or "",
                        "source_url_address": src_addr or "",
                        "source_url_rep": src_rep or "",
                        "homepage_official_flag": int(homepage_official_flag or 0),
                        "homepage_official_source": homepage_official_source or "",
                        "homepage_official_score": float(homepage_official_score or 0.0),
                        "address_confidence": address_ai_confidence,
                        "address_evidence": address_ai_evidence,
                        "deep_pages_visited": int(deep_pages_visited or 0),
                        "deep_fetch_count": int(deep_fetch_count or 0),
                        "deep_fetch_failures": int(deep_fetch_failures or 0),
                        "deep_skip_reason": company.get("deep_skip_reason", "") or deep_skip_reason or "",
                            "deep_urls_visited": json.dumps(list(deep_urls_visited or [])[:5], ensure_ascii=False),
                            "deep_phone_candidates": int(deep_phone_candidates or 0),
                            "deep_address_candidates": int(deep_address_candidates or 0),
                            "deep_rep_candidates": int(deep_rep_candidates or 0),
                            "timeout_stage": timeout_stage or "",
                        })
                    manager.save_company_data(company, status="review")
                    timeout_saved = True
                except Exception:
                    log.warning("[%s] timeout partial save failed", cid, exc_info=True)

            def save_no_homepage(reason: str) -> None:
                nonlocal timeout_saved
                if timeout_saved:
                    return
                try:
                    top3_urls = list(urls or [])[:3]
                    top3_records = sorted(candidate_records or [], key=lambda r: r.get("search_rank", 1e9))[:3]
                    all_directory_like = bool(top3_records) and all(
                        bool((r.get("rule") or {}).get("directory_like")) for r in top3_records
                    )
                    if not top3_urls:
                        skip_reason = "no_search_results_or_prefiltered"
                    elif all_directory_like:
                        skip_reason = "top3_all_directory_like"
                    else:
                        skip_reason = reason or "no_official_in_top3"
                    if not (company.get("error_code") or "").strip():
                        company["error_code"] = skip_reason
                    append_jsonl(
                        NO_OFFICIAL_LOG_PATH,
                        {
                            "id": cid,
                            "company_name": name,
                            "csv_address": addr,
                            "skip_reason": skip_reason,
                            "top3_urls": top3_urls,
                            "top3_candidates": [
                                {
                                    "url": (r.get("normalized_url") or r.get("url") or ""),
                                    "search_rank": int(r.get("search_rank") or 0),
                                    "domain_score": int(r.get("domain_score") or 0),
                                    "rule_score": float(((r.get("rule") or {}).get("score")) or 0.0),
                                    "directory_like": bool((r.get("rule") or {}).get("directory_like")),
                                    "directory_score": int((r.get("rule") or {}).get("directory_score") or 0),
                                    "directory_reasons": list((r.get("rule") or {}).get("directory_reasons") or [])[:8],
                                    "blocked_host": bool((r.get("rule") or {}).get("blocked_host")),
                                    "prefecture_mismatch": bool((r.get("rule") or {}).get("prefecture_mismatch")),
                                }
                                for r in top3_records
                            ],
                        },
                    )
                    normalized_found_address = normalize_address(found_address) if found_address else ""
                    try:
                        _exclude = exclude_reasons  # type: ignore[name-defined]
                    except Exception:
                        _exclude = {}
                    try:
                        exclude_reasons_json = json.dumps(_exclude or {}, ensure_ascii=False)
                    except Exception:
                        exclude_reasons_json = "{}"
                    company.update(
                        {
                            "homepage": "",
                            "phone": phone or "",
                            "found_address": normalized_found_address,
                            "rep_name": rep_name_val or "",
                            "description": description_val or "",
                            "listing": listing_val or "",
                            "revenue": revenue_val or "",
                            "profit": profit_val or "",
                            "capital": capital_val or "",
                            "fiscal_month": fiscal_val or "",
                            "founded_year": founded_val or "",
                            "phone_source": phone_source or "",
                            "address_source": address_source or "",
                            "ai_used": int(ai_used or 0),
                            "ai_model": ai_model or "",
                            "extract_confidence": confidence,
                            "source_url_phone": src_phone or "",
                            "source_url_address": src_addr or "",
                            "source_url_rep": src_rep or "",
                            "homepage_official_flag": 0,
                            "homepage_official_source": "",
                            "homepage_official_score": 0.0,
                            "address_confidence": address_ai_confidence,
                            "address_evidence": address_ai_evidence,
                            "deep_pages_visited": 0,
                            "deep_fetch_count": 0,
                            "deep_fetch_failures": 0,
                            "deep_skip_reason": f"{skip_reason}:no_official".strip(":"),
                            "deep_urls_visited": "[]",
                            "deep_phone_candidates": 0,
                            "deep_address_candidates": 0,
                            "deep_rep_candidates": 0,
                            "top3_urls": json.dumps(top3_urls, ensure_ascii=False),
                            "exclude_reasons": exclude_reasons_json,
                            "skip_reason": skip_reason,
                            "provisional_homepage": "",
                            "final_homepage": "",
                            "deep_enabled": 0,
                            "deep_stop_reason": "no_official",
                            "timeout_stage": timeout_stage or "",
                        }
                    )
                    manager.save_company_data(company, status="no_homepage")
                    timeout_saved = True
                except Exception:
                    log.warning("[%s] no_homepage save failed", cid, exc_info=True)

            fatal_error = False
            skip_company_reason = ""
            try:
                try:
                    candidate_limit = SEARCH_CANDIDATE_LIMIT
                    company_tokens = scraper._company_tokens(name)  # type: ignore
                    try:
                        if SEARCH_PHASE_TIMEOUT_SEC > 0:
                            urls = await asyncio.wait_for(
                                scraper.search_company(name, addr, num_results=candidate_limit),
                                timeout=clamp_timeout(SEARCH_PHASE_TIMEOUT_SEC),
                            )
                        else:
                            ensure_global_time("search_company_start")
                            urls = await scraper.search_company(name, addr, num_results=candidate_limit)
                        ensure_global_time("search_company_end")
                    except asyncio.TimeoutError:
                        log.info(
                            "[%s] search_company timeout (%.1fs) -> review",
                            cid,
                            SEARCH_PHASE_TIMEOUT_SEC,
                        )
                        company.update({
                            "homepage": "",
                            "phone": "",
                            "found_address": "",
                            "rep_name": company.get("rep_name", "") or "",
                            "description": company.get("description", "") or "",
                            "listing": company.get("listing", "") or "",
                            "revenue": company.get("revenue", "") or "",
                            "profit": company.get("profit", "") or "",
                            "capital": company.get("capital", "") or "",
                            "fiscal_month": company.get("fiscal_month", "") or "",
                            "founded_year": company.get("founded_year", "") or "",
                            "homepage_official_flag": 0,
                            "homepage_official_source": "",
                            "homepage_official_score": 0.0,
                            "error_code": "search_timeout",
                        })
                        manager.save_company_data(company, status="review")
                        if csv_writer:
                            csv_writer.writerow({k: company.get(k, "") for k in CSV_FIELDNAMES})
                            csv_file.flush()
                        processed += 1
                        try:
                            await scraper.reset_context()
                        except Exception:
                            pass
                        if SLEEP_BETWEEN_SEC > 0:
                            await asyncio.sleep(jittered_seconds(SLEEP_BETWEEN_SEC, JITTER_RATIO))
                        continue
                    max_candidates = max(1, candidate_limit or 1)
                    if len(urls) > max_candidates:
                        log.info("[%s] limiting candidates to top %s (had %s)", cid, max_candidates, len(urls))
                        urls = urls[:max_candidates]
                    url_flags_map, host_flags_map = manager.get_url_flags_batch(urls)
                    exclude_reasons: dict[str, str] = {}
                    homepage = ""
                    info = None
                    primary_cands: dict[str, list[str]] = {}
                    fallback_cands: list[tuple[str, dict[str, list[str]]]] = []
                    homepage_official_flag = 0
                    homepage_official_source = ""
                    homepage_official_score = 0.0
                    ai_official_description: str | None = None
                    force_review = False
                    ai_time_spent = 0.0
                    chosen_domain_score = 0
                    search_phase_end = 0.0
                    official_phase_end = 0.0
                    deep_phase_end = 0.0
                    deep_pages_visited = 0
                    deep_fetch_count = 0
                    deep_fetch_failures = 0
                    deep_skip_reason = ""
                    deep_stop_reason = ""
                    deep_urls_visited: list[str] = []
                    deep_phone_candidates = 0
                    deep_address_candidates = 0
                    deep_rep_candidates = 0
                    # 「AI公式だが弱シグナルで公式確定できない」等のケースで、暫定URLとして保持するための退避先
                    forced_provisional_homepage = ""
                    forced_provisional_reason = ""

                    fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)

                    async def fetch_candidate_page(candidate: str, allow_slow: bool) -> Dict[str, Any]:
                        try_timeout = PAGE_FETCH_TIMEOUT_SEC * (1.0 if not allow_slow else 1.4)
                        async with fetch_sem:
                            return await asyncio.wait_for(
                                scraper.get_page_info(candidate, allow_slow=allow_slow),
                                timeout=clamp_timeout(try_timeout),
                            )

                    async def prepare_candidate(idx: int, candidate: str):
                        # 取得前は軽い正規化のみ（canonical/og:url は HTML が必要）
                        normalized_candidate = scraper.normalize_homepage_url(candidate)
                        url_for_flag = normalized_candidate or candidate
                        normalized_flag_url, host_for_flag = manager._normalize_flag_target(url_for_flag)
                        flag_info = url_flags_map.get(normalized_flag_url)
                        if not flag_info and host_for_flag:
                            flag_info = host_flags_map.get(host_for_flag)
                        domain_score_for_flag = scraper._domain_score(company_tokens, url_for_flag)  # type: ignore
                        # fetch前のURL文字列だけで弾けるものは弾く（コスト最小化）
                        try:
                            dir_hint = scraper._detect_directory_like(url_for_flag, text="", html="")  # type: ignore[attr-defined]
                            if bool(dir_hint.get("is_directory_like")) and int(dir_hint.get("directory_score") or 0) >= 8 and domain_score_for_flag < 4:
                                exclude_reasons[candidate] = "prefilter_directory_like_url"
                                log.info("[%s] URLパターンで企業DB/ディレクトリ臭が強いのでfetch前に除外: %s", cid, candidate)
                                return None
                        except Exception:
                            pass
                        if should_skip_by_url_flag(flag_info):
                            try:
                                exclude_reasons[candidate] = (
                                    f"url_flag:{(flag_info.get('judge_source') or '').strip()}:{(flag_info.get('reason') or '').strip()}"
                                ).strip(":")[:240]
                            except Exception:
                                exclude_reasons[candidate] = "url_flag"
                            log.info(
                                "[%s] 既知の非公式URLを除外: %s (domain_score=%s source=%s conf=%s reason=%s)",
                                cid,
                                candidate,
                                domain_score_for_flag,
                                flag_info.get("judge_source"),
                                flag_info.get("confidence"),
                                flag_info.get("reason"),
                            )
                            return None
                        candidate_info: Dict[str, Any] | None = None

                        async def _attempt_fetch(allow_slow: bool) -> Dict[str, Any] | None:
                            try:
                                info = await fetch_candidate_page(candidate, allow_slow=allow_slow)
                            except asyncio.TimeoutError:
                                log.info(
                                    "[%s] get_page_info timeout (allow_slow=%s) -> %s",
                                    cid,
                                    allow_slow,
                                    candidate,
                                )
                                try:
                                    http_info = await scraper._fetch_http_info(candidate)
                                    if http_info and (http_info.get("text") or http_info.get("html")):
                                        return {
                                            "url": candidate,
                                            "text": http_info.get("text", "") or "",
                                            "html": http_info.get("html", "") or "",
                                            "screenshot": b"",
                                        }
                                except Exception:
                                    pass
                                return None
                            except Exception:
                                log.warning("[%s] get_page_info failure -> %s", cid, candidate, exc_info=True)
                                return None
                            return info

                        for allow in (False, True):
                            info = await _attempt_fetch(allow)
                            if info:
                                candidate_info = info
                            if info and (info.get("text") or info.get("html")):
                                break
                        if not candidate_info:
                            return None
                        # HTML取得後に canonical/og:url を反映した正規化を再計算（追加fetchなし）
                        try:
                            normalized_candidate2 = scraper.normalize_homepage_url(candidate, candidate_info)
                        except Exception:
                            normalized_candidate2 = normalized_candidate
                        if normalized_candidate2:
                            url_for_flag = normalized_candidate2
                            normalized_flag_url, host_for_flag = manager._normalize_flag_target(url_for_flag)
                            # 旗の参照キーだけ更新（map自体は候補urlsで事前取得済みなので、無ければNoneのまま）
                            flag_info = url_flags_map.get(normalized_flag_url) or (host_flags_map.get(host_for_flag) if host_for_flag else None)
                            domain_score_for_flag = scraper._domain_score(company_tokens, url_for_flag)  # type: ignore
                        candidate_text = candidate_info.get("text", "") or ""
                        candidate_html = candidate_info.get("html") or ""
                        extracted = scraper.extract_candidates(candidate_text, candidate_html)
                        rule_details = scraper.is_likely_official_site(
                            name, candidate, candidate_info, addr, extracted, return_details=True
                        )
                        if not isinstance(rule_details, dict):
                            rule_details = {"is_official": bool(rule_details), "score": 0.0}
                        try:
                            log.info(
                                "[%s] official_candidate url=%s rule_score=%.1f evidence=%s directory=%s domain_score=%s name_ratio=%.2f exact=%s partial_only=%s pref_mismatch=%s addr_hit=%s pref_hit=%s zip_hit=%s",
                                cid,
                                candidate,
                                float(rule_details.get("score") or 0.0),
                                int(rule_details.get("official_evidence_score") or 0),
                                bool(rule_details.get("directory_like")),
                                domain_score_for_flag,
                                float(rule_details.get("name_match_ratio") or 0.0),
                                bool(rule_details.get("name_match_exact")),
                                bool(rule_details.get("name_match_partial_only")),
                                bool(rule_details.get("prefecture_mismatch")),
                                bool(rule_details.get("address_match")),
                                bool(rule_details.get("prefecture_match")),
                                bool(rule_details.get("postal_code_match")),
                            )
                        except Exception:
                            pass
                        return (
                            idx,
                            {
                                "url": candidate,
                                "normalized_url": url_for_flag,
                                "info": candidate_info,
                                "extracted": extracted,
                                "rule": rule_details,
                                "flag_info": flag_info,
                                "order_idx": idx,
                                "search_rank": idx,
                            },
                        )

                    async def _prepare_batch(
                        pairs: list[tuple[int, str]],
                        deadline: float | None,
                    ) -> tuple[list[dict[str, Any]], bool]:
                        prepared: list[Any] = []
                        prepare_timed_out = False
                        if not pairs:
                            return [], False
                        prepare_tasks = [
                            asyncio.create_task(prepare_candidate(idx, candidate))
                            for idx, candidate in pairs
                        ]
                        pending: set[asyncio.Task] = set(prepare_tasks)
                        try:
                            while pending:
                                if deadline is not None:
                                    remaining = deadline - time.monotonic()
                                    if remaining <= 0:
                                        prepare_timed_out = True
                                        break
                                else:
                                    remaining = None
                                done, pending = await asyncio.wait(
                                    pending,
                                    timeout=remaining,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                if not done:
                                    prepare_timed_out = True
                                    break
                                for task in done:
                                    try:
                                        result = task.result()
                                    except Exception:
                                        continue
                                    prepared.append(result)
                                if len(prepared) >= len(pairs):
                                    pending.clear()
                                    break
                        finally:
                            if pending:
                                for task in pending:
                                    task.cancel()
                                await asyncio.gather(*pending, return_exceptions=True)
                                prepare_timed_out = True
                        ordered: list[tuple[int, dict[str, Any]]] = []
                        for result in prepared:
                            if isinstance(result, Exception) or not result:
                                continue
                            ordered.append(result)
                        ordered.sort(key=lambda x: x[0])
                        records = [record for _, record in ordered]
                        if prepare_timed_out and records:
                            log.info(
                                "[%s] prepare_candidate partial timeout (recorded %d/%d)",
                                cid,
                                len(records),
                                len(prepared),
                            )
                        return records, prepare_timed_out

                    def _postprocess_candidates(records: list[dict[str, Any]]) -> None:
                        for record in records:
                            normalized_url = record.get("normalized_url") or record.get("url") or ""
                            domain_score_val = scraper._domain_score(company_tokens, normalized_url)  # type: ignore
                            host_token_hit = scraper._host_token_hit(company_tokens, normalized_url)  # type: ignore
                            record["domain_score"] = domain_score_val
                            record["host_token_hit"] = host_token_hit
                            # 公式候補の強さを上げる: 社名トークンがホストに入りドメインスコアが高ければ AI 否定を上書きできるようにする
                            record["strong_domain_host"] = (domain_score_val >= 5 or (company_has_corp and domain_score_val >= 4)) and host_token_hit
                            record.setdefault("order_idx", 0)
                            record.setdefault("search_rank", 0)

                    candidate_records: list[dict[str, Any]] = []
                    prepare_timed_out = False
                    url_pairs = list(enumerate(urls))
                    first_pairs = url_pairs[:3]
                    remaining_pairs = url_pairs[3:]
                    search_deadline = time.monotonic() + SEARCH_PHASE_TIMEOUT_SEC if SEARCH_PHASE_TIMEOUT_SEC > 0 else None
                    initial_records, timed_out = await _prepare_batch(first_pairs, search_deadline)
                    candidate_records.extend(initial_records)
                    prepare_timed_out = prepare_timed_out or timed_out
                    search_phase_end = elapsed()

                    if prepare_timed_out and not candidate_records:
                        log.info("[%s] prepare_candidate timeout -> review/search_timeout", cid)

                    if candidate_records:
                        _postprocess_candidates(candidate_records)
                        candidate_records.sort(
                            key=lambda rec: (
                                not rec.get("strong_domain_host", False),
                                -int(rec.get("domain_score") or 0),
                                rec.get("order_idx", 0),
                            )
                        )
                        top3_ranked = sorted(candidate_records, key=lambda r: r.get("search_rank", 1e9))[:3]
                        for r in top3_ranked:
                            r["force_ai_official"] = True

                    if over_fetch_limit() or over_time_limit():
                        timed_out = True
                        if over_hard_deadline():
                            raise_hard_timeout("candidate_phase")
                    if homepage and over_after_official():
                        timed_out = True

                    if not candidate_records:
                        company.update({
                            "homepage": "",
                            "phone": "",
                            "found_address": "",
                            "rep_name": company.get("rep_name", "") or "",
                            "description": company.get("description", "") or "",
                            "listing": company.get("listing", "") or "",
                            "revenue": company.get("revenue", "") or "",
                            "profit": company.get("profit", "") or "",
                            "capital": company.get("capital", "") or "",
                            "fiscal_month": company.get("fiscal_month", "") or "",
                            "founded_year": company.get("founded_year", "") or "",
                            "homepage_official_flag": 0,
                            "homepage_official_source": "",
                            "homepage_official_score": 0.0,
                            "error_code": "search_timeout",
                        })
                        manager.save_company_data(company, status="review")
                        log.info("[%s] 候補ゼロ -> review/search_timeout で保存", cid)
                        if csv_writer:
                            csv_writer.writerow({k: company.get(k, "") for k in CSV_FIELDNAMES})
                            csv_file.flush()
                        processed += 1
                        if SLEEP_BETWEEN_SEC > 0:
                            await asyncio.sleep(jittered_seconds(SLEEP_BETWEEN_SEC, JITTER_RATIO))
                        continue

                    ensure_global_time("after_candidate_records")
                    ai_official_attempted = False
                    selected_candidate_record: dict[str, Any] | None = None
                    ai_official_rejected_record: dict[str, Any] | None = None
                    ai_official_rejected_conf: float = 0.0
                    ai_official_rejected_reason: str = ""
                    ai_official_enabled = bool(
                        USE_AI_OFFICIAL and USE_AI and verifier is not None and hasattr(verifier, "judge_official_homepage")
                    )
                    ai_official_primary = bool(AI_OFFICIAL_PRIMARY and ai_official_enabled)
                    processed_urls: set[str] = set()
                    fetched_remaining = False
                    while True:
                        if ai_official_enabled:
                            ai_tasks: list[asyncio.Task] = []
                            ai_sem = asyncio.Semaphore(AI_OFFICIAL_CONCURRENCY)

                            async def run_official_ai(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
                                nonlocal ai_official_attempted, ai_time_spent
                                record["ai_checked"] = True
                                async with ai_sem:
                                    remaining = remaining_time_budget()
                                    if remaining <= AI_MIN_REMAINING_SEC:
                                        log.info(
                                            "[%s] skip AI公式判定（残り%.1fs）: %s",
                                            cid,
                                            max(0.0, remaining),
                                            record.get("url"),
                                        )
                                        return record, None
                                    normalized_for_ai = record.get("normalized_url") or record.get("url") or ""
                                    domain_score = int(record.get("domain_score") or 0)
                                    if normalized_for_ai and domain_score == 0:
                                        domain_score = scraper._domain_score(company_tokens, normalized_for_ai)  # type: ignore
                                        record["domain_score"] = domain_score
                                    info_payload = record.get("info") or {}
                                    rule_for_ai = record.get("rule") or {}
                                    allow_slow_ai = False
                                    if remaining > (AI_MIN_REMAINING_SEC + 2.0):
                                        evidence_score = int(rule_for_ai.get("official_evidence_score") or 0)
                                        allow_slow_ai = bool(
                                            rule_for_ai.get("is_official")
                                            or evidence_score >= 9
                                            or record.get("host_token_hit")
                                        )
                                    info_payload = await ensure_info_text(
                                        scraper,
                                        record.get("url"),
                                        info_payload,
                                        allow_slow=allow_slow_ai,
                                    )
                                    if OFFICIAL_AI_USE_SCREENSHOT:
                                        info_payload = await ensure_info_has_screenshot(
                                            scraper,
                                            record.get("url"),
                                            info_payload,
                                            need_screenshot=True,
                                        )
                                    record["info"] = info_payload
                                    remaining = remaining_time_budget()
                                    if remaining <= AI_MIN_REMAINING_SEC:
                                        log.info(
                                            "[%s] skip AI公式判定（残り%.1fs, info取得後）: %s",
                                            cid,
                                            max(0.0, remaining),
                                            record.get("url"),
                                        )
                                        return record, None
                                    pages_for_ai: list[dict[str, Any]] = []
                                    base_url = normalized_for_ai or record.get("url") or ""
                                    base_text = info_payload.get("text", "") or ""
                                    base_html = info_payload.get("html", "") or ""
                                    try:
                                        base_pt = scraper.classify_page_type(
                                            base_url, text=base_text, html=base_html
                                        ).get("page_type") or "OTHER"
                                    except Exception:
                                        base_pt = "OTHER"
                                    evidence_list = rule_for_ai.get("official_evidence") or []
                                    evidence_set = {str(x).strip() for x in evidence_list if str(x).strip()}
                                    title_match = None
                                    if "title" in evidence_set:
                                        title_match = "strong"
                                    elif "title_partial" in evidence_set:
                                        title_match = "partial"
                                    h1_match = None
                                    if "h1" in evidence_set:
                                        h1_match = "strong"
                                    elif "h1_partial" in evidence_set:
                                        h1_match = "partial"
                                    signals = {
                                        "page_type": base_pt,
                                        "domain_score": int(record.get("domain_score") or 0),
                                        "host_token_hit": bool(record.get("host_token_hit")),
                                        "name_match_ratio": rule_for_ai.get("name_match_ratio"),
                                        "name_match_exact": rule_for_ai.get("name_match_exact"),
                                        "name_match_partial_only": rule_for_ai.get("name_match_partial_only"),
                                        "name_match_source": rule_for_ai.get("name_match_source"),
                                        "official_evidence_score": rule_for_ai.get("official_evidence_score"),
                                        "official_evidence": evidence_list if evidence_set else None,
                                        "title_match": title_match,
                                        "h1_match": h1_match,
                                        "og_site_name_match": True if "og:site_name" in evidence_set else None,
                                        "directory_like": rule_for_ai.get("directory_like"),
                                    }
                                    base_snippet_full = build_official_ai_text(base_text, base_html, signals=signals)
                                    base_snippet = base_snippet_full[:1800] if len(base_snippet_full) > 1800 else base_snippet_full
                                    pages_for_ai.append(
                                        {
                                            "url": base_url,
                                            "page_type": base_pt,
                                            "snippet": base_snippet,
                                            "screenshot": info_payload.get("screenshot"),
                                        }
                                    )
                                    priority_docs: dict[str, dict[str, Any]] = {}
                                    if remaining_time_budget() > AI_MIN_REMAINING_SEC:
                                        try:
                                            priority_docs = await scraper.fetch_priority_documents(
                                                base_url or record.get("url"),
                                                base_html,
                                                max_links=2,
                                                concurrency=FETCH_CONCURRENCY,
                                                target_types=["about", "contact"],
                                                exclude_urls={base_url} if base_url else None,
                                            )
                                        except Exception:
                                            priority_docs = {}
                                    for url, pdata in priority_docs.items():
                                        page_info = {
                                            "url": url,
                                            "text": pdata.get("text", "") or "",
                                            "html": pdata.get("html", "") or "",
                                        }
                                        ptext = page_info.get("text", "") or ""
                                        phtml = page_info.get("html", "") or ""
                                        try:
                                            pt = scraper.classify_page_type(url, text=ptext, html=phtml).get("page_type") or "OTHER"
                                        except Exception:
                                            pt = "OTHER"
                                        snippet_full = build_official_ai_text(ptext, phtml)
                                        snippet = snippet_full[:1800] if len(snippet_full) > 1800 else snippet_full
                                        pages_for_ai.append(
                                            {
                                                "url": url,
                                                "page_type": pt,
                                                "snippet": snippet,
                                                "screenshot": None,
                                            }
                                        )
                                    pages_for_ai = [p for p in pages_for_ai if p.get("snippet")] or pages_for_ai
                                    if len(pages_for_ai) > 3:
                                        pages_for_ai = pages_for_ai[:3]
                                    ai_started = time.monotonic()
                                    try:
                                        if remaining_time_budget() <= AI_MIN_REMAINING_SEC:
                                            return record, None
                                        if hasattr(verifier, "judge_official_homepage_multi") and pages_for_ai:
                                            ai_verdict = await asyncio.wait_for(
                                                verifier.judge_official_homepage_multi(
                                                    pages_for_ai,
                                                    name,
                                                    addr,
                                                    base_url or record.get("url"),
                                                ),
                                                timeout=clamp_timeout(max(ai_call_timeout, 5.0)),
                                            )
                                        else:
                                            ai_verdict = await asyncio.wait_for(
                                                verifier.judge_official_homepage(
                                                    base_snippet_full,
                                                    info_payload.get("screenshot"),
                                                    name,
                                                    addr,
                                                    record.get("normalized_url") or record.get("url"),
                                                ),
                                                timeout=clamp_timeout(max(ai_call_timeout, 5.0)),
                                            )
                                    except Exception:
                                        log.warning("[%s] AI公式判定失敗: %s", cid, record.get("url"), exc_info=True)
                                        return record, None
                                    ai_time_spent += time.monotonic() - ai_started
                                    ai_official_attempted = True
                                    # AI公式のうち、ドメイン/ルール根拠が弱いものは「確定公式には使わない」が、
                                    # deep起点としては保持する（除外扱いに落とさない）。
                                    if ai_verdict and ai_verdict.get("is_official") and domain_score < 4 and not record.get("rule", {}).get("address_match"):
                                        rule = record.get("rule", {}) or {}
                                        evidence_score = int(rule.get("official_evidence_score") or 0)
                                        directory_like = bool(rule.get("directory_like"))
                                        if directory_like or evidence_score < 9:
                                            ai_verdict["weak_signals"] = True
                                    return record, ai_verdict

                            ranked_for_ai_official = sorted(
                                candidate_records,
                                key=lambda r: (int(r.get("search_rank", 1_000_000_000) or 1_000_000_000), int(r.get("order_idx", 1_000_000_000) or 1_000_000_000)),
                            )
                            if not AI_OFFICIAL_ALL_CANDIDATES:
                                ranked_for_ai_official = ranked_for_ai_official[:3]
                            elif AI_OFFICIAL_CANDIDATE_LIMIT > 0:
                                ranked_for_ai_official = ranked_for_ai_official[:AI_OFFICIAL_CANDIDATE_LIMIT]
                            for record in ranked_for_ai_official:
                                if not record_needs_official_ai(record):
                                    continue
                                if remaining_time_budget() <= AI_MIN_REMAINING_SEC:
                                    break
                                ai_tasks.append(asyncio.create_task(run_official_ai(record)))

                            if ai_tasks and not ai_official_primary:
                                results = await asyncio.gather(*ai_tasks, return_exceptions=True)
                                for res in results:
                                    if not isinstance(res, tuple) or len(res) != 2:
                                        continue
                                    rec, verdict = res
                                    if verdict:
                                        rec["ai_judge"] = verdict
                            if ai_official_primary:
                                for record in ranked_for_ai_official:
                                    if not record_needs_official_ai(record):
                                        continue
                                    if remaining_time_budget() <= AI_MIN_REMAINING_SEC:
                                        break
                                    rec, verdict = await run_official_ai(record)
                                    if verdict:
                                        rec["ai_judge"] = verdict
                                        if ai_official_hint_from_judge(verdict, AI_VERIFY_MIN_CONFIDENCE):
                                            log.info("[%s] AI公式判定で早期確定候補を取得 -> 以降のAI判定を省略", cid)
                                            break

                            # AI公式が複数出るケースに備え、AI判定が強い候補を先に評価する
                            def _ai_select_score(rec: dict[str, Any]) -> float:
                                aj = rec.get("ai_judge") or {}
                                if not isinstance(aj, dict):
                                    aj = {}
                                ai_is = aj.get("is_official_site")
                                if ai_is is None:
                                    ai_is = aj.get("is_official")
                                conf = aj.get("official_confidence")
                                if conf is None:
                                    conf = aj.get("confidence")
                                try:
                                    conf_f = float(conf) if conf is not None else 0.0
                                except Exception:
                                    conf_f = 0.0
                                rd = rec.get("rule") or {}
                                evidence = float(rd.get("official_evidence_score") or 0.0)
                                domain = float(rec.get("domain_score") or 0.0)
                                directory_like = bool(rd.get("directory_like"))
                                host_token_hit = bool(rec.get("host_token_hit"))
                                strong_domain_host = bool(rec.get("strong_domain_host"))
                                name_present = bool(rd.get("name_present"))
                                # 公式と判定されたものを優先しつつ、低confは過信しない
                                score = 0.0
                                if ai_is is True and conf_f >= AI_VERIFY_MIN_CONFIDENCE:
                                    score += 1000.0 + conf_f * 100.0
                                elif ai_is is True:
                                    score += conf_f * 10.0
                                elif ai_is is False:
                                    score -= 200.0
                                score += domain * 5.0 + evidence * 2.0
                                score += 20.0 if host_token_hit else 0.0
                                score += 12.0 if strong_domain_host else 0.0
                                score += 10.0 if name_present else 0.0
                                score -= 500.0 if directory_like else 0.0
                                return score

                            candidate_records.sort(
                                key=lambda r: (
                                    -_ai_select_score(r),
                                    int(r.get("search_rank", 1_000_000_000) or 1_000_000_000),
                                    int(r.get("order_idx", 1_000_000_000) or 1_000_000_000),
                                )
                            )

                        for record in candidate_records:
                            normalized_url = record.get("normalized_url") or record.get("url")
                            if normalized_url and normalized_url in processed_urls:
                                continue
                            if normalized_url:
                                processed_urls.add(normalized_url)
                            extracted = record.get("extracted") or {}
                            rule_details = record.get("rule") or {}
                            domain_score = int(record.get("domain_score") or 0)
                            if normalized_url and domain_score == 0:
                                domain_score = scraper._domain_score(company_tokens, normalized_url)  # type: ignore
                                record["domain_score"] = domain_score
                            host_token_hit = bool(record.get("host_token_hit"))
                            strong_domain_host = bool(record.get("strong_domain_host"))
                            addr_hit = bool(rule_details.get("address_match"))
                            pref_hit = bool(rule_details.get("prefecture_match"))
                            zip_hit = bool(rule_details.get("postal_code_match"))
                            # ドメイン一致だけで「社名一致」と扱うと誤採用しやすいので、ページ内の社名シグナルを重視する
                            name_present = bool(rule_details.get("name_present"))
                            name_match_exact = bool(rule_details.get("name_match_exact"))
                            name_match_partial_only = bool(rule_details.get("name_match_partial_only"))
                            try:
                                name_match_ratio = float(rule_details.get("name_match_ratio") or 0.0)
                            except Exception:
                                name_match_ratio = 0.0
                            name_match_source = str(rule_details.get("name_match_source") or "")
                            high_signal_sources = {"title", "h1", "og_site_name", "og_title", "app_name"}
                            official_evidence = rule_details.get("official_evidence") or []
                            strong_name_hit = bool(
                                name_match_exact
                                or ("jsonld:org_name" in official_evidence)
                                or ("h1" in official_evidence)
                                or ("title" in official_evidence)
                                or (
                                    name_match_ratio >= 0.92
                                    and not name_match_partial_only
                                    and name_match_source in high_signal_sources
                                )
                            )
                            name_hit = strong_name_hit
                            pref_only_ok = bool(
                                input_addr_pref_only
                                and pref_hit
                                and (name_hit or strong_domain_host or host_token_hit or domain_score >= 4)
                            )
                            address_ok = bool(addr) and (addr_hit or zip_hit or pref_only_ok)
                            evidence_score = int(rule_details.get("official_evidence_score") or 0)
                            directory_like = bool(rule_details.get("directory_like"))
                            host_name, _, allowed_tld, whitelist_host, _ = CompanyScraper._allowed_official_host(normalized_url or "")
                            content_strong = name_hit and (address_ok or evidence_score >= 9)
                            ai_judge = record.get("ai_judge")
                            flag_info = record.get("flag_info")


                            ai_is_official = None
                            ai_is_official_effective = None
                            ai_conf_f = 0.0
                            ai_official_hint = False

                            if ai_judge:
                                ai_is_official = ai_judge.get("is_official_site")
                                if ai_is_official is None:
                                    ai_is_official = ai_judge.get("is_official")
                                ai_conf = ai_judge.get("official_confidence")
                                if ai_conf is None:
                                    ai_conf = ai_judge.get("confidence")
                                try:
                                    ai_conf_f = float(ai_conf) if ai_conf is not None else 0.0
                                except Exception:
                                    ai_conf_f = 0.0
                                ai_official_hint = ai_official_hint_from_judge(ai_judge, AI_VERIFY_MIN_CONFIDENCE)
                                if ai_is_official is True:
                                    ai_is_official_effective = True
                                elif ai_is_official is False and ai_conf_f >= AI_VERIFY_MIN_CONFIDENCE:
                                    ai_is_official_effective = False
                                # AI negative with high confidence can exclude
                                if ai_is_official_effective is False:
                                    name_signal_ok = bool(
                                        strong_name_hit
                                        or (name_match_ratio >= 0.85 and not name_match_partial_only)
                                    )
                                    rule_strong_for_conflict = bool(
                                        content_strong
                                        or host_token_hit
                                        or strong_domain_host
                                        or rule_details.get("strong_domain")
                                        or domain_score >= 5
                                        or evidence_score >= 10
                                        or (name_signal_ok and evidence_score >= 3)
                                    )
                                    if not rule_strong_for_conflict:
                                        manager.upsert_url_flag(
                                            normalized_url,
                                            is_official=False,
                                            source="ai",
                                            reason=ai_judge.get("reason", "") or "ai_not_official",
                                            confidence=ai_conf_f,
                                        )
                                        fallback_cands.append((record.get("url"), extracted))
                                        log.info(
                                            "[%s] AI reject: %s (is_official=%s confidence=%.2f)",
                                            cid,
                                            record.get("url"),
                                            ai_is_official,
                                            ai_conf_f,
                                        )
                                        continue
                                    # AI negative conflict: keep as review-only
                                    record["ai_conflict"] = True
                                    record["ai_conflict_confidence"] = ai_conf_f
                                    force_review = True
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=False,
                                        source="ai_conflict",
                                        reason=ai_judge.get("reason", "") or "ai_not_official_rule_conflict",
                                        confidence=ai_conf_f,
                                    )
                                    fallback_cands.append((record.get("url"), extracted))
                                    log.info(
                                        "[%s] AI negative conflict (review): %s (domain=%s evidence=%s conf=%.2f)",
                                        cid,
                                        record.get("url"),
                                        domain_score,
                                        evidence_score,
                                        ai_conf_f,
                                    )
                                    if ai_official_primary:
                                        continue
                                elif ai_is_official is not None and ai_conf_f < AI_VERIFY_MIN_CONFIDENCE:
                                    # Low confidence: keep for rule evaluation
                                    record["ai_low_confidence"] = True

                            if directory_like and not ai_is_official_effective:
                                manager.upsert_url_flag(
                                    normalized_url,
                                    is_official=False,
                                    source="rule",
                                    reason="directory_like",
                                    confidence=rule_details.get("directory_score"),
                                )
                                fallback_cands.append((record.get("url"), extracted))
                                log.info("[%s] directory_like -> skip: %s", cid, record.get("url"))
                                continue

                            fast_phone_hit = bool(extracted.get("phone_numbers"))
                            fast_address_ok = address_ok or bool(rule_details.get("address_match"))
                            fast_domain_ok = (
                                domain_score >= 4
                                or host_token_hit
                                or strong_domain_host
                                or rule_details.get("strong_domain")
                            )
                            if ai_is_official_effective is True:
                                candidate_url = normalized_url
                                candidate_source = "ai_fast" if fast_domain_ok else "ai_review"
                                candidate_score = float(rule_details.get("score") or 0.0)
                                info = record.get("info")
                                primary_cands = extracted
                                homepage = candidate_url
                                homepage_official_flag = 1
                                homepage_official_source = candidate_source
                                homepage_official_score = candidate_score
                                ai_official_description = ai_judge.get("description") if isinstance(ai_judge, dict) else None
                                chosen_domain_score = domain_score
                                selected_candidate_record = record
                                if not fast_domain_ok or (addr and not address_ok):
                                    force_review = True
                                manager.upsert_url_flag(
                                    candidate_url,
                                    is_official=True,
                                    source=homepage_official_source,
                                    reason=ai_judge.get("reason", "") if isinstance(ai_judge, dict) else "",
                                    confidence=ai_judge.get("confidence") if isinstance(ai_judge, dict) else None,
                                )
                                log.info(
                                    "[%s] AI official selected: %s (source=%s domain=%s host=%s addr=%s phone=%s review=%s)",
                                    cid,
                                    candidate_url,
                                    homepage_official_source,
                                    domain_score,
                                    host_token_hit,
                                    fast_address_ok,
                                    fast_phone_hit,
                                    force_review,
                                )
                                break

                            brand_allowed = (allowed_tld or whitelist_host) and not host_token_hit
                            if (
                                not homepage
                                and brand_allowed
                                and content_strong
                                and not flag_info
                            ):
                                homepage = normalized_url
                                info = record.get("info")
                                primary_cands = extracted
                                # AI公式判定が使える場合、この分岐だけで公式確定しない（review候補として保持）
                                homepage_official_flag = 0 if ai_official_primary else 1
                                homepage_official_source = "name_addr"
                                homepage_official_score = float(rule_details.get("score") or 0.0)
                                chosen_domain_score = domain_score
                                selected_candidate_record = record
                                force_review = True
                                if record.get("ai_conflict"):
                                    homepage_official_source = "name_addr_ai_conflict"
                                if not ai_official_primary:
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=True,
                                        source="name_addr",
                                        reason="name_address_match_brand_host",
                                        confidence=rule_details.get("score"),
                                    )
                                log.info(
                                    "[%s] 名前+住所一致（ブランドドメイン）でreview候補保存: %s host=%s",
                                    cid,
                                    normalized_url,
                                    host_name,
                                )
                                break

                            # 社名トークンなし＆住所一致だけの候補は公式扱いしない
                            if not host_token_hit and not name_hit and address_ok:
                                manager.upsert_url_flag(
                                    normalized_url,
                                    is_official=False,
                                    source="rule",
                                    reason="address_only_no_name_host",
                                )
                                fallback_cands.append((record.get("url"), extracted))
                                log.info("[%s] ホスト社名なし・名称一致なし・住所一致のみのため非公式扱い: %s", cid, record.get("url"))
                                continue
                            if rule_details.get("blocked_host"):
                                manager.upsert_url_flag(
                                    normalized_url,
                                    is_official=False,
                                    source="rule",
                                    reason=f"blocked_host:{rule_details.get('host', '')}",
                                    scope="host",
                                )
                                fallback_cands.append((record.get("url"), extracted))
                                log.info("[%s] 除外ホスト(%s)をスキップ: %s", cid, rule_details.get("host"), record.get("url"))
                                continue
                            # キャッシュ公式は採用しない（参考のみ）
                            if rule_details.get("is_official"):
                                if input_addr_pref_only and not (host_token_hit or name_hit or strong_domain_host or domain_score >= 4):
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=False,
                                        source="rule",
                                        reason="pref_only_input_no_name_host",
                                    )
                                    fallback_cands.append((record.get("url"), extracted))
                                    log.info("[%s] 公式判定だが都道府県だけの入力で社名/ホスト根拠なしのため除外: %s", cid, record.get("url"))
                                    continue
                                if addr and not address_ok:
                                    # 住所根拠なしなら review 送りでURLは維持
                                    force_review = True
                                    log.info("[%s] 公式判定だが入力住所と一致せず -> review: %s", cid, record.get("url"))
                                if not host_token_hit and not address_ok:
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=False,
                                        source="rule",
                                        reason="host_no_name_no_address",
                                    )
                                    fallback_cands.append((record.get("url"), extracted))
                                    log.info("[%s] 公式判定でもホストに社名なし・住所根拠なしのため除外: %s", cid, record.get("url"))
                                    continue
                                if not host_token_hit and not name_hit:
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=False,
                                        source="rule",
                                        reason="no_host_token_no_name",
                                    )
                                    fallback_cands.append((record.get("url"), extracted))
                                    log.info("[%s] 名称/ホストトークンなしのため公式判定を除外: %s", cid, record.get("url"))
                                    continue
                                name_or_domain_ok = (
                                    name_hit
                                    or strong_domain_host
                                    or rule_details.get("strong_domain")
                                    or domain_score >= 5
                                )
                                if not (name_or_domain_ok or address_ok):
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=False,
                                        source="rule",
                                        reason="name_domain_mismatch",
                                    )
                                    fallback_cands.append((record.get("url"), extracted))
                                    log.info("[%s] 名称/ドメイン一致弱のため公式判定を除外: %s", cid, record.get("url"))
                                    continue
                                # 低ドメイン一致は name/address/host が強い場合のみ採用
                                if domain_score < 3 and not strong_domain_host and not rule_details.get("strong_domain") and not address_ok and not name_hit and not host_token_hit:
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=False,
                                        source="rule",
                                        reason=f"weak_domain_score={domain_score}",
                                    )
                                    fallback_cands.append((record.get("url"), extracted))
                                    log.info("[%s] 低ドメイン一致のため公式判定を見送り: %s", cid, record.get("url"))
                                    continue
                                if not address_ok and not name_hit:
                                    force_review = True
                                    log.info("[%s] 名称/住所一致弱いが公式扱い→review: %s", cid, record.get("url"))
                                homepage = normalized_url
                                info = record.get("info")
                                primary_cands = extracted
                                selected_candidate_record = record
                                # AI公式判定が使える場合 rule だけで公式確定しない（AI公式の方で採用される）
                                homepage_official_flag = 0 if ai_official_primary else 1
                                homepage_official_source = "rule" if not ai_official_primary else "rule_review"
                                homepage_official_score = float(rule_details.get("score") or 0.0)
                                chosen_domain_score = domain_score
                                if ai_official_primary:
                                    force_review = True
                                if record.get("ai_conflict"):
                                    homepage_official_source = "rule_ai_conflict" if not ai_official_primary else "rule_review_ai_conflict"
                                    force_review = True
                                free_host = _is_free_host(homepage)
                                if (free_host and evidence_score < 10) or not _official_signal_ok(
                                    host_token_hit=host_token_hit,
                                    strong_domain_host=strong_domain_host,
                                    domain_score=domain_score,
                                    name_hit=name_hit,
                                    address_ok=address_ok,
                                    official_evidence_score=evidence_score,
                                ):
                                    homepage_official_flag = 0
                                    homepage_official_source = "provisional_freehost"
                                    force_review = True
                                    if normalized_url:
                                        forced_provisional_homepage = normalized_url
                                        forced_provisional_reason = "free_host_or_weak_signals"
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=False,
                                        source="rule",
                                        reason="free_host_or_weak_signals",
                                    )
                                if not ai_official_primary:
                                    manager.upsert_url_flag(
                                        normalized_url,
                                        is_official=True,
                                        source="rule",
                                        reason=f"score={rule_details.get('score', 0.0):.1f}",
                                    )
                                break
                            score_val = float(rule_details.get("score") or 0.0)
                            if score_val <= 1 and not rule_details.get("strong_domain"):
                                manager.upsert_url_flag(
                                    normalized_url,
                                    is_official=False,
                                    source="rule",
                                    reason=f"score={score_val:.1f}",
                                )
                                fallback_cands.append((record.get("url"), extracted))
                            log.info("[%s] 非公式と判断: %s", cid, record.get("url"))

                        if homepage:
                            break
                        if fetched_remaining or not remaining_pairs:
                            break
                        if prepare_timed_out or over_fetch_limit() or over_time_limit():
                            break
                        fetched_remaining = True
                        more_records, more_timed_out = await _prepare_batch(remaining_pairs, search_deadline)
                        remaining_pairs = []
                        if more_records:
                            _postprocess_candidates(more_records)
                            candidate_records.extend(more_records)
                            candidate_records.sort(
                                key=lambda rec: (
                                    not rec.get('strong_domain_host', False),
                                    -int(rec.get('domain_score') or 0),
                                    rec.get('order_idx', 0),
                                )
                            )
                            top3_ranked = sorted(candidate_records, key=lambda r: r.get('search_rank', 1e9))[:3]
                            for r in top3_ranked:
                                r['force_ai_official'] = True
                        prepare_timed_out = prepare_timed_out or more_timed_out
                        search_phase_end = elapsed()
                        continue
                    provisional_homepage = ""
                    provisional_info = None
                    provisional_cands: dict[str, list[str]] = {}
                    provisional_domain_score = 0
                    provisional_host_token = False
                    provisional_name_present = False
                    provisional_address_ok = False
                    provisional_ai_hint = False
                    provisional_profile_hit = False
                    provisional_evidence_score = 0
                    best_record: dict[str, Any] | None = None
                    if not homepage and candidate_records:
                        best_score = float("-inf")
                        for record in candidate_records:
                            normalized_url = record.get("normalized_url") or record.get("url")
                            rule_details = record.get("rule") or {}
                            if rule_details.get("directory_like"):
                                continue
                            domain_score = int(record.get("domain_score") or 0)
                            if normalized_url and domain_score == 0:
                                domain_score = scraper._domain_score(company_tokens, normalized_url)  # type: ignore
                                record["domain_score"] = domain_score
                            host_token_hit = bool(record.get("host_token_hit"))
                            name_present = bool(rule_details.get("name_present"))
                            strong_domain_host = bool(record.get("strong_domain_host"))
                            addr_hit = bool(rule_details.get("address_match"))
                            pref_hit = bool(rule_details.get("prefecture_match"))
                            zip_hit = bool(rule_details.get("postal_code_match"))
                            pref_only_ok = bool(
                                input_addr_pref_only
                                and pref_hit
                                and (name_present or strong_domain_host or host_token_hit or domain_score >= 4)
                            )
                            address_ok = bool(addr) and (addr_hit or zip_hit or pref_only_ok)
                            evidence_score = int(rule_details.get("official_evidence_score") or 0)
                            ai_bonus = 0.0
                            aj = record.get("ai_judge")
                            ai_official_hint = False
                            if aj:
                                is_official_site = aj.get("is_official_site")
                                if is_official_site is None:
                                    is_official_site = aj.get("is_official")
                                conf = aj.get("official_confidence")
                                if conf is None:
                                    conf = aj.get("confidence")
                                try:
                                    conf_f = float(conf) if conf is not None else 0.0
                                except Exception:
                                    conf_f = 0.0
                                ai_official_hint = ai_official_hint_from_judge(aj, AI_VERIFY_MIN_CONFIDENCE)
                                if ai_official_hint:
                                    ai_bonus = 6.0
                            if ai_official_rejected_record is record:
                                ai_bonus = max(ai_bonus, 6.0)
                            allow_without_host = (
                                strong_domain_host
                                or name_present
                                or domain_score >= 4
                                or (address_ok and domain_score >= 3)
                                or evidence_score >= 10
                            )
                            # AI公式ヒントは「除外」ではなく暫定候補として保持する（誤爆回避のため directory_like は除外済み）
                            if not host_token_hit and not allow_without_host and not ai_official_hint:
                                continue
                            score = (
                                domain_score * 2
                                + (3 if address_ok else 0)
                                + float(rule_details.get("score") or 0.0)
                                + min(6.0, evidence_score / 2.0)
                                + (4 if strong_domain_host else 0)
                                + ai_bonus
                            )
                            if score > best_score:
                                best_score = score
                                best_record = record
                        if best_record:
                            normalized_url = best_record.get("normalized_url") or best_record.get("url") or ""
                            rule_details = best_record.get("rule") or {}
                            domain_score = int(best_record.get("domain_score") or 0)
                            if normalized_url and domain_score == 0:
                                domain_score = scraper._domain_score(company_tokens, normalized_url)  # type: ignore
                                best_record["domain_score"] = domain_score
                            strong_domain_host = bool(best_record.get("strong_domain_host"))
                            name_present = bool(rule_details.get("name_present"))
                            strong_domain = bool(rule_details.get("strong_domain"))
                            addr_hit = bool(rule_details.get("address_match"))
                            pref_hit = bool(rule_details.get("prefecture_match"))
                            zip_hit = bool(rule_details.get("postal_code_match"))
                            address_ok = addr_hit or pref_hit or zip_hit
                            provisional_host_token = bool(best_record.get("host_token_hit"))
                            provisional_name_present = name_present
                            allow_without_host = (
                                provisional_name_present
                                or strong_domain_host
                                or domain_score >= 4
                                or (address_ok and domain_score >= 3)
                            )
                            aj = best_record.get("ai_judge") if isinstance(best_record.get("ai_judge"), dict) else None
                            provisional_ai_hint = ai_official_hint_from_judge(aj, AI_VERIFY_MIN_CONFIDENCE)

                            # 完全に根拠が無い暫定URLのみ破棄（ただしAI公式ヒントは暫定として保持）
                            if not provisional_host_token and not allow_without_host and not provisional_ai_hint:
                                log.info("[%s] 社名トークン/名称/強ドメイン/住所一致なしのため暫定URL候補を破棄: %s", cid, normalized_url)
                                best_record = None
                                provisional_homepage = ""
                                provisional_info = None
                                provisional_cands = {}
                                provisional_domain_score = 0
                                provisional_address_ok = False
                                break
                            # 公式昇格の予備候補だが保存はしない。強条件のみ後で昇格。
                            provisional_homepage = normalized_url
                            provisional_info = best_record.get("info")
                            provisional_cands = best_record.get("extracted") or {}
                            provisional_domain_score = domain_score
                            provisional_address_ok = address_ok
                            provisional_evidence_score = int(rule_details.get("official_evidence_score") or 0)
                            if provisional_info:
                                try:
                                    pt = scraper.classify_page_type(
                                        normalized_url,
                                        text=provisional_info.get("text", "") or "",
                                        html=provisional_info.get("html", "") or "",
                                    ).get("page_type") or "OTHER"
                                    page_type_per_url[normalized_url] = str(pt)
                                    provisional_profile_hit = (pt == "COMPANY_PROFILE")
                                except Exception:
                                    pass
                            # ログだけ出して深掘りターゲットとする
                            if provisional_ai_hint:
                                company["provisional_reason"] = "ai_official_hint"
                            log.info(
                                "[%s] 公式未確定のため暫定URLで深掘り: %s (domain_score=%s name=%s addr=%s host_token=%s ai_hint=%s)",
                                cid,
                                provisional_homepage,
                                domain_score,
                                name_present,
                                address_ok,
                                strong_domain_host,
                                provisional_ai_hint,
                            )

                    # 暫定URLは深掘りにのみ使用し、保存は公式昇格条件を満たした場合に限定
                    if not homepage and provisional_homepage:
                        weak_provisional = (
                            provisional_domain_score < 3
                            and not provisional_host_token
                            and not provisional_name_present
                            and not provisional_address_ok
                            and not provisional_ai_hint
                            and provisional_evidence_score < 6
                            and not provisional_profile_hit
                        )
                        if weak_provisional:
                            # ルール上は弱いがAI公式（ただし除外）を見ている場合は、reviewで保持して深掘りは継続する
                            if ai_official_rejected_record:
                                rejected_url = ai_official_rejected_record.get("normalized_url") or ai_official_rejected_record.get("url") or ""
                                if rejected_url:
                                    ds = int(ai_official_rejected_record.get("domain_score") or 0)
                                    if ds == 0:
                                        ds = scraper._domain_score(company_tokens, rejected_url)  # type: ignore
                                        ai_official_rejected_record["domain_score"] = ds
                                    homepage = rejected_url
                                    info = ai_official_rejected_record.get("info")
                                    primary_cands = ai_official_rejected_record.get("extracted") or {}
                                    selected_candidate_record = ai_official_rejected_record
                                    homepage_official_flag = 0
                                    homepage_official_source = f"ai_rejected:{ai_official_rejected_reason or 'weak_provisional'}"
                                    homepage_official_score = float((ai_official_rejected_record.get("rule") or {}).get("score") or 0.0)
                                    chosen_domain_score = ds
                                    force_review = True
                                else:
                                    homepage = ""
                                    primary_cands = {}
                                    provisional_info = None
                                    provisional_cands = {}
                                    force_review = True
                                    provisional_homepage = ""
                            else:
                                # 弱い暫定でも「深掘り起点」としては保持する（保存可否は後段のポリシー/envで制御）
                                homepage = provisional_homepage
                                info = provisional_info
                                primary_cands = provisional_cands
                                selected_candidate_record = best_record
                                homepage_official_flag = 0
                                homepage_official_source = "provisional_weak"
                                homepage_official_score = float((best_record.get("rule") or {}).get("score") or 0.0) if best_record else 0.0
                                chosen_domain_score = int(provisional_domain_score or 0)
                                force_review = True
                                if not (company.get("provisional_reason") or "").strip():
                                    company["provisional_reason"] = "weak_provisional_target"
                        else:
                            homepage = provisional_homepage
                            info = provisional_info
                            primary_cands = provisional_cands
                            selected_candidate_record = best_record
                            homepage_official_flag = 0
                            homepage_official_source = homepage_official_source or "provisional"
                            homepage_official_score = 0.0
                            chosen_domain_score = provisional_domain_score

                    if not homepage and timed_out and best_record:
                        normalized_url = best_record.get("normalized_url") or best_record.get("url") or ""
                        if normalized_url:
                            rule_details = best_record.get("rule") or {}
                            domain_score = int(best_record.get("domain_score") or 0)
                            host_token_hit = bool(best_record.get("host_token_hit"))
                            strong_domain_host = bool(best_record.get("strong_domain_host"))
                            name_present = bool(rule_details.get("name_present"))
                            strong_domain = bool(rule_details.get("strong_domain"))
                            addr_hit = bool(rule_details.get("address_match"))
                            pref_hit = bool(rule_details.get("prefecture_match"))
                            zip_hit = bool(rule_details.get("postal_code_match"))
                            address_ok = addr_hit or pref_hit or zip_hit
                            allow_without_host = (
                                name_present
                                or strong_domain_host
                                or domain_score >= 4
                                or (address_ok and domain_score >= 3)
                            )
                            if (host_token_hit or allow_without_host) and (host_token_hit or domain_score >= 2 or name_present or strong_domain or address_ok):
                                log.info("[%s] タイムアウトで暫定公式として保存: %s", cid, normalized_url)
                                homepage = normalized_url
                                info = best_record.get("info")
                                primary_cands = best_record.get("extracted") or {}
                                selected_candidate_record = best_record
                                homepage_official_flag = 0
                                homepage_official_source = homepage_official_source or "provisional_timeout"
                                homepage_official_score = float(rule_details.get("score") or 0.0)
                                chosen_domain_score = domain_score
                                force_review = True

                    official_phase_end = elapsed()
                    deep_phase_deadline = time.monotonic() + DEEP_PHASE_TIMEOUT_SEC if DEEP_PHASE_TIMEOUT_SEC > 0 else None
                    priority_docs: dict[str, dict[str, Any]] = {}
                    if selected_candidate_record:
                        preload_docs = selected_candidate_record.get("profile_docs") or {}
                        for url, pdata in preload_docs.items():
                            priority_docs[url] = {
                                "text": (pdata.get("text", "") or ""),
                                "html": (pdata.get("html", "") or ""),
                            }
                        if priority_docs:
                            for url, pdata in priority_docs.items():
                                absorb_doc_data(url, pdata)
                    # 候補フェーズで取得した profile_docs があれば再利用し、不要な巡回を減らす
                    if homepage:
                        for rec in candidate_records:
                            normalized_url = rec.get("normalized_url") or rec.get("url") or ""
                            if normalized_url != homepage:
                                continue
                            preload_docs = rec.get("profile_docs") or {}
                            for url, pdata in preload_docs.items():
                                priority_docs[url] = {
                                    "text": (pdata.get("text", "") or ""),
                                    "html": (pdata.get("html", "") or ""),
                                }
                            break

                    phone = ""
                    found_address = ""
                    rep_name_val = scraper.clean_rep_name(company.get("rep_name")) or ""
                    # description は常に AI に生成させるため、既存値は参照しない
                    description_val = ""
                    listing_val = clean_listing_value(company.get("listing") or "")
                    revenue_val = clean_amount_value(company.get("revenue") or "")
                    profit_val = clean_amount_value(company.get("profit") or "")
                    capital_val = clean_amount_value(company.get("capital") or "")
                    fiscal_val = clean_fiscal_month(company.get("fiscal_month") or "")
                    founded_val = clean_founded_year(company.get("founded_year") or "")
                    phone_source = "none"
                    address_source = "none"
                    ai_used = 0
                    ai_model = ""
                    company.setdefault("error_code", "")
                    company.setdefault("listing", listing_val)
                    company.setdefault("revenue", revenue_val)
                    company.setdefault("profit", profit_val)
                    company.setdefault("capital", capital_val)
                    company.setdefault("fiscal_month", fiscal_val)
                    company.setdefault("founded_year", founded_val)
                    src_phone = ""
                    src_addr = ""
                    src_rep = ""
                    verify_result = {"phone_ok": False, "address_ok": False}
                    verify_result_source = "none"
                    confidence = 0.0
                    address_ai_confidence: float | None = None
                    address_ai_evidence: str | None = None
                    rule_phone = None
                    rule_address = None
                    rule_rep = None

                    need_listing = not bool(listing_val)
                    need_capital = not bool(capital_val)
                    need_revenue = not bool(revenue_val)
                    need_profit = not bool(profit_val)
                    need_fiscal = not bool(fiscal_val)
                    need_founded = not bool(founded_val)
                    need_description = not bool(description_val)

                    info_dict = info or {}
                    info_url = homepage
                    page_type_per_url: dict[str, str] = {}
                    drop_reasons: dict[str, str] = {}
                    drop_details_by_url: dict[str, dict[str, str]] = {}
                    ai_official_selected = bool(
                        homepage
                        and homepage_official_flag == 1
                        and isinstance(homepage_official_source, str)
                        and homepage_official_source.startswith("ai")
                    )
                    deep_allowed = (not ai_official_primary) or ai_official_selected

                    def absorb_doc_data(url: str, pdata: dict[str, Any]) -> None:
                        nonlocal rule_phone, rule_address, rule_rep
                        nonlocal src_phone, src_addr, src_rep
                        nonlocal listing_val, need_listing
                        nonlocal capital_val, need_capital
                        nonlocal revenue_val, need_revenue
                        nonlocal profit_val, need_profit
                        nonlocal fiscal_val, need_fiscal
                        nonlocal founded_val, need_founded
                        nonlocal description_val, need_description

                        text_val = pdata.get("text", "") or ""
                        html_val = pdata.get("html", "") or ""
                        try:
                            pt = scraper.classify_page_type(url, text=text_val, html=html_val).get("page_type") or "OTHER"
                        except Exception:
                            pt = "OTHER"
                        page_type_per_url[url] = str(pt)

                        cc = scraper.extract_candidates(text_val, html_val)
                        if cc.get("phone_numbers"):
                            cand = pick_best_phone(cc["phone_numbers"])
                            if cand and not rule_phone:
                                if pt in {"COMPANY_PROFILE", "ACCESS_CONTACT"}:
                                    rule_phone = cand
                                    src_phone = url
                                else:
                                    drop_reasons["phone"] = drop_reasons.get("phone") or f"not_profile:{pt}"
                        if cc.get("addresses"):
                            cand_addr = pick_best_address(None if ai_official_selected else addr, cc["addresses"])
                            if cand_addr and not rule_address:
                                cand_norm = normalize_address(cand_addr) or cand_addr
                                ok, reason = _address_candidate_ok(
                                    cand_norm,
                                    cc.get("addresses") or [],
                                    pt,
                                    addr,
                                    ai_official_selected,
                                )
                                if ok:
                                    rule_address = cand_norm
                                    src_addr = url
                                else:
                                    reason = reason or "no_hq_marker"
                                    drop_reasons["address"] = drop_reasons.get("address") or f"{reason}:{pt}"
                        if cc.get("rep_names"):
                            cand_rep = pick_best_rep(cc["rep_names"], url)
                            cand_rep = scraper.clean_rep_name(cand_rep) if cand_rep else None
                            if cand_rep:
                                rep_ok, rep_reason = _rep_candidate_ok(
                                    cand_rep,
                                    cc.get("rep_names") or [],
                                    pt,
                                    url,
                                )
                                if rep_ok:
                                    if not rule_rep or len(cand_rep) > len(rule_rep):
                                        rule_rep = cand_rep
                                        src_rep = url
                                else:
                                    drop_reasons["rep"] = drop_reasons.get("rep") or rep_reason
                        if cc.get("listings"):
                            candidate = pick_best_listing(cc["listings"])
                            if candidate and not listing_val:
                                listing_val = candidate
                                need_listing = False
                        if cc.get("capitals"):
                            candidate = pick_best_amount(cc["capitals"])
                            if candidate and (not capital_val or len(candidate) > len(capital_val)):
                                capital_val = candidate
                                need_capital = False
                        if cc.get("revenues"):
                            candidate = pick_best_amount(cc["revenues"])
                            if candidate and (not revenue_val or len(candidate) > len(revenue_val)):
                                revenue_val = candidate
                                need_revenue = False
                        if cc.get("profits"):
                            candidate = pick_best_amount(cc["profits"])
                            if candidate and (not profit_val or len(candidate) > len(profit_val)):
                                profit_val = candidate
                                need_profit = False
                        if cc.get("fiscal_months"):
                            cleaned_fiscal = clean_fiscal_month(cc["fiscal_months"][0] or "")
                            if cleaned_fiscal and not fiscal_val:
                                fiscal_val = cleaned_fiscal
                                need_fiscal = False
                        if cc.get("founded_years"):
                            for cand in cc["founded_years"]:
                                cleaned_founded = clean_founded_year(cand or "")
                                if cleaned_founded and not founded_val:
                                    founded_val = cleaned_founded
                                    need_founded = False
                                    break

                    def refresh_need_flags() -> tuple[int, int]:
                        nonlocal need_phone, need_addr, need_rep
                        nonlocal need_listing, need_capital, need_revenue
                        nonlocal need_profit, need_fiscal, need_founded, need_description
                        need_phone = not bool(phone or rule_phone)
                        # 入力住所があってもサイト住所未取得なら取りに行く（found/ruleのみで判定）
                        need_addr = not bool(found_address or rule_address)
                        need_rep = not bool(rep_name_val or rule_rep)
                        need_listing = not bool(listing_val)
                        need_capital = not bool(capital_val)
                        need_revenue = not bool(revenue_val)
                        need_profit = not bool(profit_val)
                        need_fiscal = not bool(fiscal_val)
                        need_founded = not bool(founded_val)
                        need_description = not bool(description_val)
                        missing_contact = int(need_phone) + int(need_addr) + int(need_rep)
                        missing_extra = sum([
                            int(need_listing), int(need_capital), int(need_revenue),
                            int(need_profit), int(need_fiscal), int(need_founded), int(need_description),
                        ])
                        return missing_contact, missing_extra

                    def update_description_candidate(candidate: str | None) -> bool:
                        nonlocal description_val, need_description
                        if not candidate:
                            return False
                        cleaned = clean_description_value(candidate)
                        if not cleaned:
                            return False
                        if looks_mojibake(cleaned):
                            return False
                        banned_terms = (
                            "お問い合わせ",
                            "お問合せ",
                            "採用情報",
                            "求人",
                            "ニュース",
                            "お知らせ",
                            "アクセス",
                            "所在地",
                            "電話番号",
                            "メール",
                        )
                        if any(term in cleaned for term in banned_terms):
                            return False
                        lower = cleaned.lower()
                        if lower.startswith(("contact", "recruit", "news")):
                            return False
                        if "http://" in lower or "https://" in lower:
                            return False
                        if len(cleaned) < 10:
                            return False
                        if len(cleaned) > 120:
                            cleaned = cleaned[:120].rstrip()
                        if cleaned == description_val:
                            return False
                        description_val = cleaned
                        need_description = False
                        return True

                    # AI公式採用のときは、同じAI呼び出し(judge_official_homepage)で生成された description を優先採用する
                    if homepage_official_flag == 1 and homepage_official_source.startswith("ai") and isinstance(ai_official_description, str) and ai_official_description.strip():
                        prev_desc = description_val
                        description_val = ""
                        if not update_description_candidate(ai_official_description):
                            description_val = prev_desc

                    if homepage and info_dict:
                        absorb_doc_data(info_url, info_dict)

                    missing_contact, missing_extra = refresh_need_flags()

                    def quick_verify_from_docs(phone_val: str | None, addr_val: str | None) -> dict[str, Any]:
                        result = {"phone_ok": False, "address_ok": False}
                        phone_pat = scraper._phone_variants_regex(phone_val) if phone_val else None  # type: ignore
                        addr_key = CompanyScraper._addr_key(addr_val) if addr_val else ""
                        if not phone_pat and not addr_key:
                            return result

                        def check_text(text: str) -> None:
                            if phone_pat and not result["phone_ok"] and phone_pat.search(text):
                                result["phone_ok"] = True
                            if addr_key and not result["address_ok"]:
                                text_key = CompanyScraper._addr_key(text)
                                if addr_key and addr_key in text_key:
                                    result["address_ok"] = True

                        payloads: list[dict[str, Any]] = []
                        if info_dict:
                            payloads.append(info_dict)
                        payloads.extend(priority_docs.values())
                        for payload in payloads:
                            text = (payload.get("text", "") or "")
                            if not text and payload.get("html"):
                                text = payload.get("html", "") or ""
                            if text:
                                check_text(text)
                            if result["phone_ok"] and result["address_ok"]:
                                break
                        return result

                    fully_filled = False
                    if homepage:
                        info_dict = info or {}
                        info_url = info_dict.get("url") or homepage
                        # まず「会社概要/企業情報/会社情報」系の優先リンクを先に巡回して主要情報を拾う
                        if over_hard_deadline() or over_time_limit() or over_deep_phase_deadline():
                            timed_out = True
                            if over_hard_deadline():
                                raise_hard_timeout("priority_docs")
                            priority_docs = {}
                        else:
                            should_fetch_priority = (
                                deep_allowed
                                and (
                                    missing_contact > 0
                                    or need_description
                                    or need_founded
                                    or need_listing
                                    or need_revenue
                                    or need_profit
                                    or need_capital
                                    or need_fiscal
                                )
                            )
                            allow_slow_priority = bool(
                                missing_contact > 0
                                or need_rep
                                or need_description
                                or need_founded
                                or need_listing
                            )
                            try:
                                early_priority_docs = await asyncio.wait_for(
                                    scraper.fetch_priority_documents(
                                        homepage,
                                        info_dict.get("html", ""),
                                        max_links=3 if should_fetch_priority else 0,
                                        concurrency=FETCH_CONCURRENCY,
                                        target_types=["about", "contact", "finance"] if should_fetch_priority else None,
                                        allow_slow=allow_slow_priority,
                                        exclude_urls=set(priority_docs.keys()) if priority_docs else None,
                                    ),
                                    timeout=clamp_timeout(PAGE_FETCH_TIMEOUT_SEC),
                                ) if should_fetch_priority else {}
                            except Exception:
                                early_priority_docs = {}
                            for url, pdata in early_priority_docs.items():
                                priority_docs[url] = pdata
                                absorb_doc_data(url, pdata)
                        if timed_out:
                            missing_contact, missing_extra = refresh_need_flags()
                            fully_filled = False
                            related = {}

                        cands = primary_cands or {}
                        phones = cands.get("phone_numbers") or []
                        addrs = cands.get("addresses") or []
                        reps = cands.get("rep_names") or []
                        listings = cands.get("listings") or []
                        capitals = cands.get("capitals") or []
                        revenues = cands.get("revenues") or []
                        profits = cands.get("profits") or []
                        fiscals = cands.get("fiscal_months") or []
                        founded_years = cands.get("founded_years") or []

                        # primary_cands は page_type が不明なことがあるため、基本は absorb_doc_data の結果を優先する
                        pt_info = page_type_per_url.get(info_url) or "OTHER"
                        if phones and not rule_phone and pt_info in {"COMPANY_PROFILE", "ACCESS_CONTACT"}:
                            rule_phone = pick_best_phone(phones)
                        if addrs and not rule_address:
                            cand_addr = pick_best_address(None if ai_official_selected else addr, addrs)
                            if cand_addr:
                                cand_norm = normalize_address(cand_addr) or cand_addr
                                ok, reason = _address_candidate_ok(
                                    cand_norm,
                                    addrs,
                                    pt_info,
                                    addr,
                                    ai_official_selected,
                                )
                                if ok:
                                    rule_address = cand_norm
                                else:
                                    reason = reason or "no_hq_marker"
                                    drop_reasons["address"] = drop_reasons.get("address") or f"{reason}:{pt_info}"
                        if reps and not rule_rep:
                            cand_rep = pick_best_rep(reps, info_url)
                            cand_rep = scraper.clean_rep_name(cand_rep) if cand_rep else None
                            if cand_rep:
                                rep_ok, rep_reason = _rep_candidate_ok(
                                    cand_rep,
                                    reps,
                                    pt_info,
                                    info_url,
                                )
                                if rep_ok:
                                    rule_rep = cand_rep
                                else:
                                    drop_reasons["rep"] = drop_reasons.get("rep") or rep_reason
                        if rule_phone and not src_phone:
                            src_phone = info_url
                        if rule_address and not src_addr:
                            src_addr = info_url
                        if rule_rep:
                            if not rep_name_val or len(rule_rep) > len(rep_name_val):
                                rep_name_val = rule_rep
                            if not src_rep:
                                src_rep = info_url
                        if listings and not listing_val:
                            candidate = pick_best_listing(listings)
                            if candidate:
                                listing_val = candidate
                        if capitals and not capital_val:
                            candidate = pick_best_amount(capitals)
                            if candidate:
                                capital_val = candidate
                        if revenues and not revenue_val:
                            candidate = pick_best_amount(revenues)
                            if candidate:
                                revenue_val = candidate
                        if profits and not profit_val:
                            candidate = pick_best_amount(profits)
                            if candidate:
                                profit_val = candidate
                        if fiscals and not fiscal_val:
                            cleaned_fiscal = clean_fiscal_month(fiscals[0] or "")
                            if cleaned_fiscal:
                                fiscal_val = cleaned_fiscal
                        if founded_years and not founded_val:
                            cleaned_founded = clean_founded_year(founded_years[0] or "")
                            if cleaned_founded:
                                founded_val = cleaned_founded

                        need_listing = not bool(listing_val)
                        need_capital = not bool(capital_val)
                        need_revenue = not bool(revenue_val)
                        need_profit = not bool(profit_val)
                        need_fiscal = not bool(fiscal_val)
                        need_founded = not bool(founded_val)
                        need_description = not bool(description_val)

                        missing_contact, missing_extra = refresh_need_flags()

                        if over_time_limit():
                            timed_out = True
                            if over_hard_deadline():
                                raise_hard_timeout("after_priority_docs")

                        fully_filled = homepage and missing_contact == 0 and missing_extra == 0

                        try:
                            # 深掘りは不足がある場合のみ。揃っていれば追加巡回しない。
                            priority_limit = 0
                            # 不足項目に応じて深掘り対象リンクを絞り込む
                            target_types: list[str] = []
                            if not timed_out and not fully_filled and not over_deep_phase_deadline():
                                if missing_contact > 0:
                                    priority_limit = 3
                                    target_types.append("contact")
                                    base_pt = page_type_per_url.get(info_url) or page_type_per_url.get(homepage) or "OTHER"
                                    # provisional/非プロフィール起点だと contact だけでは会社概要に到達しにくいので about も許可する
                                    if base_pt != "COMPANY_PROFILE" and "about" not in target_types:
                                        priority_limit = max(priority_limit, 2)
                                        target_types.append("about")
                                        # 会社概要導線は1〜2本だけ辿る（既存max_pages内、無駄打ち禁止）
                                        priority_limit = min(priority_limit, 2)
                                if need_description or need_founded or need_listing:
                                    priority_limit = max(priority_limit, 2)
                                    target_types.append("about")
                                if need_revenue or need_profit or need_capital or need_fiscal:
                                    priority_limit = max(priority_limit, 2)
                                    target_types.append("finance")
                            site_docs = {}
                            # 既取得URLは除外しつつ不足がある場合のみ追加巡回する
                            if priority_limit > 0 and not over_deep_phase_deadline():
                                allow_slow_priority = bool("about" in target_types or "contact" in target_types)
                                site_docs = await asyncio.wait_for(
                                    scraper.fetch_priority_documents(
                                        homepage,
                                        info_dict.get("html", ""),
                                        max_links=priority_limit,
                                        concurrency=FETCH_CONCURRENCY,
                                        target_types=target_types or None,
                                        allow_slow=allow_slow_priority,
                                        exclude_urls=set(priority_docs.keys()) if priority_docs else None,
                                    ),
                                    timeout=clamp_timeout(PAGE_FETCH_TIMEOUT_SEC),
                                )
                        except Exception:
                            site_docs = {}
                        for url, pdata in site_docs.items():
                            priority_docs[url] = pdata
                            absorb_doc_data(url, pdata)
                        if site_docs:
                            # provisional起点が会社概要でない場合、会社概要ページに到達できたら採用元URLを切り替える
                            try:
                                base_pt = page_type_per_url.get(info_url) or page_type_per_url.get(homepage) or "OTHER"
                            except Exception:
                                base_pt = page_type_per_url.get(info_url) or "OTHER"
                            if homepage and homepage_official_flag == 0 and base_pt != "COMPANY_PROFILE":
                                profile_urls = [u for u in site_docs.keys() if page_type_per_url.get(u) == "COMPANY_PROFILE"]
                                if profile_urls:
                                    profile_urls.sort(key=lambda u: (0 if any(seg in (u or "").lower() for seg in ("/company", "/about", "/corporate", "/profile", "/overview", "/outline")) else 1, len(u or ""), u))
                                    adopted_url = profile_urls[0]
                                    adopted = site_docs.get(adopted_url) or priority_docs.get(adopted_url)
                                    if adopted and adopted_url and adopted_url != homepage:
                                        log.info("[%s] provisional起点から会社概要へ誘導: adopt=%s from=%s", cid, adopted_url, homepage)
                                        info_url = adopted_url
                                        info_dict = adopted
                                        homepage = adopted_url
                                        try:
                                            provisional_profile_hit = True
                                            if adopted_url:
                                                ds = scraper._domain_score(company_tokens, adopted_url)  # type: ignore
                                                provisional_domain_score = max(int(provisional_domain_score or 0), int(ds or 0))
                                                provisional_host_token = provisional_host_token or scraper._host_token_hit(company_tokens, adopted_url)  # type: ignore
                                            text_val = adopted.get("text", "") or ""
                                            html_val = adopted.get("html", "") or ""
                                            extracted = scraper.extract_candidates(text_val, html_val)
                                            adopted_rule = scraper.is_likely_official_site(
                                                name,
                                                adopted_url,
                                                {"url": adopted_url, "text": text_val, "html": html_val},
                                                addr,
                                                extracted,
                                                return_details=True,
                                            )
                                            if isinstance(adopted_rule, dict):
                                                provisional_name_present = provisional_name_present or bool(adopted_rule.get("name_present"))
                                                provisional_address_ok = provisional_address_ok or bool(
                                                    adopted_rule.get("address_match")
                                                    or adopted_rule.get("prefecture_match")
                                                    or adopted_rule.get("postal_code_match")
                                                )
                                                provisional_evidence_score = max(
                                                    int(provisional_evidence_score or 0),
                                                    int(adopted_rule.get("official_evidence_score") or 0),
                                                )
                                        except Exception:
                                            pass
                                        # 保存用の暫定URL(起点)は保持しつつ、採用URLは切り替える
                                        force_review = True
                            missing_contact, missing_extra = refresh_need_flags()

                        # AIは最終手段（1社あたり最大1回）なので、この段階では呼び出さない。
                        # deep後に候補群（住所/電話/代表/会社情報/事業テキスト）をまとめて1回だけAIへ渡す。
                        ai_result = None
                        ai_attempted = False
                        ai_phone: str | None = None
                        ai_addr: str | None = None
                        ai_rep: str | None = None
    
                        missing_contact, missing_extra = refresh_need_flags()
                        if need_description:
                            payloads: list[dict[str, Any]] = []
                            if info_dict:
                                payloads.append(info_dict)
                            payloads.extend(priority_docs.values())
                            for pdata in payloads:
                                desc = extract_description_from_payload(pdata)
                                if desc:
                                    description_val = desc
                                    need_description = False
                                    break
    
                        if rule_phone:
                            phone = rule_phone
                            phone_source = "rule"
                            if not src_phone:
                                src_phone = info_url
                        else:
                            phone = ""
                            phone_source = "none"
    
                        if rule_address:
                            found_address = rule_address
                            address_source = "rule"
                            if not src_addr:
                                src_addr = info_url
                        else:
                            found_address = rule_address or ""
                            address_source = "none"
    
                        if rule_rep:
                            if not rep_name_val or len(rule_rep) > len(rep_name_val):
                                rep_name_val = rule_rep
                                if not src_rep:
                                    src_rep = info_url
                        # deep crawl は代表者の有無に関係なく実行する（不足がある場合のみ）
                        deep_pages_visited = 0
                        deep_fetch_count = 0
                        deep_fetch_failures = 0
                        deep_skip_reason = ""
                        deep_urls_visited = []
                        deep_phone_candidates = 0
                        deep_address_candidates = 0
                        deep_rep_candidates = 0
    
                        missing_contact, missing_extra = refresh_need_flags()
                        need_extra_fields = missing_extra > 0
                        related = {}
                        related_meta: dict[str, Any] = {}
                        if not deep_allowed:
                            deep_skip_reason = "ai_not_selected"
                        elif missing_contact == 0 and not need_extra_fields:
                            deep_skip_reason = "no_missing_fields"
                        else:
                            related_page_limit = RELATED_BASE_PAGES + (1 if missing_extra else 0)
                            if need_phone:
                                related_page_limit += RELATED_EXTRA_PHONE
                            if need_rep:
                                related_page_limit += 1
                            if need_description:
                                related_page_limit += 1
                            related_cap = 6 if ai_official_selected else 4
                            related_page_limit = max(0, min(related_cap, related_page_limit))

                            deep_on_weak = os.getenv("DEEP_ON_WEAK_PROVISIONAL", "true").lower() == "true"
                            weak_provisional_target = (
                                homepage_official_flag == 0
                                and homepage_official_source.startswith("provisional")
                                and chosen_domain_score < 3
                                and not provisional_host_token
                                and not provisional_name_present
                                and not provisional_address_ok
                                and not provisional_ai_hint
                                and provisional_evidence_score < 6
                                and not provisional_profile_hit
                            )
                            if weak_provisional_target and not deep_on_weak:
                                deep_skip_reason = "weak_provisional_target"
    
                            if not timed_out and ((missing_contact > 0) or need_extra_fields):
                                if over_time_limit() or over_deep_limit() or over_hard_deadline() or over_deep_phase_deadline():
                                    deep_skip_reason = "over_limit_before_deep"
                                    timed_out = True
                                    if over_hard_deadline():
                                        raise_hard_timeout("related_crawl")
                                else:
                                    # 弱い暫定URLでも、未取得があるなら「軽量deep」で救済する
                                    if weak_provisional_target and deep_on_weak:
                                        weak_cap = 2 if (need_rep or need_description) else 1
                                        related_page_limit = min(weak_cap, related_page_limit)
                                        max_hops = 2 if (need_rep or need_description) else 1
                                    else:
                                        max_hops = RELATED_MAX_HOPS_PHONE if need_phone else RELATED_MAX_HOPS_BASE
                                        max_hops_cap = 4 if ai_official_selected else 3
                                        max_hops = max(0, min(max_hops_cap, int(max_hops or 0)))
                                    try:
                                        related, related_meta = await asyncio.wait_for(
                                            scraper.crawl_related(
                                                homepage,
                                                need_phone,
                                                need_addr,
                                                need_rep,
                                                max_pages=related_page_limit,
                                                max_hops=max_hops,
                                                need_listing=need_listing,
                                                need_capital=need_capital,
                                                need_revenue=need_revenue,
                                                need_profit=need_profit,
                                                need_fiscal=need_fiscal,
                                                need_founded=need_founded,
                                                need_description=need_description,
                                                initial_info=info_dict if info_url == homepage else None,
                                                expected_address=addr,
                                                return_meta=True,
                                                allow_slow=bool(need_addr or need_rep or need_description) and bool(
                                                    (homepage_official_flag == 1)
                                                    or (chosen_domain_score >= 4)
                                                    or provisional_host_token
                                                    or provisional_name_present
                                                    or provisional_address_ok
                                                ),
                                            ),
                                            timeout=clamp_timeout(PAGE_FETCH_TIMEOUT_SEC),
                                        )
                                    except Exception:
                                        related = {}
                                        related_meta = {}
                                        deep_skip_reason = deep_skip_reason or "deep_exception"
    
                            deep_pages_visited = int((related_meta or {}).get("pages_visited") or len(related))
                            deep_fetch_count = int((related_meta or {}).get("fetch_count") or 0)
                            deep_fetch_failures = int((related_meta or {}).get("fetch_failures") or 0)
                            deep_urls_visited = list((related_meta or {}).get("urls_visited") or list(related.keys()))
                            deep_stop_reason = str((related_meta or {}).get("stop_reason") or "")
                            if related:
                                try:
                                    log.info("[%s] deep_crawl visited=%s", cid, list(related.keys()))
                                except Exception:
                                    pass
                            try:
                                log.info(
                                    "[%s] deep_crawl summary pages=%s fetch=%s fail=%s reason=%s",
                                    cid,
                                    deep_pages_visited,
                                    deep_fetch_count,
                                    deep_fetch_failures,
                                    deep_skip_reason or (related_meta or {}).get("stop_reason") or "",
                                )
                            except Exception:
                                pass

                            for url, data in related.items():
                                    text = data.get("text", "") or ""
                                    html_content = data.get("html", "") or ""
                                    try:
                                        pt = scraper.classify_page_type(url, text=text, html=html_content).get("page_type") or "OTHER"
                                    except Exception:
                                        pt = "OTHER"
                                    page_type_per_url[url] = str(pt)
                                    cc = scraper.extract_candidates(text, html_content)
                                    deep_phone_candidates += len(cc.get("phone_numbers") or [])
                                    deep_address_candidates += len(cc.get("addresses") or [])
                                    deep_rep_candidates += len(cc.get("rep_names") or [])
                                    if need_phone and cc.get("phone_numbers"):
                                            cand = pick_best_phone(cc["phone_numbers"])
                                            if cand:
                                                if pt in {"COMPANY_PROFILE", "ACCESS_CONTACT"}:
                                                    phone = cand
                                                    phone_source = "rule"
                                                    src_phone = url
                                                    need_phone = False
                                                    log.info("[%s] deep_crawl picked phone=%s url=%s", cid, cand, url)
                                                else:
                                                    reason = f"not_profile:{pt}"
                                                    drop_reasons["phone"] = drop_reasons.get("phone") or reason
                                                    drop_details_by_url.setdefault(url, {})["phone"] = reason
                                                    log.info("[%s] deep_crawl rejected phone reason=%s url=%s cand=%s", cid, reason, url, cand)
                                            else:
                                                reason = "no_valid_phone"
                                                drop_reasons["phone"] = drop_reasons.get("phone") or reason
                                                drop_details_by_url.setdefault(url, {})["phone"] = reason
                                                log.info(
                                                    "[%s] deep_crawl rejected phones reason=%s url=%s candidates=%s",
                                                    cid,
                                                    reason,
                                                    url,
                                                    (cc.get("phone_numbers") or [])[:3],
                                                )
                                    if need_addr and cc.get("addresses"):
                                        cand_addr = pick_best_address(None if ai_official_selected else addr, cc["addresses"])
                                        if cand_addr:
                                            cand_norm = normalize_address(cand_addr) or cand_addr
                                            ok, reason = _address_candidate_ok(
                                                cand_norm,
                                                cc.get("addresses") or [],
                                                pt,
                                                addr,
                                                ai_official_selected,
                                            )
                                            if ok:
                                                found_address = cand_norm
                                                address_source = "rule"
                                                src_addr = url
                                                need_addr = False
                                                log.info("[%s] deep_crawl picked address=%s url=%s", cid, cand_norm, url)
                                            else:
                                                reason = (reason or "no_hq_marker")
                                                reason = f"{reason}:{pt}"
                                                drop_reasons["address"] = drop_reasons.get("address") or reason
                                                drop_details_by_url.setdefault(url, {})["address"] = reason
                                                log.info("[%s] deep_crawl rejected address reason=%s url=%s cand=%s", cid, reason, url, cand_norm)
                                        else:
                                            reason = "no_valid_address"
                                            drop_reasons["address"] = drop_reasons.get("address") or reason
                                            drop_details_by_url.setdefault(url, {})["address"] = reason
                                            log.info(
                                                "[%s] deep_crawl rejected addresses reason=%s url=%s candidates=%s",
                                                cid,
                                                reason,
                                                url,
                                                (cc.get("addresses") or [])[:3],
                                            )
                                    if need_rep and cc.get("rep_names"):
                                        cand_rep = pick_best_rep(cc["rep_names"], url)
                                        cand_rep = scraper.clean_rep_name(cand_rep) if cand_rep else None
                                        if cand_rep:
                                            rep_ok, rep_reason = _rep_candidate_ok(
                                                cand_rep,
                                                cc.get("rep_names") or [],
                                                pt,
                                                url,
                                            )
                                            if rep_ok:
                                                rep_name_val = cand_rep
                                                src_rep = url
                                                need_rep = False
                                                log.info("[%s] deep_crawl picked rep=%s url=%s", cid, cand_rep, url)
                                            else:
                                                reason = rep_reason or f"not_profile:{pt}"
                                                drop_reasons["rep"] = drop_reasons.get("rep") or reason
                                                drop_details_by_url.setdefault(url, {})["rep"] = reason
                                                log.info("[%s] deep_crawl rejected rep reason=%s url=%s cand=%s", cid, reason, url, cand_rep)
                                    if need_description and cc.get("description"):
                                        desc = clean_description_value(cc["description"])
                                        if desc:
                                            description_val = desc
                                            need_description = False
                                    if need_listing and cc.get("listings"):
                                        cleaned_listing = clean_listing_value(cc["listings"][0] or "")
                                        if cleaned_listing:
                                            listing_val = cleaned_listing
                                            need_listing = False
                                    if need_capital and cc.get("capitals"):
                                        cleaned_capital = clean_amount_value(cc["capitals"][0] or "")
                                        if cleaned_capital:
                                            capital_val = cleaned_capital
                                            need_capital = False
                                    if need_revenue and cc.get("revenues"):
                                        cleaned_revenue = clean_amount_value(cc["revenues"][0] or "")
                                        if cleaned_revenue:
                                            revenue_val = cleaned_revenue
                                            need_revenue = False
                                    if need_profit and cc.get("profits"):
                                        cleaned_profit = clean_amount_value(cc["profits"][0] or "")
                                        if cleaned_profit:
                                            profit_val = cleaned_profit
                                            need_profit = False
                                    if need_fiscal and cc.get("fiscal_months"):
                                        cleaned_fiscal = clean_fiscal_month(cc["fiscal_months"][0] or "")
                                        if cleaned_fiscal:
                                            fiscal_val = cleaned_fiscal
                                            need_fiscal = False
                                    if need_founded and cc.get("founded_years"):
                                        cleaned_founded = clean_founded_year(cc["founded_years"][0] or "")
                                        if cleaned_founded:
                                            founded_val = cleaned_founded
                                            need_founded = False
                                    if not (
                                        need_phone or need_addr or need_rep or need_listing or need_capital
                                        or need_revenue or need_profit or need_fiscal or need_founded or need_description
                                    ):
                                        break
    
                            if homepage and (need_addr or not found_address) and not timed_out and not over_deep_phase_deadline():
                                try:
                                    extra_docs = await asyncio.wait_for(
                                        scraper.fetch_priority_documents(
                                            homepage,
                                            info_dict.get("html", ""),
                                            max_links=3,
                                            concurrency=FETCH_CONCURRENCY,
                                            allow_slow=need_addr,
                                            exclude_urls=set(priority_docs.keys()) if priority_docs else None,
                                        ),
                                        timeout=clamp_timeout(PAGE_FETCH_TIMEOUT_SEC),
                                    )
                                except Exception:
                                    extra_docs = {}
                                for url, pdata in extra_docs.items():
                                    priority_docs[url] = pdata
                                    absorb_doc_data(url, pdata)
    
                            deep_phase_end = elapsed()
                            # ---- AI (final, max 1 call/company) ----
                            missing_contact, missing_extra = refresh_need_flags()
                            ai_need_final = bool(
                                homepage
                                and USE_AI
                                and verifier is not None
                                and not timed_out
                                and has_time_for_ai()
                                and (not USE_AI_OFFICIAL or AI_FINAL_WITH_OFFICIAL)
                                and (
                                    missing_contact > 0
                                    or missing_extra > 0
                                    or (not description_val)
                                )
                            )
                            if ai_need_final:
                                try:
                                    # 取得済みdocsから、page_type優先で最大3ページ分だけAIへ渡す（探索増なし）
                                    docs_by_url: dict[str, dict[str, Any]] = {}
                                    if info_url and info_dict:
                                        docs_by_url[info_url] = {
                                            "text": info_dict.get("text", "") or "",
                                            "html": info_dict.get("html", "") or "",
                                        }
                                    for u, d in (priority_docs or {}).items():
                                        if u not in docs_by_url:
                                            docs_by_url[u] = d
                                    for u, d in (related or {}).items():
                                        if u not in docs_by_url:
                                            docs_by_url[u] = {
                                                "text": d.get("text", "") or "",
                                                "html": d.get("html", "") or "",
                                            }
    
                                    def _pt_priority(pt: str) -> int:
                                        return {"COMPANY_PROFILE": 0, "ACCESS_CONTACT": 1, "OTHER": 2, "BASES_LIST": 3, "DIRECTORY_DB": 4}.get(pt, 9)
    
                                    scored_urls: list[tuple[int, str]] = []
                                    for u, d in docs_by_url.items():
                                        try:
                                            pt = page_type_per_url.get(u) or scraper.classify_page_type(
                                                u, text=d.get("text", ""), html=d.get("html", "")
                                            ).get("page_type") or "OTHER"
                                        except Exception:
                                            pt = page_type_per_url.get(u) or "OTHER"
                                        page_type_per_url[u] = str(pt)
                                        scored_urls.append((_pt_priority(str(pt)), u))
                                    scored_urls.sort(key=lambda x: (x[0], x[1]))
                                    top_urls_for_ai = [u for _, u in scored_urls if u][:3]
    
                                    def _pack_candidates(urls_for_ai: list[str]) -> dict[str, Any]:
                                        out: dict[str, Any] = {
                                            "company_name": name,
                                            "csv_address": addr_raw,
                                            "urls": [],
                                            "candidates": {
                                                "phone_numbers": [],
                                                "addresses": [],
                                                "representatives": [],
                                                "company_facts": {"capitals": [], "founded": [], "listing": []},
                                            },
                                            "business_snippets": [],
                                        }
                                        for u in urls_for_ai:
                                            d = docs_by_url.get(u) or {}
                                            t = d.get("text", "") or ""
                                            h = d.get("html", "") or ""
                                            pt = page_type_per_url.get(u) or "OTHER"
                                            out["urls"].append({"url": u, "page_type": pt})
                                            cc = scraper.extract_candidates(t, h)
                                            for p in (cc.get("phone_numbers") or [])[:10]:
                                                out["candidates"]["phone_numbers"].append({"value": p, "url": u, "page_type": pt})
                                            for a in (cc.get("addresses") or [])[:10]:
                                                out["candidates"]["addresses"].append({"value": a, "url": u, "page_type": pt})
                                            for r in (cc.get("rep_names") or [])[:10]:
                                                out["candidates"]["representatives"].append({"value": r, "url": u, "page_type": pt})
                                            for c in (cc.get("capitals") or [])[:8]:
                                                out["candidates"]["company_facts"]["capitals"].append({"value": c, "url": u, "page_type": pt})
                                            for fy in (cc.get("founded_years") or [])[:6]:
                                                out["candidates"]["company_facts"]["founded"].append({"value": fy, "url": u, "page_type": pt})
                                            for li in (cc.get("listings") or [])[:6]:
                                                out["candidates"]["company_facts"]["listing"].append({"value": li, "url": u, "page_type": pt})
                                            biz = select_relevant_paragraphs(t, limit=3)
                                            if biz:
                                                out["business_snippets"].append({"url": u, "snippet": biz[:800], "page_type": pt})
                                        return out
    
                                    ai_payload = _pack_candidates(top_urls_for_ai)
                                    screenshot_payload = None
                                    if info_url:
                                        info_dict = await ensure_info_has_screenshot(
                                            scraper, info_url, info_dict, need_screenshot=OFFICIAL_AI_USE_SCREENSHOT
                                        )
                                        screenshot_payload = (info_dict or {}).get("screenshot")
    
                                    ai_started = time.monotonic()
                                    ai_attempted = True
                                    ai_result = await asyncio.wait_for(
                                        verifier.select_company_fields(ai_payload, screenshot_payload, name, addr_raw),
                                        timeout=clamp_timeout(max(ai_call_timeout, 5.0)),
                                    )
                                    ai_time_spent += time.monotonic() - ai_started
                                except Exception:
                                    ai_result = None
    
                                if isinstance(ai_result, dict):
                                    ai_used = 1
                                    ai_model = AI_MODEL_NAME
                                    company["ai_confidence"] = ai_result.get("confidence")
                                    company["ai_reason"] = "final_selection"
                                    def _strip_tags_for_ai(raw: str) -> str:
                                        out = raw or ""
                                        while True:
                                            m = re.match(r"^\[[A-Z_]+\]", out)
                                            if not m:
                                                break
                                            out = out[m.end():].lstrip()
                                        return out
                                    def _find_src(cands: list[dict[str, Any]], kind: str, chosen: str) -> str:
                                        if not chosen:
                                            return ""
                                        for it in cands or []:
                                            if not isinstance(it, dict):
                                                continue
                                            raw = it.get("value")
                                            url0 = it.get("url")
                                            if not (isinstance(raw, str) and isinstance(url0, str) and url0):
                                                continue
                                            raw_val = _strip_tags_for_ai(raw)
                                            if kind == "phone" and (normalize_phone(raw_val) or "") == chosen:
                                                return url0
                                            if kind == "address" and (normalize_address(raw_val) or "") == chosen:
                                                return url0
                                            if kind == "rep":
                                                cleaned = scraper.clean_rep_name(raw_val) or ""
                                                if cleaned and cleaned == chosen:
                                                    return url0
                                        return ""
                                    if not description_val and isinstance(ai_result.get("description"), str) and ai_result.get("description"):
                                        description_val = ai_result["description"]
                                        try:
                                            company["description_evidence"] = json.dumps(ai_result.get("description_evidence") or [], ensure_ascii=False)
                                        except Exception:
                                            company["description_evidence"] = ""
                                    if not phone and isinstance(ai_result.get("phone_number"), str) and ai_result.get("phone_number"):
                                        phone = normalize_phone(ai_result.get("phone_number")) or ""
                                        if phone:
                                            phone_source = "ai"
                                            src_phone = _find_src(ai_payload.get("candidates", {}).get("phone_numbers") or [], "phone", phone)
                                    if not found_address and isinstance(ai_result.get("address"), str) and ai_result.get("address"):
                                        addr_ai = normalize_address(ai_result.get("address"))
                                        if addr_ai:
                                            found_address = addr_ai
                                            address_source = "ai"
                                            src_addr = _find_src(ai_payload.get("candidates", {}).get("addresses") or [], "address", found_address)
                                            ev = ai_result.get("evidence")
                                            if isinstance(ev, str) and ev.strip():
                                                address_ai_evidence = ev.strip()[:200]
                                    if not rep_name_val and isinstance(ai_result.get("representative"), str) and ai_result.get("representative"):
                                        rep_candidate = scraper.clean_rep_name(ai_result.get("representative"))
                                        if rep_candidate:
                                            rep_name_val = rep_candidate
                                            src_rep = _find_src(ai_payload.get("candidates", {}).get("representatives") or [], "rep", rep_name_val)
                                    facts = ai_result.get("company_facts") if isinstance(ai_result.get("company_facts"), dict) else {}
                                    if not founded_val and isinstance(facts.get("founded"), str) and facts.get("founded"):
                                        founded_val = clean_founded_year(facts.get("founded"))
                                    if not capital_val and isinstance(facts.get("capital"), str) and facts.get("capital"):
                                        capital_val = clean_amount_value(facts.get("capital"))
                                    if isinstance(facts.get("employees"), str) and facts.get("employees"):
                                        company["employees"] = str(facts.get("employees"))[:60]
                                    if isinstance(facts.get("license"), str) and facts.get("license"):
                                        company["license"] = str(facts.get("license"))[:80]
                                    if isinstance(ai_result.get("industry"), str) and ai_result.get("industry"):
                                        company["industry"] = str(ai_result.get("industry"))[:60]
                                    if isinstance(ai_result.get("business_tags"), list):
                                        try:
                                            company["business_tags"] = json.dumps(ai_result.get("business_tags")[:5], ensure_ascii=False)
                                        except Exception:
                                            company["business_tags"] = ""
    
                            expected_phone = phone or rule_phone or None
                            expected_addr = found_address or addr or rule_address or ""
                            expected_addr = normalize_address(expected_addr) or ""
                            verifiable_addr = expected_addr if is_address_verifiable(expected_addr) else None
                            quick_verify_result = quick_verify_from_docs(expected_phone, verifiable_addr)
                            verify_result = dict(quick_verify_result)
                            verify_result_source = "docs" if any(verify_result.values()) else "skip"
                            require_phone = bool(expected_phone)
                            require_addr = bool(verifiable_addr)
                            need_online_verify = (
                                not timed_out
                                and homepage
                                and (require_phone or require_addr)
                                and ((require_phone and not verify_result.get("phone_ok")) or (require_addr and not verify_result.get("address_ok")))
                                and not over_after_official()
                                and not over_deep_phase_deadline()
                            )
                            if need_online_verify:
                                try:
                                    verify_result = await asyncio.wait_for(
                                        scraper.verify_on_site(
                                            homepage,
                                            expected_phone,
                                            verifiable_addr,
                                            fetch_limit=5,
                                        ),
                                        timeout=clamp_timeout(PAGE_FETCH_TIMEOUT_SEC),
                                    )
                                    verify_result_source = "online"
                                except Exception:
                                    log.warning("[%s] verify_on_site 失敗", cid, exc_info=True)
                                    verify_result = quick_verify_result
                                    verify_result_source = "docs"
    
                            phone_ok = bool(verify_result.get("phone_ok"))
                            addr_ok = bool(verify_result.get("address_ok"))
                            required = int(require_phone) + int(require_addr)
                            matches = int(phone_ok) + int(addr_ok)
                            evidence_ok = (not require_phone or phone_ok) and (not require_addr or addr_ok)
                            if required == 0:
                                confidence = max(confidence or 0.0, 0.6)
                            elif evidence_ok:
                                confidence = 1.0 if required == 2 else 0.85
                            elif matches >= 1:
                                confidence = 0.75
                            else:
                                confidence = 0.45
    
                            # 公式フラグは「公式らしさ」の判定を優先し、verifyは追加根拠として扱う。
                            # ただし、電話/住所ともに具体的な期待値があるのに両方拾えない場合は疑わしいので降格する。
                            if homepage and homepage_official_flag == 1 and required == 2 and matches == 0:
                                log.info("[%s] verify_on_siteで根拠不足のため公式フラグを降格 (required=%s matches=%s): %s", cid, required, matches, homepage)
                                homepage_official_flag = 0
                                homepage_official_source = "verify_fail"
                                force_review = True
                            elif homepage and homepage_official_flag == 1 and required > 0 and not evidence_ok:
                                force_review = True
                    else:
                        if urls:
                            log.info("[%s] 公式サイト候補を判別できず -> 未保存", cid)
                        else:
                            log.info("[%s] 有効なホームページ候補なし。", cid)
                        company["rep_name"] = company.get("rep_name", "") or ""
                        company["description"] = company.get("description", "") or ""
                        confidence = 0.4

                except SkipCompany:
                    raise
                except HardTimeout:
                    raise
                except Exception:
                    fatal_error = True
                    raise
                finally:
                    if fatal_error:
                        pass
                    else:
                        if skip_company_reason:
                            save_no_homepage(skip_company_reason)
                            # SkipCompany を上位へ伝播させて次の会社へ（以降の補完/深掘りは行わない）
                            raise SkipCompany(skip_company_reason)
                        # 公式サイトが無い場合のみ、検索結果の非公式ページから連絡先を補完
                        if not homepage and (not phone or not found_address or not rep_name_val):
                            for url, data in fallback_cands:
                                if not phone and data.get("phone_numbers"):
                                    cand = pick_best_phone(data["phone_numbers"])
                                    if cand:
                                        phone = cand
                                        phone_source = "rule"
                                        src_phone = url
                                if not found_address and data.get("addresses"):
                                    cand_addr = pick_best_address(addr, data["addresses"])
                                    if cand_addr:
                                        found_address = cand_addr
                                        address_source = "rule"
                                        src_addr = url
                                if not rep_name_val and data.get("rep_names"):
                                    cand_rep = pick_best_rep(data["rep_names"], url)
                                    cand_rep = scraper.clean_rep_name(cand_rep) if cand_rep else None
                                    if cand_rep:
                                        pt_fallback = page_type_per_url.get(url) or "OTHER"
                                        rep_ok, rep_reason = _rep_candidate_ok(
                                            cand_rep,
                                            data.get("rep_names") or [],
                                            pt_fallback,
                                            url,
                                        )
                                        if rep_ok:
                                            rep_name_val = cand_rep
                                            src_rep = url
                                        else:
                                            drop_reasons["rep"] = drop_reasons.get("rep") or rep_reason
                                if phone and found_address and rep_name_val:
                                    break

                        if not listing_val:
                            for url, data in fallback_cands:
                                values = data.get("listings") or []
                                candidate = pick_best_listing(values)
                                if candidate:
                                    listing_val = candidate
                                    break
                        if not capital_val:
                            for url, data in fallback_cands:
                                values = data.get("capitals") or []
                                candidate = pick_best_amount(values)
                                if candidate:
                                    capital_val = candidate
                                    break
                        if not revenue_val:
                            for url, data in fallback_cands:
                                values = data.get("revenues") or []
                                candidate = pick_best_amount(values)
                                if candidate:
                                    revenue_val = candidate
                                    break
                        if not profit_val:
                            for url, data in fallback_cands:
                                values = data.get("profits") or []
                                candidate = pick_best_amount(values)
                                if candidate:
                                    profit_val = candidate
                                    break
                        if not fiscal_val:
                            for url, data in fallback_cands:
                                values = data.get("fiscal_months") or []
                                if values:
                                    cleaned_fallback_fiscal = clean_fiscal_month(values[0] or "")
                                    if cleaned_fallback_fiscal:
                                        fiscal_val = cleaned_fallback_fiscal
                                        break
                        if not founded_val:
                            for url, data in fallback_cands:
                                values = data.get("founded_years") or []
                                if values:
                                    cleaned_fallback_founded = clean_founded_year(values[0] or "")
                                    if cleaned_fallback_founded:
                                        founded_val = cleaned_fallback_founded
                                        break

                        found_address = sanitize_text_block(found_address)
                        rule_address = sanitize_text_block(rule_address)
                        if found_address and not looks_like_address(found_address):
                            found_address = ""
                        if not found_address and rule_address:
                            found_address = rule_address
                        if found_address and not looks_like_address(found_address):
                            found_address = ""
                        normalized_found_address = normalize_address(found_address) if found_address else ""
                        if not normalized_found_address and addr:
                            normalized_found_address = normalize_address(addr) or ""
                        if ai_official_selected and normalized_found_address and (address_source or "") != "none":
                            company["address"] = normalized_found_address
                        csv_pref = CompanyScraper._extract_prefecture(addr) if addr else ""
                        hp_pref = CompanyScraper._extract_prefecture(normalized_found_address) if normalized_found_address else ""
                        pref_match = int(bool(csv_pref and hp_pref and csv_pref == hp_pref)) if (csv_pref and hp_pref) else None
                        csv_city_m = CITY_RE.search(addr or "")
                        hp_city_m = CITY_RE.search(normalized_found_address or "")
                        city_match = int(bool(csv_city_m and hp_city_m and csv_city_m.group(1) == hp_city_m.group(1))) if (csv_city_m and hp_city_m) else None
                        rep_name_val = scraper.clean_rep_name(rep_name_val) or ""
                        description_val = clean_description_value(sanitize_text_block(description_val))
                        listing_val = clean_listing_value(listing_val)
                        capital_val = ai_normalize_amount(capital_val) or clean_amount_value(capital_val)
                        revenue_val = ai_normalize_amount(revenue_val) or clean_amount_value(revenue_val)
                        profit_val = ai_normalize_amount(profit_val) or clean_amount_value(profit_val)
                        fiscal_val = clean_fiscal_month(fiscal_val)
                        founded_val = clean_founded_year(founded_val)
                        homepage = clean_homepage_url(homepage)
                        if not homepage:
                            homepage_official_flag = 0
                            homepage_official_source = ""
                            homepage_official_score = 0.0
                        normalized_phone = normalize_phone(phone)
                        if normalized_phone:
                            phone = normalized_phone
                        else:
                            phone = ""
                            phone_source = "none"
                            src_phone = ""
                        if drop_details_by_url:
                            try:
                                drop_reasons["_by_url"] = {k: drop_details_by_url[k] for k in list(drop_details_by_url.keys())[:3]}
                            except Exception:
                                pass
                        try:
                            log.info(
                                "[%s] final_decision homepage=%s official=%s(%s score=%.1f domain=%s) phone=%s(%s) address=%s(%s) rep=%s(%s)",
                                cid,
                                homepage or "",
                                homepage_official_flag,
                                homepage_official_source,
                                float(homepage_official_score or 0.0),
                                chosen_domain_score,
                                phone or "",
                                phone_source or "",
                                normalized_found_address or "",
                                address_source or "",
                                rep_name_val or "",
                                (src_rep or ""),
                            )
                        except Exception:
                            pass
                        company.update({
                            "homepage": homepage,
                            "phone": phone or "",
                            "found_address": normalized_found_address,
                            "rep_name": rep_name_val,
                            "description": description_val,
                            "listing": listing_val,
                            "revenue": revenue_val,
                            "profit": profit_val,
                            "capital": capital_val,
                            "fiscal_month": fiscal_val,
                            "founded_year": founded_val,
                            "phone_source": phone_source,
                            "address_source": address_source,
                            "ai_used": ai_used,
                            "ai_model": ai_model,
                            "extract_confidence": confidence,
                            "source_url_phone": src_phone,
                            "source_url_address": src_addr,
                            "source_url_rep": src_rep,
                            "homepage_official_flag": homepage_official_flag,
                            "homepage_official_source": homepage_official_source,
                            "homepage_official_score": homepage_official_score,
                            "address_confidence": address_ai_confidence,
                            "address_evidence": address_ai_evidence,
                            "deep_pages_visited": int(deep_pages_visited or 0),
                            "deep_fetch_count": int(deep_fetch_count or 0),
                            "deep_fetch_failures": int(deep_fetch_failures or 0),
                            "deep_skip_reason": deep_skip_reason or "",
                            "deep_urls_visited": json.dumps(list(deep_urls_visited or [])[:5], ensure_ascii=False),
                            "deep_phone_candidates": int(deep_phone_candidates or 0),
                            "deep_address_candidates": int(deep_address_candidates or 0),
                            "deep_rep_candidates": int(deep_rep_candidates or 0),
                            "top3_urls": json.dumps(list(urls or [])[:3], ensure_ascii=False),
                            "exclude_reasons": json.dumps(exclude_reasons or {}, ensure_ascii=False),
                            "skip_reason": (company.get("skip_reason") or "").strip(),
                            "provisional_homepage": (provisional_homepage or forced_provisional_homepage or ""),
                            "provisional_reason": ((company.get("provisional_reason") or "").strip() or forced_provisional_reason or ""),
                            "final_homepage": (homepage or ""),
                            "deep_enabled": int(bool(deep_pages_visited or deep_fetch_count)),
                            "deep_stop_reason": (deep_stop_reason or deep_skip_reason or ""),
                            "timeout_stage": timeout_stage or "",
                            "page_type_per_url": json.dumps(page_type_per_url or {}, ensure_ascii=False),
                            "extracted_candidates_count": json.dumps(
                                {
                                    "phone": int(deep_phone_candidates or 0) + len((primary_cands or {}).get("phone_numbers") or []),
                                    "address": int(deep_address_candidates or 0) + len((primary_cands or {}).get("addresses") or []),
                                    "rep": int(deep_rep_candidates or 0) + len((primary_cands or {}).get("rep_names") or []),
                                },
                                ensure_ascii=False,
                            ),
                            "drop_reasons": json.dumps(drop_reasons or {}, ensure_ascii=False),
                            "pref_match": pref_match,
                            "city_match": city_match,
                        })

                        had_verify_target = bool(
                            (phone or rule_phone) or is_address_verifiable(found_address or rule_address or addr)
                        )

                        if REFERENCE_CHECKER:
                            accuracy_payload = REFERENCE_CHECKER.evaluate(company)
                            if accuracy_payload:
                                company.update(accuracy_payload)

                        # 暫定URLの保存可否（SAVE_PROVISIONAL_HOMEPAGE=false のときだけ抑制）
                        if not SAVE_PROVISIONAL_HOMEPAGE:
                            decision = apply_provisional_homepage_policy(
                                homepage=homepage,
                                homepage_official_flag=int(homepage_official_flag or 0),
                                homepage_official_source=str(homepage_official_source or ""),
                                homepage_official_score=float(homepage_official_score or 0.0),
                                chosen_domain_score=int(chosen_domain_score or 0),
                                provisional_host_token=bool(provisional_host_token),
                                provisional_name_present=bool(provisional_name_present),
                                provisional_address_ok=bool(provisional_address_ok),
                                provisional_ai_hint=bool(provisional_ai_hint),
                                provisional_profile_hit=bool(provisional_profile_hit),
                                provisional_evidence_score=int(provisional_evidence_score or 0),
                            )
                            if decision.dropped:
                                log.info(
                                    "[%s] 暫定URLを保存しません (domain_score=%s host_token=%s name=%s addr=%s): %s",
                                    cid,
                                    chosen_domain_score,
                                    provisional_host_token,
                                    provisional_name_present,
                                    provisional_address_ok,
                                    homepage,
                                )
                            homepage = decision.homepage
                            homepage_official_flag = decision.homepage_official_flag
                            homepage_official_source = decision.homepage_official_source
                            homepage_official_score = decision.homepage_official_score
                            chosen_domain_score = decision.chosen_domain_score
                            if decision.dropped:
                                # company dict も同期してDB/CSVと状態が一致するようにする（final_homepage は保持）
                                company["homepage"] = ""
                                company["homepage_official_flag"] = 0
                                company["homepage_official_source"] = ""
                                company["homepage_official_score"] = 0.0

                        if not homepage:
                            top3_urls = list(urls or [])[:3]
                            top3_records = sorted(candidate_records or [], key=lambda r: r.get("search_rank", 1e9))[:3]
                            all_directory_like = bool(top3_records) and all(bool((r.get("rule") or {}).get("directory_like")) for r in top3_records)
                            if not top3_urls:
                                skip_reason = "no_search_results_or_prefiltered"
                            elif all_directory_like:
                                skip_reason = "top3_all_directory_like"
                            else:
                                skip_reason = "no_official_in_top3"
                            if not (company.get("error_code") or "").strip():
                                company["error_code"] = skip_reason
                            append_jsonl(
                                NO_OFFICIAL_LOG_PATH,
                                {
                                    "id": cid,
                                    "company_name": name,
                                    "csv_address": addr,
                                    "skip_reason": skip_reason,
                                    "top3_urls": top3_urls,
                                    "top3_candidates": [
                                        {
                                            "url": (r.get("normalized_url") or r.get("url") or ""),
                                            "search_rank": int(r.get("search_rank") or 0),
                                            "domain_score": int(r.get("domain_score") or 0),
                                            "rule_score": float(((r.get("rule") or {}).get("score")) or 0.0),
                                            "directory_like": bool((r.get("rule") or {}).get("directory_like")),
                                            "directory_score": int((r.get("rule") or {}).get("directory_score") or 0),
                                            "directory_reasons": list((r.get("rule") or {}).get("directory_reasons") or [])[:8],
                                            "blocked_host": bool((r.get("rule") or {}).get("blocked_host")),
                                            "prefecture_mismatch": bool((r.get("rule") or {}).get("prefecture_mismatch")),
                                        }
                                        for r in top3_records
                                    ],
                                },
                            )

                        status = "done" if homepage else "no_homepage"
                        if timed_out:
                            company["error_code"] = "timeout"
                            status = "review"
                        if not homepage and candidate_records:
                            status = "review"
                            if status == "done" and found_address and (not ai_official_selected) and not addr_compatible(addr, found_address):
                                status = "review"
                        if (
                            status == "done"
                            and had_verify_target
                            and not verify_result.get("phone_ok")
                            and not verify_result.get("address_ok")
                            and chosen_domain_score < 5
                        ):
                            status = "review"
                        if force_review and status != "error":
                            status = "review"
                        if status == "done" and chosen_domain_score and chosen_domain_score < 4 and homepage_official_source in ("ai", "rule"):
                            status = "review"
                        if status == "review" and homepage_official_source == "provisional" and not verify_result.get("phone_ok") and not verify_result.get("address_ok"):
                            # 暫定URLで検証できない場合でも、深掘り/AI結果があれば保持し、ホームページは空に戻さない
                            pass

                        strong_official = False
                        if status == "done":
                            strong_official = bool(
                                homepage
                                and homepage_official_flag == 1
                                and (chosen_domain_score or 0) >= 4
                                and (
                                    ai_official_selected
                                    or (not addr or not found_address or addr_compatible(addr, found_address))
                                )
                                and (
                                    not had_verify_target
                                    or verify_result.get("phone_ok")
                                    or verify_result.get("address_ok")
                                )
                            )
                            if not strong_official:
                                status = "review"

                        company.setdefault("error_code", "")

                        total_elapsed = elapsed()
                        if not search_phase_end:
                            search_phase_end = total_elapsed
                        if not official_phase_end:
                            official_phase_end = search_phase_end
                        if not deep_phase_end:
                            deep_phase_end = total_elapsed
                        search_time = search_phase_end
                        official_time = max(0.0, official_phase_end - search_phase_end)
                        deep_time = max(0.0, deep_phase_end - official_phase_end)
                        log.info(
                            "[%s] timings: search=%.1fs official=%.1fs deep=%.1fs ai=%.1fs total=%.1fs verify=%s",
                            cid,
                            search_time,
                            official_time,
                            deep_time,
                            ai_time_spent,
                            total_elapsed,
                            verify_result_source,
                        )
                        try:
                            log_phase_metric(cid, "search", search_time, status, homepage, company.get("error_code", ""))
                            log_phase_metric(cid, "official", official_time, status, homepage, company.get("error_code", ""))
                            log_phase_metric(cid, "deep", deep_time, status, homepage, company.get("error_code", ""))
                            log_phase_metric(cid, "ai", ai_time_spent, status, homepage, company.get("error_code", ""))
                            log_phase_metric(cid, "total", total_elapsed, status, homepage, company.get("error_code", ""))
                        except Exception:
                            log.debug("phase metrics skipped", exc_info=True)

                        manager.save_company_data(company, status=status)
                        log.info("[%s] 保存完了: status=%s elapsed=%.1fs (worker=%s)", cid, status, elapsed(), WORKER_ID)

                        if csv_writer:
                            csv_writer.writerow({k: company.get(k, "") for k in CSV_FIELDNAMES})
                            csv_file.flush()

                        processed += 1

            except SkipCompany:
                # save_no_homepage() が保存済み（エラー扱いにしない）
                try:
                    log.info("[%s] 候補が全滅のため次へ (worker=%s)", cid, WORKER_ID)
                    if csv_writer:
                        csv_writer.writerow({k: company.get(k, "") for k in CSV_FIELDNAMES})
                        csv_file.flush()
                    processed += 1
                except Exception:
                    pass
            except HardTimeout:
                # 60秒超え等で打ち切り：ここまでに分かっている情報を保存して次へ
                try:
                    save_partial("timeout")
                except Exception:
                    pass
            except Exception as e:
                log.error("[%s] エラー: %s (worker=%s)", cid, e, WORKER_ID, exc_info=True)
                manager.update_status(cid, "error")

            # 1社ごとのスリープ（±JITTERでレート制限/ドメイン集中回避）
            if SLEEP_BETWEEN_SEC > 0:
                await asyncio.sleep(jittered_seconds(SLEEP_BETWEEN_SEC, JITTER_RATIO))

        if timeouts_extended:
            TIME_LIMIT_FETCH_ONLY = DEFAULT_TIME_LIMIT_FETCH_ONLY
            TIME_LIMIT_WITH_OFFICIAL = DEFAULT_TIME_LIMIT_WITH_OFFICIAL
            TIME_LIMIT_DEEP = DEFAULT_TIME_LIMIT_DEEP
            try:
                scraper.page_timeout_ms = normal_page_timeout_ms
                scraper.slow_page_threshold_ms = normal_slow_page_threshold_ms
            except Exception:
                pass

    finally:
        if csv_file:
            csv_file.close()
        if hasattr(scraper, "close") and callable(getattr(scraper, "close")):
            try:
                await scraper.close()
            except Exception:
                log.warning("scraper.close() はスキップ（未実装または失敗）", exc_info=True)
        manager.close()
        log.info("全処理終了 (worker=%s)", WORKER_ID)

if __name__ == "__main__":
    asyncio.run(process())
