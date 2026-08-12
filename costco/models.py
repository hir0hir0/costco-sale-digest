"""セール情報の正規化データ構造。

外部依存ゼロ。全部そのままJSONに落ちる形にしてある（`targets.json` と同じ流儀）。

セールもクーポンも実体は「ある期間、ある商品が安い」という同じ話なので、
`kind` フィールドで区別する一つの `Offer` にまとめている。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9), "JST")

KIND_SALE = "sale"
KIND_COUPON = "coupon"
KINDS = (KIND_SALE, KIND_COUPON)

# 全倉庫店共通のセールを表す番人。特定店舗限定なら店名を入れる。
ALL_WAREHOUSES = "all"


def today_jst() -> date:
    """日本時間の今日。GitHub Actions（UTC）で動かしても日付がズレないように。"""
    return datetime.now(JST).date()


def now_jst_iso() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- 文字列正規化

_SPACE_RE = re.compile(r"[\s　]+")


def norm_text(s: object) -> str:
    """全角英数・半角カナを正規化し、空白を1個に潰す。"""
    if s is None:
        return ""
    t = unicodedata.normalize("NFKC", str(s))
    t = t.replace("​", "").replace("﻿", "")
    return _SPACE_RE.sub(" ", t).strip()


_NOISE_RE = re.compile(r"[\s　\-‐‑‒–—―ー_･・,、.。/／()（）\[\]【】「」『』*＊+＋:：;；!！?？'\"”“]+")


def norm_name(s: object) -> str:
    """比較・キー生成用の商品名。表記ゆれ（空白/記号/大小）を吸収する。

    「カークランド シグネチャー ミックスナッツ 1.13kg」と
    「KIRKLAND SIGNATURE ミックスナッツ1.13kg」は別物として残る（英日は正規化しない）。
    そこまで踏み込むと誤マージのほうが痛いので、記号と大小だけを潰す。
    """
    return _NOISE_RE.sub("", norm_text(s).lower())


def offer_key(item_no: str | None, name: str) -> str:
    """商品の安定キー。商品番号があれば最優先、無ければ正規化名のハッシュ。

    商品番号はコストコの値札・レシートに出る番号で、これが取れていれば
    店舗やチラシをまたいでも同じ商品として履歴が繋がる。
    """
    item_no = norm_text(item_no)
    if item_no:
        return "no:" + item_no
    h = hashlib.sha1(norm_name(name).encode("utf-8")).hexdigest()[:16]
    return "nm:" + h


# ---------------------------------------------------------------- カテゴリ推定

# 上から順に見て最初に当たったものを採用する（食品系を先に置く）。
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("肉・魚", ("牛", "豚", "鶏", "ビーフ", "ポーク", "チキン", "肉", "サーモン", "まぐろ", "マグロ",
              "えび", "エビ", "刺身", "寿司", "ハム", "ベーコン", "ソーセージ", "ツナ", "鮭",
              "数の子", "ランチョンミート", "スパム", "たらこ", "明太")),
    # 「トマトケチャップ」を野菜に入れないため、調味料を野菜より先に見る
    ("調味料・食材", ("醤油", "しょうゆ", "味噌", "みそ", "オリーブオイル", "オイル", "油", "塩", "砂糖",
                 "ソース", "ケチャップ", "マヨネーズ", "スパイス", "だし", "つゆ", "ドレッシング")),
    ("野菜・果物", ("野菜", "サラダ", "トマト", "レタス", "じゃがいも", "玉ねぎ", "バナナ", "りんご",
                "ぶどう", "みかん", "いちご", "ブルーベリー", "アボカド", "フルーツ", "果物")),
    ("乳製品・卵", ("牛乳", "ミルク", "チーズ", "ヨーグルト", "バター", "生クリーム", "卵", "たまご")),
    # 「パン」単体は「パンツ」に当たるので使わない
    ("パン・米・麺", ("食パン", "菓子パン", "ディナーロール", "ベーグル", "クロワッサン", "マフィン",
                 "バゲット", "パスタ", "麺", "うどん", "そば", "ラーメン", "シリアル",
                 "オートミール", "パン粉")),
    ("惣菜・冷凍", ("ピザ", "冷凍", "惣菜", "デリ", "ロティサリー", "餃子", "弁当", "グラタン", "スープ")),
    ("お菓子", ("チョコ", "クッキー", "スナック", "ポテトチップ", "ナッツ", "アーモンド", "キャンディ",
             "グミ", "せんべい", "菓子", "ケーキ", "アイス", "ガム", "ゼリー", "甘栗", "イチジク",
             "詰め合わせ", "ドーナツ", "プリン")),
    ("飲料・酒", ("水", "ウォーター", "コーヒー", "紅茶", "お茶", "ジュース", "炭酸", "コーラ",
              "ビール", "ワイン", "ウイスキー", "日本酒", "焼酎", "ハイボール", "ドリンク",
              "豆乳", "サイダー", "茶", "ミネラルウォーター", "エナジー")),
    ("日用品", ("トイレットペーパー", "バスティシュ", "ペーパータオル", "ティッシュ", "洗剤", "柔軟剤",
             "ラップ", "ジップロック", "ゴミ袋", "電池", "マスク", "除菌", "キッチンペーパー",
             "タオル", "漂白", "ハンドソープ", "スポンジ", "カビキラー", "パイプユニッシュ",
             "ライナー", "ナプキン", "消臭", "芳香")),
    ("ヘルス＆ビューティー", ("シャンプー", "コンディショナー", "石鹸", "ボディソープ", "歯磨き", "歯ブラシ",
                     "化粧", "サプリ", "ビタミン", "プロテイン", "医薬", "オムツ", "おむつ", "コンタクト",
                     "洗顔", "美顔", "ドライヤー", "保湿", "熱さま", "UV", "日焼け")),
    ("家電・PC", ("テレビ", "モニター", "掃除機", "クリーナー", "冷蔵庫", "空気清浄", "家電",
               "ヘッドホン", "イヤホン", "スピーカー", "タブレット", "パソコン", "ノートPC",
               "MacBook", "ノートブック", "カメラ", "プロジェクター", "モバイルバッテリー",
               "ポータブル電源", "充電", "マウス", "キーボード", "サーキュレーター", "扇風機",
               "ハンディファン", "ミキサー", "炊飯", "電子レンジ", "ゲーミング", "Switch",
               "PlayStation", "プリンター", "ルーター", "美容器", "シャワーヘッド")),
    ("家具・寝具", ("マットレス", "ベッド", "ソファ", "チェア", "チェアー", "テーブル", "デスク", "収納",
               "ラグ", "カーペット", "枕", "ピロー", "布団", "寝具", "シェルフ", "ラック",
               "バスマット", "玄関マット", "シーツ", "敷きパッド", "パッド")),
    ("宝飾・時計", ("ネックレス", "リング", "ブレスレット", "アンクレット", "ピアス", "イヤリング",
               "喜平", "ダイヤモンド", "真珠", "アコヤ", "K18", "プラチナ900", "腕時計",
               "ウォッチ", "Watch")),
    ("アウトドア・レジャー", ("テント", "タープ", "クーラーボックス", "キャンプ", "プール", "ビーチ",
                    "浮き輪", "チューブ", "BBQ", "バーベキュー", "アウトドア", "レジャー")),
    ("衣料品", ("シャツ", "パンツ", "ジョガー", "ジャケット", "靴", "スニーカー", "ソックス", "靴下",
             "インナー", "パジャマ", "ウェア", "ドレス", "レディース", "メンズ", "キッズ")),
    ("家電・雑貨", ("スーツケース", "バッグ", "財布", "食器", "キッチン用品", "工具", "文具")),
    ("タイヤ・カー用品", ("タイヤ", "ホイール", "カーバッテリー", "エンジンオイル", "ワイパー")),
]

CATEGORY_OTHER = "その他"
CATEGORIES = [name for name, _ in CATEGORY_RULES] + [CATEGORY_OTHER]


def guess_category(name: str) -> str:
    """商品名からカテゴリを推定する。外れても絞り込みが少し鈍るだけ。"""
    t = norm_text(name)
    for label, words in CATEGORY_RULES:
        for w in words:
            if w in t:
                return label
    return CATEGORY_OTHER


# ---------------------------------------------------------------- Offer

@dataclass
class Offer:
    """1件の「安売り」。セールもクーポンもこれ一つで表す。"""

    name: str
    key: str = ""
    kind: str = KIND_SALE
    item_no: str = ""
    category: str = ""
    price: int | None = None            # セール後価格（税込・円）
    regular_price: int | None = None    # 通常価格
    discount: int | None = None         # 値引き額（円）
    starts_on: str = ""                 # YYYY-MM-DD
    ends_on: str = ""                   # YYYY-MM-DD（含む）
    warehouse: str = ALL_WAREHOUSES
    source: str = ""                    # "web" / "mail" / "manual"
    source_url: str = ""
    source_label: str = ""              # 「メルマガ 8/12号」など人間向けの出所
    image_url: str = ""                 # 取り込み元の画像URL
    image: str = ""                     # site/img/ に落とした後のファイル名
    note: str = ""
    first_seen: str = ""                # 初めて観測した日 YYYY-MM-DD
    last_seen: str = ""                 # 最後に観測した日
    confidence: float = 1.0             # 抽出の確からしさ（0-1）。低いものはサイトで控えめに出す

    # ---- 派生値（保存はするがマージ時に必ず再計算する）
    def normalize(self, seen_on: date | None = None) -> "Offer":
        """欠けている項目を埋め、矛盾を直した自分自身を返す。"""
        self.name = norm_text(self.name)
        self.item_no = re.sub(r"\D", "", norm_text(self.item_no))
        self.note = norm_text(self.note)
        self.warehouse = norm_text(self.warehouse) or ALL_WAREHOUSES
        if self.kind not in KINDS:
            self.kind = KIND_SALE
        if not self.category:
            self.category = guess_category(self.name)
        if not self.key:
            self.key = offer_key(self.item_no, self.name)

        # 価格・通常価格・値引き額は2つ分かれば3つ目が決まる
        if self.discount is None and self.price is not None and self.regular_price is not None:
            self.discount = self.regular_price - self.price
        elif self.price is None and self.regular_price is not None and self.discount is not None:
            self.price = self.regular_price - self.discount
        elif self.regular_price is None and self.price is not None and self.discount is not None:
            self.regular_price = self.price + self.discount

        # 明らかに壊れている値引きは捨てる（負・通常価格超え）
        if self.discount is not None and (self.discount <= 0 or (
                self.regular_price is not None and self.discount > self.regular_price)):
            self.discount = None
            if self.price is not None and self.regular_price is not None:
                d = self.regular_price - self.price
                self.discount = d if d > 0 else None

        d = (seen_on or today_jst()).isoformat()
        self.first_seen = self.first_seen or d
        self.last_seen = self.last_seen or d
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 0.5
        return self

    # ---- 期間まわり
    def end_date(self) -> date | None:
        return _parse_iso(self.ends_on)

    def start_date(self) -> date | None:
        return _parse_iso(self.starts_on)

    def is_active(self, on: date | None = None) -> bool:
        """指定日に有効か。期間不明のものは有効扱い（落とすと情報が消えるため）。"""
        on = on or today_jst()
        s, e = self.start_date(), self.end_date()
        if s and on < s:
            return False
        if e and on > e:
            return False
        return True

    def days_left(self, on: date | None = None) -> int | None:
        e = self.end_date()
        if not e:
            return None
        return (e - (on or today_jst())).days

    def discount_rate(self) -> float | None:
        if self.discount and self.regular_price:
            return self.discount / self.regular_price
        return None

    # ---- 直列化
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Offer":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def _parse_iso(s: str) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- マージ

# 同じ商品の2件が「同じ売り出し」かどうかの判定に使う。
def same_run(a: Offer, b: Offer) -> bool:
    """同一商品の2件が同じセール期間を指しているか。

    期間が両方分かっていれば期間が重なるか、片方でも不明なら同じとみなす
    （不明なものを別扱いすると同じセールが二重に並ぶ）。
    """
    # kind は見ない。同じ商品・同じ期間の値引きが、チラシでは「セール」、
    # クーポン号では「クーポン」として届くことがあり、別扱いにすると同じ品が
    # 2行並ぶ。買う側にとっては1件なので、まとめて「クーポン」表示に寄せる。
    if a.key != b.key or a.warehouse != b.warehouse:
        return False
    as_, ae = a.start_date(), a.end_date()
    bs, be = b.start_date(), b.end_date()
    if ae and be and ae != be:
        # 終了日が違えば別の売り出し。開始日はチラシに書かれないことが多いので、
        # 「開始日が両方不明だから同じ」と扱うと数か月後のセールまで一つに潰れる。
        return False
    if ae and bs and bs > ae:
        return False
    if be and as_ and as_ > be:
        return False
    return True


def merge_offer(old: Offer, new: Offer) -> Offer:
    """既存の1件に新しい観測を重ねる。情報量が増える方向にだけ更新する。"""
    out = Offer.from_dict(old.to_dict())
    seens = [x for x in (old.first_seen, new.first_seen) if x]
    out.first_seen = min(seens) if seens else ""
    out.last_seen = max(old.last_seen or "", new.last_seen or "")

    # 名前は長いほう（省略されていない方）を採る
    if len(norm_text(new.name)) > len(norm_text(out.name)):
        out.name = new.name
    for f in ("item_no", "starts_on", "ends_on", "note", "source_url", "source_label",
              "image_url", "image"):
        if not getattr(out, f) and getattr(new, f):
            setattr(out, f, getattr(new, f))
    # 価格は新しい観測を正とする（値下がりを追いたいので）
    for f in ("price", "regular_price", "discount"):
        v = getattr(new, f)
        if v is not None:
            setattr(out, f, v)
    if KIND_COUPON in (out.kind, new.kind):
        out.kind = KIND_COUPON
    if new.confidence > out.confidence:
        out.confidence = new.confidence
        out.source = new.source or out.source
    return out.normalize()
