"""静的サイトの書き出し。

`site/` に置くのは3つだけ:

- `index.html` … データを埋め込んだ単一ファイル。CSS/JSは外部を一切読まない
- `data.json` … 同じ中身。他から使いたいとき用
- `.nojekyll` … Pages に余計な変換をさせない

商品写真だけは例外扱いで、`costco_sources.json` の `images` に従う:

- `"link"`（既定） … コストコのCDNのURLをそのまま `<img src>` に入れる。
  リポジトリに画像を溜めず、複製もしない。先方が消せばその時点で壊れる
- `true`          … `site/img/` にダウンロードして相対参照（オフラインでも出る）
- `false`         … 写真なし
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import CATEGORIES, KIND_COUPON, KIND_SALE, now_jst_iso, today_jst
from .store import Store

TEMPLATE = Path(__file__).resolve().parent / "site_template.html"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "site"

SITE_TITLE = "コストコ 安売りまとめ"
SITE_TAGLINE = "セール・クーポン・値下がりを1ページで"


IMAGES_LINK = "link"
IMAGES_DOWNLOAD = "download"
IMAGES_OFF = "off"


def images_mode(conf_value) -> str:
    """`costco_sources.json` の `images` の値をモード名に正規化する。"""
    if conf_value in (IMAGES_LINK, None):
        return IMAGES_LINK
    if conf_value in (True, IMAGES_DOWNLOAD):
        return IMAGES_DOWNLOAD
    return IMAGES_OFF


def _image_src(o, mode: str) -> str:
    if mode == IMAGES_LINK and o.image_url.startswith("http"):
        return o.image_url
    if mode != IMAGES_OFF and o.image:
        return "img/" + o.image
    return ""


def site_data(store: Store, on: date | None = None,
              images: str = IMAGES_LINK) -> dict:
    """サイトが読むJSONを組み立てる。並べ替えや絞り込みはブラウザ側でやる。"""
    on = on or today_jst()
    offers = []
    # 価格も値引きも読めなかったものは「安売りまとめ」として意味がないので載せない。
    # 捨てた件数は build 時に表示する（黙って減らさない）。
    listed = [o for o in store.active(on) if o.price is not None or o.discount is not None]
    for o in sorted(listed, key=lambda x: (x.ends_on or "9999-12-31", x.name)):
        stats = store.price_stats(o.key)
        prev = stats["prev"]
        lowest = stats["lowest"]
        d = o.to_dict()
        d.update({
            "days_left": o.days_left(on),
            "discount_rate": round(o.discount_rate(), 3) if o.discount_rate() else None,
            "prev_price": prev,
            "lowest": lowest,
            "is_lowest": bool(o.price is not None and lowest is not None
                              and o.price <= lowest and stats["count"] > 1),
            "drop": (prev - o.price) if (prev and o.price and o.price < prev) else None,
            "is_new": o.first_seen == on.isoformat(),
            "history": stats["points"],
            "image_src": _image_src(o, images),
        })
        # サイトが使うのは組み立て済みの image_src だけ
        d.pop("image_url", None)
        d.pop("image", None)
        offers.append(d)

    used = [c for c in CATEGORIES if any(o["category"] == c for o in offers)]
    return {
        "title": SITE_TITLE,
        "tagline": SITE_TAGLINE,
        "generated_at": now_jst_iso(),
        "today": on.isoformat(),
        "categories": used,
        "offers": offers,
        "hidden_no_price": len(store.active(on)) - len(listed),
        "stats": {
            "total": len(offers),
            "sale": sum(1 for o in offers if o["kind"] == KIND_SALE),
            "coupon": sum(1 for o in offers if o["kind"] == KIND_COUPON),
            "ending_soon": sum(1 for o in offers
                               if o["days_left"] is not None and 0 <= o["days_left"] <= 3),
            "lowest": sum(1 for o in offers if o["is_lowest"]),
        },
    }


def build_site(store: Store, out_dir: Path | str = DEFAULT_OUT,
               on: date | None = None, images: str = IMAGES_LINK) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = site_data(store, on, images)

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # JSON文字列中の "<" を < にしておけば </script> でHTMLが切れない
    payload_inline = payload.replace("<", "\\u003c")

    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload_inline)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return out / "index.html"
