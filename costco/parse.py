"""HTML／メール本文から「商品名・価格・値引き・期間」を取り出す。

公式サイトもメルマガもレイアウトが変わる前提なので、特定のクラス名に依存した
決め打ちパースはしない。次の順で確度の高いものから拾う:

1. JSON-LD（`Product` / `Offer`）があればそれが一番正確
2. 商品番号つきの行
3. 「名前らしい行のすぐ近くに価格がある」という汎用ヒューリスティック（確度は低めに付ける）

3 で拾ったものは `confidence` が低く、サイト側で控えめに表示される。
"""

from __future__ import annotations

import json
import re
from datetime import date
from html import unescape
from html.parser import HTMLParser

from .models import Offer, norm_name, norm_text, today_jst

# ---------------------------------------------------------------- HTML → テキスト

# head は入れない。HTML5 では `</head>` を省略してよく、省略されると
# 「スキップ中」から復帰できずページ全文を捨ててしまう（実際に踏んだ）。
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "section", "article", "h1", "h2",
    "h3", "h4", "h5", "h6", "ul", "ol", "table", "header", "footer", "dt", "dd",
}


# 画像は本文の行として混ぜて運び、あとで位置ごと取り出す。私用領域の文字を
# 目印にしておけば、商品名や価格の判定に紛れ込まない。
IMG_MARK = ""
# src と alt の区切り。タブや空白は norm_text で潰れるので私用領域の文字を使う。
IMG_SEP = ""


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self._skip = 0   # 閉じ忘れたタグを body の開始で必ず断ち切る
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "img" and not self._skip:
            d = dict(attrs)
            src = norm_text(d.get("src"))
            alt = norm_text(d.get("alt"))
            if src:
                # src と alt を1行にまとめて運ぶ。alt には商品名が入っていることが
                # 多く、位置より確実に商品と結びつけられる。
                self.parts.append("\n" + IMG_MARK + src + IMG_SEP + alt + "\n")
            if alt:
                self.parts.append(" " + alt + " ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


_SCRIPT_BLOCK_RE = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1>",
                              re.I | re.S)
_BLOCK_TAG_RE = re.compile(r"</?(?:%s)\b[^>]*>" % "|".join(_BLOCK_TAGS), re.I)
_ANY_TAG_RE = re.compile(r"<[^>]*>")


def _split_lines(text: str) -> list[str]:
    return [ln for ln in (norm_text(x) for x in text.split("\n")) if ln]


_ONLY_YEN_RE = re.compile(r"[¥￥]$")
_ONLY_NUM_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d{2,7}$")


def _merge_split_prices(lines: list[str]) -> tuple[list[str], list[int]]:
    """「¥」と「7,358」のように分断された金額を1行に戻す。

    HTMLメールは通貨記号と数字を別の要素に入れることが多く、そのままだと
    どちらの行も金額として認識できない（実際のメルマガで抽出0件になった）。

    画像の位置を追えるように、元の行番号 → 新しい行番号の対応も返す。
    """
    out: list[str] = []
    index_map: list[int] = []
    for ln in lines:
        if out and _ONLY_YEN_RE.search(out[-1]) and _ONLY_NUM_RE.match(ln):
            out[-1] = out[-1] + ln
        elif out and _ONLY_NUM_RE.match(out[-1]) and ln.startswith("円"):
            out[-1] = out[-1] + ln
        else:
            out.append(ln)
        index_map.append(len(out) - 1)
    return out, index_map


def html_to_lines_with_images(html: str) -> tuple[list[str], list[tuple[int, str, str]]]:
    """テキスト行と、(行番号, 画像URL, alt) の一覧を返す。

    正攻法（HTMLParser）で何も取れなかったときは、閉じ忘れたタグに引きずられて
    全部落とした可能性が高いので、正規表現で乱暴に剥がし直す。抽出0件のまま
    黙って進むのが一番困るため、汚くても中身を拾う方を選ぶ。
    """
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # 壊れたHTMLでも取れたところまで使う
        pass
    raw = _split_lines("".join(p.parts))
    if len([ln for ln in raw if not ln.startswith(IMG_MARK)]) < 3 and len(html) > 2000:
        crude = _SCRIPT_BLOCK_RE.sub(" ", html)
        crude = _BLOCK_TAG_RE.sub("\n", crude)
        crude = unescape(_ANY_TAG_RE.sub(" ", crude))
        raw = _split_lines(crude)

    text: list[str] = []
    pending: list[tuple[int, str, str]] = []
    for ln in raw:
        if ln.startswith(IMG_MARK):
            src, _, alt = ln[len(IMG_MARK):].partition(IMG_SEP)
            pending.append((len(text), src, alt))
        else:
            text.append(ln)

    merged, index_map = _merge_split_prices(text)
    images = [(index_map[i] if i < len(index_map) else len(merged), src, alt)
              for i, src, alt in pending]
    return merged, images


def html_to_lines(html: str) -> list[str]:
    """HTMLを「見た目の行」に近い形のテキスト行へ落とす。"""
    return html_to_lines_with_images(html)[0]


def html_to_text(html: str) -> str:
    return "\n".join(html_to_lines(html))


# ---------------------------------------------------------------- JSON-LD

_LD_RE = re.compile(
    r"<script[^>]+type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
    re.I | re.S,
)


def iter_json_ld(html: str):
    """ページ内の JSON-LD をすべて（`@graph` は展開して）返す。"""
    for m in _LD_RE.finditer(html):
        raw = unescape(m.group(1)).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


# ---------------------------------------------------------------- 価格

# 「1,580円」「¥1,580」「2万9800円」に対応。「1.13kg」等は拾わない。
_PRICE_RE = re.compile(
    r"(?:(?P<man>\d{1,3})\s*万\s*(?P<manrest>[0-9,]{0,5})\s*円)"
    r"|(?:[¥￥]\s*(?P<yen>\d{1,3}(?:,\d{3})+|\d{2,7}))"
    r"|(?:(?P<plain>\d{1,3}(?:,\d{3})+|\d{2,7})\s*円)"
)

# 価格の周辺に出る文脈語。どれが「通常価格」でどれが「セール価格」かの手掛かり。
_REGULAR_HINTS = ("通常", "定価", "参考", "希望小売", "元値", "もと値", "通常価格")
_SALE_HINTS = ("セール", "特価", "割引後", "クーポン適用", "→", "⇒")


def _to_int(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace(",", "")
    return int(s) if s.isdigit() else None


def find_prices(text: str) -> list[int]:
    """テキスト中の金額を出現順に返す。"""
    out: list[int] = []
    for m in _PRICE_RE.finditer(text):
        if m.group("man"):
            rest = _to_int(m.group("manrest")) or 0
            out.append(int(m.group("man")) * 10000 + rest)
        else:
            v = _to_int(m.group("yen") or m.group("plain"))
            if v is not None:
                out.append(v)
    return out


def split_prices(text: str, exclude: int | None = None) -> tuple[int | None, int | None]:
    """(セール価格, 通常価格) を推定する。

    「通常1,980円 → 1,580円」のような書き方を想定し、文脈語が無ければ
    「2つあれば安い方がセール価格」という素直な規則にする。

    `exclude` には値引き額を渡す。「¥69,800（¥15,000引き）」の 15,000 は価格では
    ないので、候補から外さないと値引き額を売価と読み違える。
    """
    hits: list[tuple[int, str]] = []
    for m in _PRICE_RE.finditer(text):
        if m.group("man"):
            v = int(m.group("man")) * 10000 + (_to_int(m.group("manrest")) or 0)
        else:
            v = _to_int(m.group("yen") or m.group("plain"))
        if v is None:
            continue
        left = text[max(0, m.start() - 12):m.start()]
        hits.append((v, left))

    if exclude is not None and len(hits) > 1:
        kept = [h for h in hits if h[0] != exclude]
        if kept:
            hits = kept

    if not hits:
        return None, None
    if len(hits) == 1:
        v, left = hits[0]
        if any(h in left for h in _REGULAR_HINTS):
            return None, v
        return v, None

    regular = next((v for v, left in hits if any(h in left for h in _REGULAR_HINTS)), None)
    sale = next((v for v, left in hits if any(h in left for h in _SALE_HINTS)), None)
    if regular is not None and sale is not None:
        return sale, regular
    values = [v for v, _ in hits]
    if regular is not None:
        cheaper = [v for v in values if v < regular]
        return (min(cheaper) if cheaper else None), regular
    if sale is not None:
        dearer = [v for v in values if v > sale]
        return sale, (max(dearer) if dearer else None)
    return min(values), max(values) if max(values) != min(values) else None


# ---------------------------------------------------------------- 値引き

_DISCOUNT_YEN_RE = re.compile(
    r"(?:[¥￥]\s*)?(\d{1,3}(?:,\d{3})+|\d{2,6})\s*円?\s*(?:引き?|OFF|off|オフ|値引き|割引)")
_DISCOUNT_PCT_RE = re.compile(r"(\d{1,2})\s*[%％]\s*(?:OFF|off|オフ|引き?|割引)?")


def find_discount(text: str) -> tuple[int | None, float | None]:
    """(値引き額, 値引き率) を返す。どちらか片方しか書かれていないことも多い。"""
    amount = None
    m = _DISCOUNT_YEN_RE.search(text)
    if m:
        amount = _to_int(m.group(1))
    rate = None
    m = _DISCOUNT_PCT_RE.search(text)
    if m:
        pct = int(m.group(1))
        if 1 <= pct <= 95:
            rate = pct / 100.0
    return amount, rate


# ---------------------------------------------------------------- 期間

# 区切りに "." や "-" を許すと「1.13kg」「V-12」を日付と誤読するので、
# ISO形式だけ別枝にして、和文側は「月」か「/」に限定する。
_DATE_RE = re.compile(
    r"(?P<iy>20\d{2})-(?P<im>\d{1,2})-(?P<id>\d{1,2})"
    r"|(?:(?P<y>20\d{2})\s*年\s*)?(?P<m>\d{1,2})\s*(?:月|[/／])\s*(?P<d>\d{1,2})\s*日?"
    r"(?:\s*[（(][月火水木金土日][）)])?(?![0-9a-zA-Z])"
)
_RANGE_SEP = ("～", "〜", "~", "-", "−", "–", "—", "ー", "から", "to", "→")
_END_ONLY = ("まで", "迄", "終了", "期限")


def _mk_date(y: int | None, m: int, d: int, base: date) -> date | None:
    """年が省略されているときに、基準日から見て自然な年を当てる。"""
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    if y is None:
        for cand in (base.year, base.year + 1, base.year - 1):
            try:
                got = date(cand, m, d)
            except ValueError:
                continue
            # 基準日の前後半年に入るものを採る（年末年始をまたぐチラシ対策）
            if -185 <= (got - base).days <= 185:
                return got
        y = base.year
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_period(text: str, base: date | None = None) -> tuple[str, str]:
    """(開始日, 終了日) を ISO 文字列で返す。取れなければ空文字。"""
    base = base or today_jst()
    t = norm_text(text)
    hits = []
    for m in _DATE_RE.finditer(t):
        if m.group("iy"):
            y, mo, dy = int(m.group("iy")), int(m.group("im")), int(m.group("id"))
        else:
            y, mo, dy = _to_int(m.group("y")), int(m.group("m")), int(m.group("d"))
        d = _mk_date(y, mo, dy, base)
        if d:
            hits.append((m.start(), m.end(), d))
    if not hits:
        return "", ""

    # 2つの日付が近接し、間に範囲記号があれば期間とみなす
    for (s1, e1, d1), (s2, e2, d2) in zip(hits, hits[1:]):
        between = t[e1:s2]
        if len(between) <= 12 and any(sep in between for sep in _RANGE_SEP) and d2 >= d1:
            return d1.isoformat(), d2.isoformat()

    s, e, d = hits[0]
    tail = t[e:e + 8]
    if any(w in tail for w in _END_ONLY):
        return "", d.isoformat()
    return d.isoformat(), ""


# ---------------------------------------------------------------- 商品番号

_ITEM_NO_RE = re.compile(
    r"(?:商品\s*番号|商品\s*コード|品番|アイテム\s*番号|item\s*(?:#|no\.?|number))"
    r"\s*[:：#]?\s*(\d{4,9})", re.I)


def find_item_no(text: str) -> str:
    m = _ITEM_NO_RE.search(norm_text(text))
    return m.group(1) if m else ""


# ---------------------------------------------------------------- Offer 組み立て

# 値段まわりの飾り語。これしか残らない行は商品名ではない。
_PRICE_WORDS_RE = re.compile(
    r"通常価格|通常|定価|税込|税抜|本体価格|参考価格|希望小売価格|会員価格|セール価格|特価"
    r"|割引|値引き?|クーポン|OFF|オフ|引き|price", re.I)
_NAME_NOISE_RE = re.compile(r"[\s　,.\-~〜～ー_／/円¥￥%％()（）→⇒:：!！*＊]")

_BOILERPLATE = (
    "会員", "ログイン", "カート", "検索", "メニュー", "続きを読む", "詳細はこちら",
    "プライバシー", "利用規約", "配信停止", "お問い合わせ", "copyright", "すべて表示",
    "カテゴリー", "倉庫店を選択", "セール期間", "販売期間", "有効期限", "期間限定",
    "在庫状況", "予告なく", "画像はイメージ", "shop now", "詳しくはこちら",
    "メールが正しく表示されない",
)


def _looks_like_name(line: str) -> bool:
    """商品名の行っぽいか。価格だけ・日付だけ・定型文の行を弾く。"""
    t = norm_text(line)
    if not (3 <= len(t) <= 80):
        return False
    low = t.lower()
    if any(b in low for b in _BOILERPLATE):
        return False
    core = _PRICE_RE.sub("", t)
    core = _DATE_RE.sub("", core)
    core = _PRICE_WORDS_RE.sub("", core)
    core = _NAME_NOISE_RE.sub("", core)
    if len(core) < 3:
        return False
    # 数字と記号ばかりの行（型番の羅列など）は名前として扱わない
    return len(re.sub(r"\d", "", core)) >= 2


def offers_from_json_ld(html: str, *, source: str, source_url: str = "",
                        base: date | None = None) -> list[Offer]:
    """JSON-LD の Product からセール情報を作る。取れれば一番確実。"""
    out: list[Offer] = []
    for node in iter_json_ld(html):
        types = node.get("@type") or ""
        types = types if isinstance(types, list) else [types]
        if not any(str(t).lower() == "product" for t in types):
            continue
        name = norm_text(node.get("name"))
        if not name:
            continue
        offers = node.get("offers") or {}
        offers = offers[0] if isinstance(offers, list) and offers else offers
        if not isinstance(offers, dict):
            offers = {}
        price = _to_int(str(offers.get("price", "")).split(".")[0]) if offers.get("price") else None
        o = Offer(
            name=name,
            item_no=norm_text(node.get("sku") or node.get("productID") or ""),
            price=price,
            source=source,
            source_url=norm_text(offers.get("url") or node.get("url") or source_url),
            note=norm_text(node.get("description"))[:200],
            confidence=0.95,
        )
        valid_to = norm_text(offers.get("priceValidUntil") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", valid_to):
            o.ends_on = valid_to
        out.append(o.normalize(base))
    return out


def offers_from_lines(lines: list[str], *, source: str, source_url: str = "",
                      source_label: str = "", base: date | None = None,
                      kind: str | None = None) -> list[Offer]:
    """テキスト行から総当たりでセール情報を組み立てる（確度低め）。

    価格を含む行を軸に、その行と直前の数行を1商品ぶんの塊として扱う。
    ページ全体に効いている期間（「8/1〜8/14」がヘッダに1回だけ書いてある等）は
    最後に、期間の取れなかった商品へ流し込む。
    """
    base = base or today_jst()
    out: list[Offer] = []

    for i, line in enumerate(lines):
        if not find_prices(line):
            continue

        # 商品名の行を先に決め、そこから価格行の次までを「1商品ぶんの塊」とする。
        # 塊を広く取ると、隣の商品の商品番号や期間を吸ってしまう。
        name, name_idx = "", i
        for back in range(i, max(-1, i - 4), -1):
            cand = _PRICE_RE.sub("", lines[back]).strip(" 　-–—~〜:：")
            if _looks_like_name(cand):
                name, name_idx = cand, back
                break
        if not name:
            continue
        blob = " ".join(lines[name_idx:i + 2])

        amount, rate = find_discount(blob)
        price, regular = split_prices(line, exclude=amount)
        if len(find_prices(line)) == 1 and regular is None:
            # 「通常◯円」が別の行に書いてあることが多いので塊で取り直す
            p2, r2 = split_prices(blob, exclude=amount)
            if r2 is not None:
                price, regular = p2, r2
        if amount is None and rate is not None and regular:
            amount = int(round(regular * rate))
        if price is None and regular and amount:
            price = regular - amount

        starts, ends = find_period(blob, base)
        o = Offer(
            name=name,
            item_no=find_item_no(blob),
            price=price,
            regular_price=regular,
            discount=amount,
            starts_on=starts,
            ends_on=ends,
            source=source,
            source_url=source_url,
            source_label=source_label,
            confidence=0.55 if (price and (regular or amount)) else 0.4,
        )
        if kind:
            o.kind = kind
        out.append(o.normalize(base))

    page_start, page_end = find_period("\n".join(lines[:40]), base)
    if page_end:
        for o in out:
            if not o.ends_on:
                o.starts_on = o.starts_on or page_start
                o.ends_on = page_end

    return dedupe(_unshare_item_no(out))


def _unshare_item_no(offers: list[Offer]) -> list[Offer]:
    """同じ商品番号が別商品に付いてしまった場合、最初の1件だけに残す。

    商品番号は近くの行から拾うので、たまに隣の商品のものを掴む。放置すると
    別商品が同じキーになり、価格履歴が混ざって台無しになる。
    """
    owner: dict[str, str] = {}
    for o in offers:
        if not o.item_no:
            continue
        holder = owner.setdefault(o.item_no, norm_text(o.name))
        if holder != norm_text(o.name):
            o.item_no = ""
            o.key = ""
            o.confidence = min(o.confidence, 0.45)
            o.normalize()
    return offers


_ITEM_LINE_RE = re.compile(
    r"(?:ITEM|商品\s*番号|商品\s*コード|品番|アイテム\s*番号)\s*[#＃:：]?\s*(\d{4,9})", re.I)
_JA_RE = re.compile(r"[ぁ-んァ-ヶ一-龥]")

# メルマガは「Price」「Off」というラベルを金額の *前の行* に置く。
#   Price / ¥3,498 / Off / ¥680 / ¥2,818
#   （通常価格）      （値引き額）  （売価）
_LABEL_REGULAR_RE = re.compile(
    r"^(?:price|通常価格|通常|定価|参考価格|希望小売価格|メーカー希望小売価格)$", re.I)
_LABEL_OFF_RE = re.compile(r"^(?:off|割引|値引き?|引き|お値引き)$", re.I)


def prices_from_block(block: list[str]) -> tuple[int | None, int | None, int | None]:
    """1商品ぶんの行から (売価, 通常価格, 値引き額) を読む。

    ラベルが金額の前に来る形（Price→金額、Off→金額）を最優先で読み、
    ラベルが無ければ「安い方が売価」という素直な規則に落とす。
    """
    regular = discount = None
    plain: list[int] = []
    pending: str | None = None

    for line in block:
        if _LABEL_REGULAR_RE.match(line):
            pending = "regular"
            continue
        if _LABEL_OFF_RE.match(line):
            pending = "off"
            continue
        found = find_prices(line)
        if not found:
            continue
        # 同じ行に「¥680 OFF」のように書かれている場合はそちらを優先
        inline, _ = find_discount(line)
        if inline is not None and inline in found:
            discount = discount if discount is not None else inline
            found = [v for v in found if v != inline]
        if found and pending == "regular":
            regular = regular if regular is not None else found.pop(0)
        elif found and pending == "off":
            discount = discount if discount is not None else found.pop(0)
        plain.extend(found)
        pending = None

    known = {v for v in (regular, discount) if v is not None}
    rest = [v for v in plain if v not in known]
    price = None

    if regular is not None and discount is not None:
        # 通常価格と値引き額が両方ラベル付きで取れているなら、売価は引き算と
        # 一致するはず。合わない金額は隣の商品のものなので採らない。
        expected = regular - discount
        near = [v for v in rest if abs(v - expected) <= max(50, expected * 0.02)]
        price = near[0] if near else expected
    elif regular is not None:
        # 通常価格だけ分かっている場合、半額を下回る金額は別商品の疑いが濃い
        plausible = [v for v in rest if regular * 0.5 <= v <= regular]
        price = min(plausible) if plausible else regular
        if not plausible:
            regular = None   # 割引の書かれていない「価格」はただの売価
    elif discount is not None and rest:
        price = rest[0]
    elif len(rest) >= 2:
        # ラベルが無く金額が並ぶだけの形は「高い方が通常価格・安い方が売価」。
        # ただし読み範囲が隣の商品にはみ出していると、無関係な2つを対にして
        # 「90%引き」のような嘘になる（実物で踏んだ）。割引率が常識外なら
        # 対にせず、商品番号に一番近い最初の金額だけを採る。
        hi, lo = max(rest), min(rest)
        regular, price = (hi, lo) if lo * 2 >= hi else (None, rest[0])
    elif rest:
        price = rest[0]

    # 最後の砦: どの経路を通っても半額超えの値引きは名乗らない
    if price is not None and regular is not None and price * 2 < regular:
        regular = discount = None
    return price, regular, discount


def _pick_image(images: list[tuple[int, str, str]], name: str,
                lo: int, anchor: int, hi: int) -> str:
    """この商品の画像URLを1つ選ぶ。

    位置だけで選ぶと隣の商品の写真を掴む（実物で、真珠のネックレスにソファの
    写真が付いた）。メルマガの `<img>` は alt に商品名を入れているので、
    **まず alt で突き合わせる**。当たらなければ位置で選ぶが、その場合も
    商品番号より前にある画像に限る。当てずっぽうで別商品の写真を出すより、
    写真なしのほうがましなため。
    """
    key = norm_name(name)
    if len(key) >= 4:
        for _, src, alt in images:
            a = norm_name(alt)
            if a and (a == key or (len(a) >= 4 and (a in key or key in a))):
                return src
    before = [src for j, src, _ in images if lo <= j <= anchor]
    return before[-1] if before else ""


def offers_from_item_numbers(lines: list[str], *, source: str, source_url: str = "",
                             source_label: str = "", base: date | None = None,
                             kind: str | None = None,
                             images: list[tuple[int, str, str]] | None = None) -> list[Offer]:
    """`ITEM #28137` の行を軸に1商品ずつ切り出す。

    コストコのメルマガは商品ごとに必ず商品番号が入る。価格行を軸にするより
    ずっと安定するうえ、商品番号が確実に取れるので価格履歴も繋がる。

    並びは概ね次の形:

        オキシクリーン 5.26kg        ← 商品名（日本語）
        Oxiclean Max Efficiency     ← 英語名
        ITEM #28137
        ¥2,898  /  ¥500 OFF         ← 価格・値引き
        Shop Now >
    """
    base = base or today_jst()
    anchors = [(i, m.group(1)) for i, ln in enumerate(lines)
               if (m := _ITEM_LINE_RE.search(ln))]
    if not anchors:
        return []

    def _has_money(ln: str) -> bool:
        if find_prices(ln):
            return True
        a, r = find_discount(ln)
        return a is not None or r is not None

    # 商品番号どうしが金額を挟まず隣接していたら「複数列レイアウト」。
    #   名前A / 名前B / …A… ITEM #A / …B… ITEM #B / Aの価格 / Shop Now / Bの価格
    # と、価格ブロックが**まとめて後ろに**左から順で並ぶ（実物で確認）。
    # 「商品番号の直後の価格＝その商品」の仮定だと、Aの価格がBに付き、
    # TEMPURの枕が隣のシーツの1,748円になった。
    runs: list[list[int]] = [[0]]
    for n in range(1, len(anchors)):
        between = lines[anchors[n - 1][0] + 1:anchors[n][0]]
        if len(between) <= 5 and not any(_has_money(ln) for ln in between):
            runs[-1].append(n)
        else:
            runs.append([n])

    _SHOPNOW_RE = re.compile(r"shop\s*now|詳しくはこちら|購入はこちら", re.I)

    # 各 run の価格ブロック（run内の各商品ぶん、左から順）
    run_segments: dict[int, list[list[str]]] = {}
    for run in runs:
        if len(run) < 2:
            continue
        last_i = anchors[run[-1]][0]
        stop = anchors[run[-1] + 1][0] if run[-1] + 1 < len(anchors) else len(lines)
        segs: list[list[str]] = []
        cur: list[str] = []
        for ln in lines[last_i + 1:stop]:
            if _SHOPNOW_RE.search(ln):
                if cur:
                    segs.append(cur)
                cur = []
                continue
            cur.append(ln)
        if cur:
            segs.append(cur)
        money_segs = [sg for sg in segs if any(_has_money(ln) for ln in sg)]
        for k, n in enumerate(run):
            run_segments[n] = [money_segs[k]] if k < len(money_segs) else []

    out: list[Offer] = []
    for n, (i, item_no) in enumerate(anchors):
        nxt = anchors[n + 1][0] if n + 1 < len(anchors) else len(lines)
        if n in run_segments:
            block = run_segments[n][0] if run_segments[n] else []
        else:
            # 単列レイアウト: 価格は商品番号の後ろに並ぶ。次の商品番号か
            # 9行先までを読む。多少はみ出しても prices_from_block が
            # 「最初のラベルが有効」で拾うので隣の商品の値に引きずられない。
            hi = max(i + 1, min(nxt, i + 9))
            block = lines[i:hi]

        # 名前は商品番号の直前から遡って探す。英語名が併記されるので
        # 「日本語を含む行」を優先し、無ければ名前らしい最後の行を使う。
        lo = max(anchors[n - 1][0] + 1 if n else 0, i - 5)
        cands = [ln for ln in lines[lo:i] if _looks_like_name(ln)]
        name = next((ln for ln in reversed(cands) if _JA_RE.search(ln)),
                    cands[-1] if cands else "")
        if not name:
            continue

        price, regular, amount = prices_from_block(block)
        if price is None and regular is None and n not in run_segments:
            # 価格が商品番号より前に書かれている号もある。後ろで取れなければ
            # 商品名から商品番号までを読み直す。複数列の商品では前を読むと
            # 前のペアの価格を掴むのでやらない。
            price, regular, amount = prices_from_block(lines[lo:i + 1])
        blob = " ".join(block)
        if amount is None:
            amount, rate = find_discount(blob)
            if amount is None and rate is not None and regular:
                amount = int(round(regular * rate))
        starts, ends = find_period(blob, base)

        o = Offer(
            name=name, item_no=item_no, price=price, regular_price=regular,
            discount=amount, starts_on=starts, ends_on=ends, source=source,
            source_url=source_url, source_label=source_label,
            image_url=_pick_image(images or [], name,
                                  anchors[n - 1][0] + 1 if n else 0, i, nxt),
            confidence=0.8 if price is not None else 0.5,
        )
        if kind:
            o.kind = kind
        out.append(o.normalize(base))

    page_start, page_end = find_period("\n".join(lines[:40]), base)
    if page_end:
        for o in out:
            if not o.ends_on:
                o.starts_on = o.starts_on or page_start
                o.ends_on = page_end
    return dedupe(out)


def dedupe(offers: list[Offer]) -> list[Offer]:
    """同一ページ内の重複（同じ商品が画像とテキストで2回出る等）を潰す。"""
    best: dict[tuple, Offer] = {}
    for o in offers:
        k = (o.key, o.kind, o.starts_on, o.ends_on)
        cur = best.get(k)
        if cur is None or _richness(o) > _richness(cur):
            best[k] = o
    return list(best.values())


def _richness(o: Offer) -> tuple:
    return (
        o.confidence,
        sum(1 for v in (o.price, o.regular_price, o.discount) if v is not None),
        1 if o.item_no else 0,
        len(o.name),
    )
