"""セール情報の保存と価格履歴。

データはリポジトリ内のJSONに置く（GitHub Actions がコミットして更新していく）。
差分が読める形にしたいので、書き出しは必ずソート済み・`ensure_ascii=False`・末尾改行。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .models import Offer, merge_offer, same_run, today_jst

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "costco_data"

OFFERS_FILE = "offers.json"
HISTORY_FILE = "history.json"
PURCHASES_FILE = "purchases.json"
META_FILE = "meta.json"


def _write_json(path: Path, data: object) -> None:
    """同一ディレクトリの一時ファイル経由で置き換える（途中で落ちても壊れない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)  # mkstemp は 0600 で作る。コミットして配る物なので通常の権限に戻す
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


@dataclass
class MergeResult:
    added: list[Offer] = field(default_factory=list)
    updated: list[Offer] = field(default_factory=list)
    price_drops: list[tuple[Offer, int]] = field(default_factory=list)  # (offer, 下がった額)
    lowest_ever: list[Offer] = field(default_factory=list)

    def summary(self) -> str:
        return (f"新規{len(self.added)}件 / 更新{len(self.updated)}件 / "
                f"値下がり{len(self.price_drops)}件 / 過去最安{len(self.lowest_ever)}件")


class Store:
    """`costco_data/` 一式の読み書き。"""

    def __init__(self, root: Path | str = DEFAULT_ROOT):
        self.root = Path(root)
        self.offers: list[Offer] = []
        self.history: dict[str, dict] = {}
        self.meta: dict = {}

    # ------------------------------------------------------------ 入出力
    @classmethod
    def load(cls, root: Path | str = DEFAULT_ROOT) -> "Store":
        st = cls(root)
        st.offers = [Offer.from_dict(d) for d in _read_json(st.root / OFFERS_FILE, [])]
        st.history = _read_json(st.root / HISTORY_FILE, {})
        st.meta = _read_json(st.root / META_FILE, {})
        return st

    def save(self) -> None:
        self.offers.sort(key=lambda o: (o.ends_on or "9999-12-31", o.category, o.key))
        _write_json(self.root / OFFERS_FILE, [o.to_dict() for o in self.offers])
        _write_json(self.root / HISTORY_FILE, self.history)
        _write_json(self.root / META_FILE, self.meta)

    # ------------------------------------------------------------ マージ
    def merge(self, incoming: list[Offer], seen_on: date | None = None) -> MergeResult:
        """収集した結果を取り込む。価格が変わっていれば履歴に1点足す。"""
        seen_on = seen_on or today_jst()
        res = MergeResult()

        # 同じ商品が1回の収集に複数回来たら（複数のメルマガ号に載る等）、
        # 先に1件へ潰す。そのまま流すと収集のたびに価格がA→B→Aと行き来して
        # 履歴がギザギザになり、偽の「過去最安」まで生む（実際に起きた）。
        # 入力はメールの古い順なので、merge_offer が後ろ＝新しい号を正とする。
        collapsed: list[Offer] = []
        for raw in incoming:
            new = raw.normalize(seen_on)
            hit = next((c for c in collapsed if same_run(c, new)), None)
            if hit is None:
                collapsed.append(new)
            else:
                collapsed[collapsed.index(hit)] = merge_offer(hit, new)

        for new in collapsed:
            new.last_seen = seen_on.isoformat()
            hit = next((o for o in self.offers if same_run(o, new)), None)
            if hit is None:
                self.offers.append(new)
                res.added.append(new)
                target = new
            else:
                before = hit.price
                merged = merge_offer(hit, new)
                self.offers[self.offers.index(hit)] = merged
                target = merged
                if merged.price != before or merged.last_seen != hit.last_seen:
                    res.updated.append(merged)

            drop, lowest = self._record_price(target, seen_on)
            if drop:
                res.price_drops.append((target, drop))
            if lowest:
                res.lowest_ever.append(target)

        self.meta["last_collected_at"] = seen_on.isoformat()
        return res

    def _record_price(self, o: Offer, on: date) -> tuple[int | None, bool]:
        """価格履歴に観測を1つ入れ、(前回からの下げ幅, 過去最安か) を返す。

        履歴は日付順・**1日1点**。同じ日の再観測は点を上書きする。
        レシート取り込みで過去の日付が後から入ることがあるので、末尾追記では
        なく日付順の位置へ挿入する。値下がり・過去最安の判定は「これが最新の
        観測」のときだけ、前日以前の点と比較して返す（過去への挿入で今さら
        通知を出しても意味がないため）。
        """
        if o.price is None:
            return None, False
        entry = self.history.setdefault(o.key, {"name": o.name, "points": []})
        entry["name"] = o.name or entry.get("name", "")
        points = entry["points"]
        today = on.isoformat()
        is_latest = not points or today >= points[-1].get("on", "")

        idx = next((j for j, pt in enumerate(points)
                    if pt.get("on", "") >= today), len(points))
        prior = points[:idx]
        prev_prices = [pt["price"] for pt in prior if isinstance(pt.get("price"), int)]
        last = prev_prices[-1] if prev_prices else None

        if idx < len(points) and points[idx].get("on") == today:
            if points[idx].get("price") != o.price:
                points[idx].update({"price": o.price,
                                    "regular_price": o.regular_price,
                                    "source": o.source})
                points[idx].pop("until", None)
        elif idx == len(points) and last == o.price:
            # 末尾に同じ価格が続くだけなら点を増やさず、最後に見た日を延ばす
            if points:
                points[-1]["until"] = today
        else:
            points.insert(idx, {
                "on": today,
                "price": o.price,
                "regular_price": o.regular_price,
                "source": o.source,
            })

        if not is_latest:
            return None, False
        drop = (last - o.price) if (last is not None and o.price < last) else None
        lowest = bool(prev_prices) and o.price < min(prev_prices)
        return drop, lowest

    # ------------------------------------------------------------ レシート
    def load_purchases(self) -> list[dict]:
        return _read_json(self.root / PURCHASES_FILE, [])

    def merge_receipts(self, purchases: list[dict] | None = None) -> int:
        """レシートの明細を「店頭で実際に付いていた価格」として履歴に入れる。

        一覧(offers)には足さない — レシートはセール情報ではない。
        同じレシートを何度取り込んでも結果は変わらない（同日上書き）。
        戻り値は履歴に入れた明細の数。
        """
        if purchases is None:
            purchases = self.load_purchases()
        n = 0
        for r in purchases:
            try:
                d = date.fromisoformat(str(r.get("date", "")))
            except ValueError:
                continue
            for it in r.get("items", []):
                no = re.sub(r"\D", "", str(it.get("item_no", "")))
                price = it.get("price")
                if not no or not isinstance(price, int):
                    continue
                o = Offer(name=str(it.get("name", "")), item_no=no,
                          price=price, source="receipt").normalize(d)
                self._record_price(o, d)
                n += 1
        return n

    def last_purchases(self) -> dict:
        """商品キー → 最後に買った記録 {on, price, coupon, effective}。"""
        out: dict[str, dict] = {}
        for r in self.load_purchases():
            day = str(r.get("date", ""))
            for it in r.get("items", []):
                no = re.sub(r"\D", "", str(it.get("item_no", "")))
                price = it.get("price")
                if not no or not isinstance(price, int):
                    continue
                coupon = it.get("coupon") if isinstance(it.get("coupon"), int) else 0
                cur = out.get("no:" + no)
                if cur is None or day >= cur["on"]:
                    out["no:" + no] = {"on": day, "price": price, "coupon": coupon,
                                       "effective": price - coupon}
        return out

    # ------------------------------------------------------------ 参照
    def price_stats(self, key: str) -> dict:
        """サイト表示用の履歴サマリ。履歴が無ければ空の形を返す。"""
        entry = self.history.get(key) or {}
        points = [p for p in entry.get("points", []) if isinstance(p.get("price"), int)]
        if not points:
            return {"points": [], "count": 0, "lowest": None, "highest": None, "prev": None}
        prices = [p["price"] for p in points]
        return {
            "points": [{"on": p["on"], "price": p["price"]} for p in points],
            "count": len(points),
            "lowest": min(prices),
            "highest": max(prices),
            "prev": prices[-2] if len(prices) >= 2 else None,
        }

    def active(self, on: date | None = None) -> list[Offer]:
        on = on or today_jst()
        return [o for o in self.offers if o.is_active(on)]

    def prune(self, keep_days: int = 120, on: date | None = None) -> int:
        """終了から `keep_days` 過ぎたセールを一覧から落とす（価格履歴は残す）。"""
        on = on or today_jst()
        cutoff = on - timedelta(days=keep_days)
        before = len(self.offers)
        kept = []
        for o in self.offers:
            end = o.end_date()
            last = o.last_seen
            if end and end < cutoff:
                continue
            if not end and last and last < cutoff.isoformat():
                continue
            kept.append(o)
        self.offers = kept
        return before - len(self.offers)
