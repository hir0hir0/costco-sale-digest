"""コストコ安売りまとめの操作口。

    python -m costco.cli discover --save     # セール系ページを探して設定に保存
    python -m costco.cli probe <URL>         # 1ページの構造を調べる（生HTMLも保存）
    python -m costco.cli collect             # 公式サイト＋メールから収集
    python -m costco.cli import-eml a.eml    # 保存済みメールから取り込み
    python -m costco.cli build               # site/ を書き出す
    python -m costco.cli update              # collect → prune → build（自動更新用）
    python -m costco.cli serve               # site/ をローカルで表示
    python -m costco.cli list                # 今のセール一覧を端末に出す
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .build import (DEFAULT_OUT, IMAGES_DOWNLOAD, IMAGES_LINK, build_site,
                    images_mode)
from .models import today_jst
from .sources import (BASE_URL, MailSkipped, backfill_mail, collect_mail,
                      collect_web, discover, fetch_images, load_dotenv,
                      load_sources, mail_probe, offers_from_eml, probe,
                      prune_images, save_sources)
from .store import DEFAULT_ROOT, Store


def _p(*args) -> None:
    print(*args, flush=True)


# ---------------------------------------------------------------- 各コマンド

def cmd_discover(args) -> int:
    conf = load_sources()
    base = args.url or conf.get("base_url") or BASE_URL
    _p(f"探索中: {base}")
    cands = discover(base)
    if not cands:
        _p("セールらしきリンクが見つかりませんでした。"
           "トップページの構造が変わった可能性があります（captures/costco/ を確認）。")
        return 1
    for c in cands:
        mark = " " if c["allowed_by_robots"] else "×"
        _p(f" {mark} [{c['kind']}] {c['name']}\n      {c['url']}")
    if args.save:
        keep = [c for c in cands if c["allowed_by_robots"]]
        existing = {e["url"] for e in conf.get("web", [])}
        added = [c for c in keep if c["url"] not in existing]
        conf["web"] = conf.get("web", []) + [
            {k: c[k] for k in ("name", "url", "kind", "enabled")} for c in added]
        conf["base_url"] = base
        save_sources(conf)
        _p(f"\ncostco_sources.json に {len(added)} 件を追加しました"
           f"（robots.txt で不可のものは除外）。")
    else:
        _p("\n--save を付けると costco_sources.json に書き込みます。")
    return 0


def _emit(info: object, out: str | None) -> None:
    """調査結果を標準出力に出し、`--out` があればファイルにも残す。

    ファイルに残せば、Actionsのログを掘らなくても `git pull` で読める。
    """
    text = json.dumps(info, ensure_ascii=False, indent=2)
    _p(text)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        _p(f"\n{path} にも書き出しました。")


def cmd_probe(args) -> int:
    _emit(probe(args.url), args.out)
    return 0


def cmd_mailprobe(args) -> int:
    load_dotenv()
    conf = load_sources()
    try:
        info = mail_probe(conf.get("mail") or {}, limit=args.limit,
                          grep=args.grep or "")
    except MailSkipped as e:
        _p(f"メールを読めません: {e}")
        return 1
    if not info:
        _p("条件に合うメールがありませんでした。"
           "costco_sources.json の mail.from_contains / since_days を確認してください。")
        return 1
    _emit(info, args.out)
    return 0


def _collect(args, store: Store) -> int:
    conf = load_sources()
    on = today_jst()
    offers, reports = [], []

    if not args.mail_only:
        if not conf.get("web"):
            _p("公式サイト: 巡回先が未設定です。"
               "`python -m costco.cli discover --save` で登録してください。")
        else:
            got, rep = collect_web(conf, base=on)
            offers += got
            reports += rep

    if not args.web_only:
        got, rep = collect_mail(conf.get("mail") or {}, base=on)
        offers += got
        reports += rep

    _p("収集結果:")
    for r in reports:
        _p(r.line())

    res = store.merge(offers, on)
    _p(f"\n{res.summary()}")
    for o, d in res.price_drops[:10]:
        _p(f"  ↓ {o.name}: ¥{d:,} 値下がり → ¥{o.price:,}")
    for o in res.lowest_ever[:10]:
        _p(f"  ★ {o.name}: 過去最安 ¥{o.price:,}")

    removed = store.prune()
    if removed:
        _p(f"終了済み {removed} 件を一覧から外しました（価格履歴は残しています）。")

    mode = images_mode(conf.get("images"))
    if mode == IMAGES_DOWNLOAD:
        got, failed = fetch_images(store.offers)
        gone = prune_images(store.offers)
        have = sum(1 for o in store.offers if o.image)
        _p(f"画像: {have} 件（新規 {got} / 失敗 {failed} / 不要削除 {gone}）")
    else:
        # リンク参照（または写真なし）ならダウンロード済みの実体は要らない
        for o in store.offers:
            o.image = ""
        gone = prune_images(store.offers)
        if mode == IMAGES_LINK:
            have = sum(1 for o in store.offers if o.image_url)
            _p(f"画像: リンク参照 {have} 件（ダウンロードなし"
               + (f" / 実体 {gone} 枚を削除" if gone else "") + "）")
    return 0


def cmd_collect(args) -> int:
    load_dotenv()
    store = Store.load(args.data)
    rc = _collect(args, store)
    if args.dry_run:
        _p("\n--dry-run のため保存しませんでした。")
    else:
        store.save()
        _p(f"\n保存しました: {store.root}")
    return rc


def cmd_import_eml(args) -> int:
    store = Store.load(args.data)
    offers = []
    for pattern in args.paths:
        direct = Path(pattern)
        found = [direct] if direct.exists() else sorted(Path().glob(pattern))
        if not found:
            _p(f"見つかりません: {pattern}")
        for path in found:
            got = offers_from_eml(path)
            _p(f"{path}: {len(got)}件")
            offers += got
    if not offers:
        _p("取り込めるものがありませんでした。")
        return 1
    res = store.merge(offers)
    _p(res.summary())
    if not args.dry_run:
        store.save()
        _p(f"保存しました: {store.root}")
    return 0


def cmd_backfill(args) -> int:
    """過去メールから蓄積と価格履歴を作り直す（履歴は当時の日付が付く）。"""
    load_dotenv()
    conf = load_sources()
    by_day, reports = backfill_mail(conf.get("mail") or {}, since_days=args.since_days)
    _p("読み込み:")
    for r in reports:
        _p(r.line())
    if not by_day:
        _p("メールから何も取れませんでした。中断します（既存データは触っていません）。")
        return 1

    store = Store(args.data)          # 空の状態から時系列で作り直す
    for d in sorted(by_day):
        res = store.merge(by_day[d], d)
        _p(f"  {d}: {len(by_day[d])}件観測 → {res.summary()}")
    removed = store.prune()
    if removed:
        _p(f"終了済み {removed} 件を一覧から外しました。")
    if args.dry_run:
        _p("\n--dry-run のため保存しませんでした。")
        return 0
    store.save()
    _p(f"\n保存しました: {store.root}")
    return cmd_build(args)


def cmd_build(args) -> int:
    store = Store.load(args.data)
    path = build_site(store, args.out,
                      images=images_mode(load_sources().get("images")))
    data = json.loads((Path(args.out) / "data.json").read_text(encoding="utf-8"))
    hidden = data.get("hidden_no_price") or 0
    _p(f"{path} を書き出しました（掲載 {data['stats']['total']} 件"
       + (f" / 価格を読めず非掲載 {hidden} 件）" if hidden else "）"))
    if data["stats"]["total"] == 0:
        _p("※ 掲載0件です。まだ収集できていないか、期間が終わったものだけの可能性があります。")
    return 0


def cmd_update(args) -> int:
    load_dotenv()
    store = Store.load(args.data)
    _collect(args, store)
    store.save()
    return cmd_build(args)


def cmd_list(args) -> int:
    store = Store.load(args.data)
    items = sorted(store.active(), key=lambda o: (o.ends_on or "9999-12-31", o.name))
    if not items:
        _p("掲載中のセールはありません。")
        return 0
    for o in items:
        price = f"¥{o.price:,}" if o.price is not None else "価格不明"
        was = f" (通常 ¥{o.regular_price:,})" if o.regular_price else ""
        left = o.days_left()
        end = f" あと{left}日" if left is not None and left >= 0 else ""
        _p(f"{price:>10}{was:<18} {o.name[:44]:<44} {o.category:<10}{end}")
    _p(f"\n計 {len(items)} 件")
    return 0


def cmd_serve(args) -> int:
    import functools
    import http.server
    import socketserver

    out = Path(args.out)
    if not (out / "index.html").exists():
        _p("site/ がまだありません。先に `build` を実行してください。")
        return 1
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        _p(f"http://127.0.0.1:{args.port}/ で表示中（Ctrl+C で終了）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            _p("\n終了しました。")
    return 0


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m costco.cli",
        description="コストコの安売り情報を集めて静的サイトにする",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--data", default=str(DEFAULT_ROOT), help="データ置き場（既定: costco_data/）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="セール系ページを探す")
    p.add_argument("--url", help=f"起点URL（既定: {BASE_URL}）")
    p.add_argument("--save", action="store_true", help="costco_sources.json に書き込む")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("probe", help="1ページの構造を調べる")
    p.add_argument("url")
    p.add_argument("--out", help="結果をこのファイルにも書き出す")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("mailprobe", help="メルマガの中身を調べる")
    p.add_argument("--limit", type=int, default=6, help="新しい方から何通見るか")
    p.add_argument("--grep", help="この語（カンマ区切り）を含む行の前後だけを抜く")
    p.add_argument("--out", help="結果をこのファイルにも書き出す")
    p.set_defaults(func=cmd_mailprobe)

    p = sub.add_parser("collect", help="公式サイト・メールから収集する")
    p.add_argument("--web-only", action="store_true")
    p.add_argument("--mail-only", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="保存しない")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("import-eml", help="保存済みメール(.eml)から取り込む")
    p.add_argument("paths", nargs="+")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_import_eml)

    p = sub.add_parser("backfill", help="過去メールから蓄積と履歴を作り直す")
    p.add_argument("--since-days", type=int, default=60, help="何日前まで遡るか")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("build", help="site/ を書き出す")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("update", help="収集してサイトまで作る（自動更新用）")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--web-only", action="store_true")
    p.add_argument("--mail-only", action="store_true")
    p.set_defaults(func=cmd_update, dry_run=False)

    p = sub.add_parser("list", help="掲載中のセールを端末に出す")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("serve", help="site/ をローカルで表示する")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
