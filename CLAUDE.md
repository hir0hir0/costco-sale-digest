# costco-sale-digest — コストコ安売りまとめ

コストコのセール・クーポン情報をメルマガから自動集計し、GitHub Pages で配信する。
サイト: https://hir0hir0.github.io/costco-sale-digest/ （毎朝07:00 JSTに自動更新）

元は私有リポジトリ hir0hir0/bsf-reserver（ジム予約自動化）の一部だったが、
2026-08-12 に公開用として切り出した。**ジム側とは無関係。ここはコストコ専用**。

## 構成

```
costco/            コード（Python 3.11+ 標準ライブラリのみ・Pillowは任意）
costco_data/       offers.json（セール蓄積）/ history.json（価格履歴）/
                   purchases.json（レシート）/ _probe.json（調査結果）
site/              生成物。GitHub Pages がここを配信
tests/             python -m unittest discover -s tests（必ず全部通してからpush）
.github/workflows/costco.yml   毎朝の自動実行＋workflow_dispatch の各モード
```

- 正本はこのGitHubリポジトリ。デーモンや常駐プロセスは無く、**全部 Actions 上で動く**
- IMAP認証はリポジトリの Secrets（COSTCO_IMAP_HOST/USER/PASSWORD）。コードに秘密は無い
- Actions は workflow_dispatch で手動実行できる（mode: update / discover / probe /
  mailprobe / backfill）。調査結果は costco_data/_probe.json にコミットされるので
  `git pull` で読める（ログをAPIで掘るより安い）

## レシート取り込み（ユーザーが写真を貼って「レシート取り込んで」と言ってくる）

1. レシート画像から明細を読み取り、`costco_data/purchases.json` の配列に追記する:
   ```json
   {"date": "YYYY-MM-DD", "store": "◯◯倉庫店", "total": *****,
    "items": [{"item_no": "588141", "name": "…", "price": 1998, "coupon": 420}]}
   ```
   - `item_no` はレシート左の数字列。`coupon` は CPN/IRC 行の値引き額を**直前の商品**に付ける
   - 名前が読めない明細は `（判読不明）…` として商品番号だけでも入れる
2. **必ず検算する**: Σ(price − coupon) がレシートの合計と一致すること。
   一致しない場合は読み取りミスがあるので、ユーザーに不明行を確認する
3. コミットして push。次の自動更新でサイトに反映される（急ぐなら mode:update を実行）
4. 反映先: 価格履歴（source=receipt）／「前回購入より安い」バッジ／購入履歴タブ

⚠️ purchases.json は公開される（買った物と日付が見える）。ユーザーが嫌がったら
空配列にして backfill で履歴を作り直す。

## 落とし穴（全部実際に踏んだ・tests/ に回帰テストあり）

- **メルマガは2列レイアウトの号がある**。商品番号が金額を挟まず隣接していたら、
  価格ブロックはまとめて後ろに左から順。直後の価格を掴むと隣の商品の値になる
  （TEMPURの枕が隣のシーツの1,748円になった）
- 価格は「¥」と「7,358」に行が分断されて届く。結合してから読む
- `Price`/`Off` ラベルは金額の**前の行**。ラベル付きは引き算と矛盾する金額を採らない。
  半額超えの値引きは名乗らない（読み範囲のはみ出し対策）
- `</head>` 省略で本文全損、「1.13kg」を日付と誤読、等 → docs/costco.md 参照
- 履歴は日付順・1日1点。レシートで過去日付が後から入るので末尾追記ではなく挿入
- 抽出を直したら**実物の並びを回帰テストに足す**こと

## 方針

- 価格が読めなかった商品は載せない（嘘の行を並べない）。捨てた件数は必ず表示する
- robots.txt に従う・収集は1日1回・メールは読み取り専用
- 写真は既定でコストコCDNへのリンク参照（複製しない）。costco_sources.json の `images`

## レビュー体制

- `/review-site` … 監査（壊れているものを見つける）
- `/propose` … 提案（あると便利な機能・より良い見せ方を構想する）
- 物差しは `docs/review/personas.md`
