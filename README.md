# コストコ 安売りまとめ

コストコの**セール・クーポン・値下がり情報**をメルマガから自動で集めて、
1ページの静的サイトにまとめる個人プロジェクト。

**サイト: https://hir0hir0.github.io/costco-sale-digest/**

- セール品の一覧・検索（商品名/商品番号）・カテゴリ絞り込み
- 価格履歴と値下げ検知（「過去最安」「前回より◯円安い」）
- クーポンの有効期間管理（期限切れは自動で落ちる）
- 買い物リスト（チェックした品だけ表示・合計金額・印刷）

毎朝 07:00 JST に GitHub Actions が収集してサイトを作り直す。

## 仕組み

```
コストコからのメルマガ (IMAP)
        │  商品ごとの ITEM # を軸に商品名・価格・値引き・期間を抽出
        ▼
costco_data/*.json （価格履歴は1日1点）
        ▼
site/index.html （単一ファイル・外部CSS/JSなし） ──→ GitHub Pages
```

- Python 3.11+ 標準ライブラリのみ（Pillow は任意）
- 公式サイトの巡回機構もあるが、セール系ページがJS描画のため現在は未使用
  （`python -m costco.cli discover` で拾い直せる）

## 自分で動かす

```bash
python -m costco.cli update      # 収集 → site/ 生成
python -m costco.cli serve       # http://127.0.0.1:8765/ で確認
python -m costco.cli list        # 端末に一覧表示
python -m unittest discover -s tests
```

メルマガの取り込みには、コストコのメルマガが届くメールボックスの IMAP 情報が要る。
リポジトリの Secrets（ローカルなら `.env`）に:

```
COSTCO_IMAP_HOST=imap.gmail.com
COSTCO_IMAP_USER=you@gmail.com
COSTCO_IMAP_PASSWORD=（Gmailならアプリパスワード）
```

未設定ならメール収集は静かにスキップされる。メールは読み取り専用で、既読にもしない。

## 設定（costco_sources.json）

| キー | 意味 |
|---|---|
| `images` | 商品写真。`"link"`=コストコCDNを直接参照（既定・複製しない）/ `true`=ダウンロード / `false`=なし |
| `mail` | 取り込むメールの条件（差出人・日数など） |
| `web` | 公式サイトの巡回先。`discover --save` が登録する |

## 注意事項

- 価格・期間は自動抽出のため**誤りが含まれることがある**。購入前に必ず店頭・
  公式サイトで確認してください。価格が読み取れなかった商品は掲載していません
- 商品名・価格などの事実情報と、コストコCDNへの画像リンクのみを扱い、
  メルマガ本文や画像の複製は配布していません
- 当プロジェクトはコストコホールセールジャパン株式会社とは無関係の個人サイトです
- 収集は1日1回・取得間隔を空けて行い、robots.txt に従います

詳しい設計と実装のノウハウ（抽出の落とし穴など）は [`docs/costco.md`](docs/costco.md)。
