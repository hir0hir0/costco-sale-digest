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
   {"month": "YYYY-MM",
    "items": [{"item_no": "588141", "name": "…", "price": 1998, "coupon": 420,
               "weight_g": 512, "unit_price": 390}]}
   ```
   - **店舗名・購入日・レシート合計は保存しない**。personas.md の公開ポリシーで
     「出さない」「粒度を落とす」区分。`costco_data/` は丸ごと公開されるので、
     ここに書いた時点で公開される。日付は `month`（YYYY-MM）まで
   - **同月・同一商品は1行に潰す**（数量を公開しない）。`store.scrub_purchases` が
     自動でやるので、読み取り時は明細をそのまま並べてよい
   - `weight_g` / `unit_price`（¥/100g）は**量り売り商品では必ず入れる**。総額は重さで
     変わるので、これが無いと価格の比較が嘘になる。レシートに単価表示がある行が該当。
     入れれば単価で比較され、入れないまま同月に金額違いの同一商品が並ぶと
     `weighed` が立ち、底値・前回比較から外れる（嘘の比較を出さないため）
   - `item_no` はレシート左の数字列。`coupon` は CPN/IRC 行の値引き額を**直前の商品**に付ける
   - 名前が読めない明細は `（判読不明）…` として商品番号だけでも入れる
2. **必ず検算する**: Σ(price − coupon) がレシートの合計と一致すること。
   一致しない場合は読み取りミスがあるので、ユーザーに不明行を確認する。
   **合計はこの検算だけに使い、ファイルには残さない**
3. コミットして push。次の自動更新でサイトに反映される（急ぐなら mode:update を実行）
4. 反映先: 価格履歴（source=receipt）／「前回購入より安い」バッジ／購入履歴タブ

⚠️ `costco_data/` は workflow が `git add costco_data site costco_sources.json` で
**丸ごとコミットする**。このディレクトリに置いたファイルは全部 public になる
（調査用の `_probe.json` も例外ではない）。公開してよい粒度は personas.md の
「公開ポリシー」表が正典で、CLAUDE.md ではなくそちらを見ること。
ユーザーが嫌がったら purchases.json を空配列にして backfill で履歴を作り直す。

## 落とし穴（全部実際に踏んだ・tests/ に回帰テストあり）

- **公開粒度は build.py だけでは守れない**。`history.json` と `purchases.json` は
  それ自体が public リポジトリの正本なので、build 時に丸めても元データに日付が残る。
  粒度を落とすなら `store.merge_receipts` の入口（取り込み時）で落とすこと
- **mailprobe / probe / discover を Actions で回すと、`captures/` が
  アーティファクトとして公開される**（`.gitignore` してあるのはリポジトリ側だけ）。
  メルマガ生HTMLを出したくないなら workflow の upload path を絞る
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
