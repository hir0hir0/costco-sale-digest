"""情報の入り口: 公式サイトの巡回と、メール（メルマガ）の取り込み。

方針:

- **robots.txt を必ず読んで従う**。公開サイトとして出す以上ここは譲らない。
- 取得間隔を空ける（既定3秒／`Crawl-delay` があればそちら）。
- 取得した生HTMLは常に `captures/` に保存する。ページ構造が変わって抽出が0件に
  なったとき、後から現物を見て直せるようにするため（JEXERで効いたやり方）。
- URLを決め打ちしない。`discover` でトップページからセール系のリンクを拾い、
  それを `costco_sources.json` に保存して使う。
"""

from __future__ import annotations

import email
import email.header
import email.message
import email.utils
import gzip
import hashlib
import imaplib
import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path

from .models import KIND_COUPON, KIND_SALE, Offer, norm_text, today_jst
from .parse import (find_prices, html_to_lines, html_to_lines_with_images,
                    offers_from_item_numbers, offers_from_json_ld,
                    offers_from_lines)

ROOT = Path(__file__).resolve().parent.parent
CAPTURE_DIR = ROOT / "captures" / "costco"
SOURCES_FILE = ROOT / "costco_sources.json"

USER_AGENT = ("Mozilla/5.0 (compatible; costco-sale-digest/1.0; "
              "+https://github.com/hir0hir0/costco-sale-digest)")
DEFAULT_DELAY = 3.0
BASE_URL = "https://www.costco.co.jp/"

# トップページからセール系ページを探すときの手掛かり
SALE_WORDS = ("セール", "お買い得", "特価", "値下げ", "割引", "クーポン", "sale",
              "coupon", "offer", "deal", "savings", "お得")
COUPON_WORDS = ("クーポン", "coupon")


# ---------------------------------------------------------------- .env

def load_dotenv(path: Path | None = None) -> None:
    """`.env` を環境変数へ読み込む（既にある値は上書きしない）。"""
    p = path or (ROOT / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- HTTP

class FetchError(RuntimeError):
    pass


@dataclass
class Page:
    url: str
    status: int
    html: str
    fetched_at: str


def _opener() -> urllib.request.OpenerDirector:
    handlers: list = []
    proxy = os.environ.get("COSTCO_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    ctx = ssl.create_default_context()
    ca = os.environ.get("COSTCO_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca and Path(ca).exists():
        ctx.load_verify_locations(ca)
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _decode(raw: bytes, headers) -> str:
    enc = (headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    elif "deflate" in enc:
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    charset = None
    ctype = headers.get("Content-Type") or ""
    m = re.search(r"charset=([\w\-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb'charset=["\']?([\w\-]+)', raw[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "replace")
    for cand in (charset, "utf-8", "cp932", "euc-jp"):
        if not cand:
            continue
        try:
            return raw.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def fetch(url: str, *, timeout: float = 20.0, tries: int = 3) -> Page:
    """1ページ取得。失敗したら指数バックオフで数回だけ再試行する。"""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            })
            with _opener().open(req, timeout=timeout) as r:
                raw = r.read()
                return Page(url=r.geturl(), status=r.status,
                            html=_decode(raw, r.headers),
                            fetched_at=datetime.now().isoformat(timespec="seconds"))
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 410):  # 再試行しても無駄
                raise FetchError(f"HTTP {e.code} {url}") from e
            last = e
        except Exception as e:  # 接続エラー・タイムアウト等
            last = e
        if attempt < tries - 1:
            time.sleep(2 ** attempt)
    raise FetchError(f"取得失敗 {url}: {last}")


class Robots:
    """robots.txt の判定。読めなければ「許可」扱い（404は許可が慣例）。"""

    def __init__(self, base_url: str = BASE_URL):
        self.base = base_url
        self.delay = DEFAULT_DELAY
        self._rp = urllib.robotparser.RobotFileParser()
        self.loaded = False
        self.note = ""

    def load(self) -> "Robots":
        url = urllib.parse.urljoin(self.base, "/robots.txt")
        try:
            page = fetch(url, tries=2)
            self._rp.parse(page.html.splitlines())
            self.loaded = True
            d = self._rp.crawl_delay(USER_AGENT) or self._rp.crawl_delay("*")
            if d:
                self.delay = max(float(d), DEFAULT_DELAY)
        except Exception as e:
            self.note = f"robots.txt を読めませんでした（{e}）。既定の間隔で控えめに巡回します。"
        return self

    def allowed(self, url: str) -> bool:
        if not self.loaded:
            return True
        return self._rp.can_fetch(USER_AGENT, url)


def capture(page: Page, tag: str) -> Path:
    """生HTMLを保存する。抽出が壊れたときに現物から直せるようにするため。"""
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", tag)[:60] or "page"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = CAPTURE_DIR / f"{stamp}-{safe}.html"
    path.write_text(f"<!-- {page.url} / {page.fetched_at} -->\n{page.html}",
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------- リンク探索

_LINK_RE = re.compile(r"<a\b[^>]*href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_links(html: str, base: str) -> list[tuple[str, str]]:
    """(絶対URL, リンク文字列) の一覧。同一ホストのものだけ返す。"""
    host = urllib.parse.urlparse(base).netloc
    out, seen = [], set()
    for m in _LINK_RE.finditer(html):
        # href の実体参照を戻す（`&amp;` のままだとクエリが壊れたURLになる）
        href = urllib.parse.urljoin(base, unescape(m.group(1).strip()))
        if urllib.parse.urlparse(href).netloc != host:
            continue
        text = norm_text(_TAG_RE.sub(" ", m.group(2)))
        if href in seen:
            continue
        seen.add(href)
        out.append((href, text))
    return out


def discover(base: str = BASE_URL) -> list[dict]:
    """トップページからセール／クーポンらしきページを探す。

    URLを決め打ちで書くとサイト改装で即死ぬので、毎回ここから拾い直せるようにする。
    """
    robots = Robots(base).load()
    page = fetch(base)
    capture(page, "discover-top")
    cands = []
    for url, text in extract_links(page.html, page.url):
        hay = (url + " " + text).lower()
        if not any(w.lower() in hay for w in SALE_WORDS):
            continue
        cands.append({
            "name": text or url,
            "url": url,
            "kind": KIND_COUPON if any(w in hay for w in COUPON_WORDS) else KIND_SALE,
            "enabled": True,
            "allowed_by_robots": robots.allowed(url),
        })
    return cands


# ---------------------------------------------------------------- 設定

DEFAULT_SOURCES = {
    "base_url": BASE_URL,
    # 商品写真の扱い。"link"=コストコのURLを直接参照（既定・複製しない）、
    # true=site/img/ にダウンロード（オフラインでも出るがリポジトリが太る）、
    # false=写真なし。
    "images": "link",
    "web": [],
    "mail": {
        "enabled": True,
        "folder": "INBOX",
        "from_contains": ["costco"],
        "subject_contains": [],
        "since_days": 21,
    },
}


def load_sources(path: Path = SOURCES_FILE) -> dict:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_SOURCES))
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    merged = json.loads(json.dumps(DEFAULT_SOURCES))
    merged.update(data)
    merged["mail"] = {**DEFAULT_SOURCES["mail"], **(data.get("mail") or {})}
    return merged


def save_sources(data: dict, path: Path = SOURCES_FILE) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------- 公式サイト収集

@dataclass
class SourceReport:
    name: str
    url: str
    ok: bool
    count: int = 0
    detail: str = ""

    def line(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"  {mark} {self.name}: {self.count}件 {self.detail}".rstrip()


def collect_web(sources: dict, *, base: date | None = None,
                limit: int | None = None) -> tuple[list[Offer], list[SourceReport]]:
    """設定に並んだページを順に取得して Offer を作る。"""
    base = base or today_jst()
    entries = [e for e in sources.get("web", []) if e.get("enabled", True)]
    if limit:
        entries = entries[:limit]
    robots = Robots(sources.get("base_url", BASE_URL)).load()

    offers: list[Offer] = []
    reports: list[SourceReport] = []
    if robots.note:
        reports.append(SourceReport("robots.txt", "", True, 0, robots.note))

    for i, e in enumerate(entries):
        url, name = e["url"], e.get("name") or e["url"]
        if not robots.allowed(url):
            reports.append(SourceReport(name, url, False, 0, "robots.txt により巡回対象外"))
            continue
        if i:
            time.sleep(robots.delay)
        try:
            page = fetch(url)
        except FetchError as ex:
            reports.append(SourceReport(name, url, False, 0, str(ex)))
            continue
        capture(page, name)

        got = offers_from_json_ld(page.html, source="web", source_url=page.url, base=base)
        if not got:
            got = offers_from_lines(html_to_lines(page.html), source="web",
                                    source_url=page.url, source_label=name,
                                    base=base, kind=e.get("kind") or KIND_SALE)
        for o in got:
            o.kind = e.get("kind") or o.kind
            o.source_label = o.source_label or name
            if e.get("warehouse"):
                o.warehouse = e["warehouse"]
        offers.extend(got)
        detail = "" if got else "抽出0件（ページ構造が変わった可能性・captures/ を確認）"
        reports.append(SourceReport(name, url, bool(got), len(got), detail))

    return offers, reports


# ---------------------------------------------------------------- メール

def _mail_body_html(msg: email.message.Message) -> str:
    """メールから本文を取り出す。HTMLパートを優先し、無ければテキスト。"""
    html_parts, text_parts = [], []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            body = payload.decode(charset, "replace")
        except LookupError:
            body = payload.decode("utf-8", "replace")
        if part.get_content_subtype() == "html":
            html_parts.append(body)
        else:
            text_parts.append(body)
    if html_parts:
        return "\n".join(html_parts)
    # プレーンテキストは行構造をそのまま活かしたいので <br> に置き換えて返す
    return "<br>".join(l for t in text_parts for l in t.splitlines())


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return norm_text(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", "replace"))
            except LookupError:
                out.append(text.decode("utf-8", "replace"))
        else:
            out.append(text)
    return norm_text("".join(out))


def offers_from_message(msg: email.message.Message, *, base: date | None = None) -> list[Offer]:
    """1通のメールから Offer を作る。"""
    subject = _decode_header(msg.get("Subject"))
    sent = email.utils.parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
    seen = sent.date() if sent else (base or today_jst())
    html = _mail_body_html(msg)
    lines, images = html_to_lines_with_images(html)
    kind = KIND_COUPON if any(w in subject for w in COUPON_WORDS) else KIND_SALE
    label = f"メール「{subject}」" if subject else "メール"
    # 商品番号を軸にした切り出しが使えるならそちらが確実。無い号だけ総当たりに落とす。
    offers = offers_from_item_numbers(lines, source="mail", source_label=label,
                                      base=seen, kind=kind, images=images)
    if not offers:
        offers = offers_from_lines(lines, source="mail", source_label=label,
                                   base=seen, kind=kind)
    # メールに期間が書かれていない号もある。その場合は届いた日を開始日とみなす。
    for o in offers:
        if not o.starts_on and not o.ends_on:
            o.starts_on = seen.isoformat()
        o.first_seen = o.last_seen = seen.isoformat()
    return offers


def offers_from_eml(path: Path, *, base: date | None = None) -> list[Offer]:
    """保存済みの `.eml` から取り込む。IMAPに繋がなくても抽出を検証できる。"""
    with Path(path).open("rb") as f:
        msg = email.message_from_binary_file(f)
    return offers_from_message(msg, base=base)


class MailSkipped(RuntimeError):
    """認証情報が無い等で、メール取り込みを黙って飛ばす場合。"""


def iter_mail(conf: dict, *, limit: int | None = None):
    """コストコからのメールを新しい順の逆（古い順）に返す。読むだけで既読にしない。"""
    host = os.environ.get("COSTCO_IMAP_HOST")
    user = os.environ.get("COSTCO_IMAP_USER")
    password = os.environ.get("COSTCO_IMAP_PASSWORD")
    if not conf.get("enabled", True):
        raise MailSkipped("設定で無効になっています")
    if not (host and user and password):
        raise MailSkipped("COSTCO_IMAP_HOST/USER/PASSWORD が未設定")

    since = today_jst() - timedelta(days=int(conf.get("since_days", 21)))
    folder = conf.get("folder") or "INBOX"
    want = conf.get("subject_contains") or []

    with imaplib.IMAP4_SSL(host) as im:
        im.login(user, password)
        im.select(folder, readonly=True)   # readonly: 既読にしない
        uids: set[bytes] = set()
        stamp = since.strftime("%d-%b-%Y")
        for s in (conf.get("from_contains") or [""]):
            q = f'(SINCE "{stamp}")' if not s else f'(SINCE "{stamp}" FROM "{s}")'
            typ, data = im.search(None, q)
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        ordered = sorted(uids, key=lambda u: int(u))
        if limit:
            ordered = ordered[-limit:]     # 新しい方から limit 通
        for uid in ordered:
            typ, data = im.fetch(uid, "(RFC822)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            if want and not any(w in _decode_header(msg.get("Subject")) for w in want):
                continue
            yield msg


def collect_mail(conf: dict, *, base: date | None = None) -> tuple[list[Offer], list[SourceReport]]:
    """IMAPでコストコからのメールを読み、Offer を作る。

    認証情報が `.env` に無ければ静かにスキップする（JEXERと同じ扱い）。
    """
    reports: list[SourceReport] = []
    host = os.environ.get("COSTCO_IMAP_HOST") or ""
    offers: list[Offer] = []
    try:
        seen = 0
        for msg in iter_mail(conf):
            offers.extend(offers_from_message(msg, base=base))
            seen += 1
        reports.append(SourceReport("メール", host, True, len(offers), f"{seen}通を確認"))
    except MailSkipped as e:
        reports.append(SourceReport("メール", host, True, 0, f"{e} のためスキップ"))
    except Exception as e:
        reports.append(SourceReport("メール", host, False, 0, f"取り込み失敗: {e}"))
    return offers, reports


def mail_probe(conf: dict, *, limit: int = 6, context: int = 3,
               max_groups: int = 14, grep: str = "") -> list[dict]:
    """メルマガの中身を要約して返す（本文は captures/ に保存）。

    レイアウトが分からないと抽出規則が書けないので、価格を含む行とその前後だけを
    抜き出す。全文をログに流すのは無駄が多いうえ読みにくい。

    `grep` にカンマ区切りの語（商品名の一部や商品番号）を渡すと、要約の代わりに
    **その語を含む行の前後±8行**を返す。特定の商品の誤抽出を調べる用。
    """
    terms = [t.strip() for t in grep.split(",") if t.strip()]
    out = []
    for msg in iter_mail(conf, limit=limit):
        subject = _decode_header(msg.get("Subject"))
        html = _mail_body_html(msg)
        page = Page(url="mail:" + subject, status=200, html=html,
                    fetched_at=norm_text(msg.get("Date")))
        saved = capture(page, "mail-" + subject[:40])
        lines = html_to_lines(html)

        groups = []
        if terms:
            spans = []
            for i, ln in enumerate(lines):
                if any(t.lower() in ln.lower() for t in terms):
                    lo, hi = max(0, i - 8), min(len(lines), i + 9)
                    if spans and lo <= spans[-1][1]:      # 重なる窓は繋げる
                        spans[-1] = (spans[-1][0], hi)
                    else:
                        spans.append((lo, hi))
            for lo, hi in spans[:max_groups]:
                groups.append({"at": lo,
                               "lines": [f"{j}: {lines[j]}" for j in range(lo, hi)]})
        else:
            for i, ln in enumerate(lines):
                if find_prices(ln) and len(groups) < max_groups:
                    groups.append({"at": i, "lines": lines[max(0, i - context):i + 2]})
        got = offers_from_message(msg, base=today_jst())
        out.append({
            "subject": subject,
            "date": norm_text(msg.get("Date")),
            "capture": str(saved),
            "html_bytes": len(html),
            "text_lines": len(lines),
            "first_lines": lines[:20],
            "price_line_groups": groups,
            "offers_extracted": len(got),
            "offer_samples": [
                {"name": o.name, "price": o.price, "regular_price": o.regular_price,
                 "item_no": o.item_no, "period": f"{o.starts_on}〜{o.ends_on}"}
                for o in got[:8]
            ],
        })
    return out


# ---------------------------------------------------------------- probe

# ページが「JSで描くタイプ」かどうかの手掛かり。中身がここに入っているなら、
# HTMLを読むのではなくこの塊（またはその裏のAPI）を狙うほうが確実。
_STATE_KEYS = ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__APOLLO_STATE__",
               "window.dataLayer", "ACC.config", "ng-app", "data-reactroot")
_API_HINT_RE = re.compile(r"[\"'](/(?:rest|api|occ)/[A-Za-z0-9_\-/{}.]{3,60})[\"']")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


IMAGE_DIR = ROOT / "site" / "img"
MAX_IMAGE_BYTES = 400_000
MIN_IMAGE_BYTES = 1_500        # これ未満は開封計測用の透明画像とみなす
THUMB_PX = 480                 # サイトでの表示は150px高なのでこれで十分
_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
              "image/gif": ".gif"}
# 開封計測やスペーサー画像を掴まないための除外語
_IMAGE_SKIP = ("pixel", "spacer", "tracking", "open.aspx", "1x1", "beacon", "blank")


def _shrink(raw: bytes) -> tuple[bytes, str] | None:
    """Pillow があれば縮小してJPEGに変換する。無ければ None（原寸で保存）。

    メルマガの画像は原寸のままだと1枚100KB近くあり、毎週コミットすると
    リポジトリが太り続ける。Pillow は必須にしたくないので任意扱いにする。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((THUMB_PX, THUMB_PX))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=78, optimize=True)
        return buf.getvalue(), ".jpg"
    except Exception:
        return None


def fetch_images(offers: list[Offer], *, out_dir: Path = IMAGE_DIR,
                 limit: int = 250) -> tuple[int, int]:
    """商品画像を取り込んで `site/img/` に置き、`offer.image` を埋める。

    ファイル名はURLのハッシュにしてあるので、同じ画像を何度も取りに行かない。
    戻り値は (新しく取得した数, 失敗した数)。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    got = failed = 0
    # サイトに載る商品（価格か値引きが読めたもの）だけ取る。載らない商品の
    # 画像はリポジトリを太らせるだけで誰も見ない。参照を外しておかないと
    # prune_images が「使用中」と見なして消せない。
    for o in offers:
        if o.price is None and o.discount is None:
            o.image = ""

    for o in offers:
        if o.price is None and o.discount is None:
            continue
        if o.image or not o.image_url or got >= limit:
            continue
        url = o.image_url
        if not url.lower().startswith("http") or any(w in url.lower() for w in _IMAGE_SKIP):
            continue
        stem = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        hit = next((p for p in out_dir.glob(stem + ".*")), None)
        if hit:
            o.image = hit.name
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "image/*,*/*;q=0.8"})
            with _opener().open(req, timeout=20) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in _IMAGE_EXT:
                    continue
                raw = r.read(MAX_IMAGE_BYTES + 1)
            if not (MIN_IMAGE_BYTES <= len(raw) <= MAX_IMAGE_BYTES):
                continue
            shrunk = _shrink(raw)
            data, ext = shrunk if shrunk else (raw, _IMAGE_EXT[ctype])
            path = out_dir / (stem + ext)
            path.write_bytes(data)
            o.image = path.name
            got += 1
            time.sleep(0.3)      # 取得間隔を空ける
        except Exception:
            failed += 1
    return got, failed


def prune_images(offers: list[Offer], *, out_dir: Path = IMAGE_DIR) -> int:
    """どの商品からも参照されなくなった画像を消す。"""
    if not out_dir.exists():
        return 0
    used = {o.image for o in offers if o.image}
    removed = 0
    for p in out_dir.iterdir():
        if p.is_file() and p.name not in used:
            p.unlink()
            removed += 1
    return removed


def probe(url: str) -> dict:
    """1ページの構造を調べて要約を返す（生HTMLは captures/ に保存）。

    実サイトのHTMLをこちらで確認できない場所からでも、これを1回走らせれば
    「JSON-LDがあるか」「価格らしき文字列がいくつあるか」「そもそも静的HTMLに
    中身があるのか」が分かる。
    """
    page = fetch(url)
    saved = capture(page, "probe-" + urllib.parse.urlparse(url).path.strip("/").replace("/", "_"))
    lines = html_to_lines(page.html)
    from .parse import iter_json_ld

    ld = list(iter_json_ld(page.html))
    ld_types: dict[str, int] = {}
    for node in ld:
        t = node.get("@type")
        for name in (t if isinstance(t, list) else [t]):
            ld_types[str(name)] = ld_types.get(str(name), 0) + 1

    price_lines = [ln for ln in lines if find_prices(ln)]
    by_ld = offers_from_json_ld(page.html, source="web", source_url=page.url)
    by_text = offers_from_lines(lines, source="web", source_url=page.url)

    text_chars = sum(len(ln) for ln in lines)
    links = extract_links(page.html, page.url)
    m = _TITLE_RE.search(page.html)
    return {
        "url": page.url,
        "status": page.status,
        "bytes": len(page.html),
        "capture": str(saved),
        "title": norm_text(_TAG_RE.sub("", m.group(1))) if m else "",
        # --- 静的HTMLに中身があるか
        "text_lines": len(lines),
        "text_chars": text_chars,
        "first_text_lines": lines[:15],
        "looks_js_rendered": len(page.html) > 20000 and text_chars < 500,
        "state_blobs": [k for k in _STATE_KEYS if k in page.html],
        "api_hints": sorted({m.group(1) for m in _API_HINT_RE.finditer(page.html)})[:15],
        # --- 構造化データ
        "json_ld_nodes": len(ld),
        "json_ld_types": ld_types,
        # --- 抽出できたか
        "price_lines": len(price_lines),
        "price_line_samples": price_lines[:10],
        "offers_from_json_ld": len(by_ld),
        "offers_from_text": len(by_text),
        "offer_samples": [
            {"name": o.name, "price": o.price, "regular_price": o.regular_price,
             "ends_on": o.ends_on}
            for o in (by_ld or by_text)[:10]
        ],
        # --- リンク
        "links_total": len(links),
        "links_with_sale_words": [
            {"url": u, "text": t} for u, t in links
            if any(w.lower() in (u + " " + t).lower() for w in SALE_WORDS)
        ][:20],
    }
