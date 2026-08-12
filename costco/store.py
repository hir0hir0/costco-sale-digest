"""セール情報の保存と価格履歴。

データはリポジトリ内のJSONに置く（GitHub Actions がコミットして更新していく）。
差分が読める形にしたいので、書き出しは必ずソート済み・`ensure_ascii=False`・末尾改行。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .models import Offer, merge_offer, same_run, today_jst

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "costco_data"

OFFERS_FILE = "offers.json"
HISTORY_FILE = "history.json"
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
        """価格履歴に追記し、(前回からの下げ幅, 過去最安か) を返す。

        履歴は**1日1点まで**。同じ日に何度収集しても点は増えず、その日の点を
        上書きする。値下がり・過去最安の判定は前日以前の点とだけ比較する
        （同じ日の観測どうしを比べても意味がない）。
        """
        if o.price is None:
            return None, False
        entry = self.history.setdefault(o.key, {"name": o.name, "points": []})
        entry["name"] = o.name or entry.get("name", "")
        points = entry["points"]
        today = on.isoformat()

        if points and points[-1].get("on") == today:
            prior = points[:-1]
            if points[-1].get("price") != o.price:
                points[-1].update({"price": o.price,
                                   "regular_price": o.regular_price,
                                   "source": o.source})
                points[-1].pop("until", None)
        else:
            prior = list(points)
            last_price = next((p["price"] for p in reversed(prior)
                               if isinstance(p.get("price"), int)), None)
            if last_price != o.price:
                points.append({
                    "on": today,
                    "price": o.price,
                    "regular_price": o.regular_price,
                    "source": o.source,
                })
            elif points:
                # 同じ価格が続いている間は点を増やさず、最後に見た日だけ延ばす
                points[-1]["until"] = today

        prev_prices = [p["price"] for p in prior if isinstance(p.get("price"), int)]
        last = prev_prices[-1] if prev_prices else None
        drop = (last - o.price) if (last is not None and o.price < last) else None
        lowest = bool(prev_prices) and o.price < min(prev_prices)
        return drop, lowest

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
