# コストコ安売りまとめ（`costco/`）

コストコの**セール・クーポン情報を集めて1ページの静的サイトにする**仕組み。
BSF/JEXER の予約自動化とは独立していて、依存も共有していない（後で別リポジトリに
切り出せるように、`costco/` だけで完結させてある）。

```
公式サイト（costco.co.jp）  ─┐
                            ├─→ 収集・正規化 ─→ costco_data/*.json ─→ site/index.html
コストコからのメール(IMAP)  ─┘        ↑                  │
                                  価格履歴の追記 ────────┘
```

- 外部依存ゼロ（Python 3.11+ 標準ライブラリのみ）。`pip install` は要らない
- 生成物 `site/index.html` は**単一ファイル完結**。外部のCSS/JS/フォントを読まないので
  そのままブラウザで開けるし、GitHub Pages にも置ける

## できること

| 機能 | どこで |
|---|---|
| セール品の一覧・検索（商品名／商品番号）・カテゴリ絞り込み | サイト |
| 価格履歴と値下げ検知（「過去最安」「前回より◯円安い」） | `store.py` → サイトのバッジとスパークライン |
| クーポンと有効期間の管理（期限切れは自動で落ちる／期限間近を強調） | `models.py` の期間判定 |
| 買い物リスト（チェックした品だけ表示・合計金額・印刷） | サイト（localStorage） |
| 商品写真 | メルマガの画像を `site/img/` に取り込み |

## 使い方

```bash
# 1) 巡回先を探して登録する（トップページからセール系リンクを拾う）
python -m costco.cli discover --save

# 2) 収集してサイトまで作る
python -m costco.cli update

# 3) ローカルで見る
python -m costco.cli serve      # http://127.0.0.1:8765/
```

その他:

```bash
python -m costco.cli probe <URL>          # 1ページの構造を調べる（JSON-LDの有無・価格行の数など）
python -m costco.cli collect --dry-run    # 収集だけして保存しない
python -m costco.cli import-eml mail.eml  # 保存したメールから取り込む
python -m costco.cli list                 # 掲載中のセールを端末に出す
python -m costco.cli build                # site/ だけ作り直す
```

### メール（メルマガ）の取り込み

`.env` に IMAP の情報を書くと、コストコからのメールを読んで取り込む。
**未設定なら静かにスキップ**して公式サイトだけ巡回する（JEXER と同じ扱い）。

```
COSTCO_IMAP_HOST=imap.gmail.com
COSTCO_IMAP_USER=you@gmail.com
COSTCO_IMAP_PASSWORD=（Gmailなら「アプリ パスワード」）
```

読む範囲は `costco_sources.json` の `mail` で決める（既定は直近21日・差出人に `costco`
を含むもの）。メールは**読むだけ**（`select(readonly=True)`）で、既読にも変更しない。

## 自動更新と公開

`.github/workflows/costco.yml` が毎日 07:00 JST に collect → build し、変更があれば
`costco_data/` と `site/` をコミットする。IMAP の情報はリポジトリの Secrets に
`COSTCO_IMAP_HOST` / `COSTCO_IMAP_USER` / `COSTCO_IMAP_PASSWORD` として入れる。

**GitHub Pages で一般公開する場合**:

1. リポジトリを public にする（無料プランでは private だと Pages が使えない）。
   このリポジトリには予約設定など私的な情報が入っているので、公開するなら
   `costco/` `costco_data/` `site/` `tests/` だけを**別の公開リポジトリに切り出す**方が安全
2. Settings → Pages → Source を「GitHub Actions」にする
3. リポジトリ変数 `COSTCO_PAGES` を `true` にする（Settings → Variables）

変数が `true` でない間は収集とコミットだけ動き、Pages への配信はスキップされる。

## 商品写真

メルマガのHTMLに入っている画像をカードに出す。扱いは `costco_sources.json` の
`images` で選ぶ:

| 値 | 動き |
|---|---|
| `"link"`（既定） | コストコのCDNのURLを `<img src>` に直接入れる。**ダウンロード・複製をしない**ので権利面の筋が良く、リポジトリも太らない。先方が画像を消すとその時点で壊れる（`onerror` で枠ごと消える）。閲覧のたびにコストコのサーバへ画像リクエストが飛ぶ |
| `true` | `site/img/` にダウンロードして相対参照。オフラインでも出るが、複製を配布する形になる。Pillow があれば長辺480pxに縮小（1枚 90KB → 15KB 程度） |
| `false` | 写真なし |

共通の挙動:

- **商品との対応は `alt` の商品名で取る**。位置だけで結びつけると隣の商品の写真を
  掴む（真珠のネックレスにソファの写真が付いた）。alt で当たらなければ位置で選ぶが、
  それでも決まらなければ**写真なし**にする。別商品の写真を出すよりましなため
- 開封計測用の透明画像を掴まないよう、1.5KB未満とURLに `pixel` / `tracking` 等を
  含むものは弾く（ダウンロード時）。リンク参照でも `referrerpolicy="no-referrer"` を付ける

## データの形

| ファイル | 中身 |
|---|---|
| `costco_sources.json` | 巡回先とメールの条件。`discover --save` が書き足す |
| `costco_data/offers.json` | 掲載中のセール（1件 = ある商品がある期間安い、という単位） |
| `costco_data/history.json` | 商品ごとの価格履歴。**価格が変わったときだけ**1点増える |
| `costco_data/meta.json` | 最終収集日 |
| `site/` | 生成物。`index.html` / `data.json` / `.nojekyll` |

商品のキーは**商品番号があればそれ**（`no:1234567`）、無ければ正規化した商品名の
ハッシュ（`nm:...`）。商品番号が取れていれば、チラシとメールをまたいでも同じ商品として
履歴が繋がる。

## 抽出がずれたときの直し方

サイトの掲載が0件になったり、価格が明らかに変な値になったら:

1. **`captures/costco/` に生HTMLが残っている**（収集のたびに保存している。gitignore済）。
   まずこれを開いて、ページ構造が変わっていないか見る
2. `python -m costco.cli probe <URL>` で「JSON-LDがあるか」「価格らしき行が何行あるか」
   「抽出できた件数」を確認する
3. 直したら `tests/test_costco.py` に**その誤読を回帰テストとして足す**

抽出は次の順で確度の高いものから拾っている:

1. JSON-LD の `Product`（あれば一番正確・`confidence` 0.95）
2. テキスト行の総当たり（`confidence` 0.4〜0.55）

確度が低いものはサイト上で薄く表示され、「⚠ 自動抽出の確度が低い項目です」が付く。

過去に踏んだ誤読（いずれもテスト済み）:

- 「ミックスナッツ **1.13kg**」を日付 1月13日と読む → 日付の区切りは `月` と `/` のみに限定
- 「¥69,800（**¥15,000引き**）」の値引き額を売価と読む → 値引き額を価格候補から除外
- 隣の商品の**商品番号を掴む** → 商品名の行から価格行の次までを1商品の塊とする
- 同じ品が「セール」と「クーポン」で二重に並ぶ → 同一商品・同一期間なら1件にまとめる

## 守っていること

- **robots.txt を読んで従う**。不許可のURLは `discover --save` の時点で登録しない
- 取得間隔は既定3秒（`Crawl-delay` があればそちら）。巡回は1日1回
- メールは読み取り専用
- サイトのフッターに「自動抽出なので誤りがありうる／購入前に店頭・公式で確認」
  「コストコホールセールジャパン株式会社とは無関係の個人サイト」と明記している

## テスト

```bash
python -m unittest discover -s tests
```
