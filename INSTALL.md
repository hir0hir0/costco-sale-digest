# 導入手順（costco-sale-digest レビュー体制キット）

## 入っているもの

```
.claude/agents/      … 監査9体 + 反証役1体 / 提案2体 + 選別役1体
.claude/commands/    … /review-site（監査）, /propose（提案）
docs/review/         … ペルソナ・2種類の出力様式・チェックリスト・運用手順
blackbox/            … 初見ユーザ評価セット（リポジトリの外で使う）
```

**2つのレーンがある。**
- `/review-site` … 壊れているものを見つける（監査）
- `/propose` … あると便利な機能・より良い見せ方を構想する（提案）

## 導入（3通り。上から順に楽）

### A. オールインワン版を添付する（推奨）
1. `costco-sale-digest` を選んでセッションを開く
2. `REVIEW-KIT-ALL-IN-ONE.md` を添付して、こう言う:
   > 添付ファイルの「=== FILE: パス ===」区切りに従って、リポジトリに全ファイルを作成してコミットして。
   > blackbox/ 配下も同じ場所に作ってよい（使うときに外へ出す）。

### B. ローカルクローンがある場合
zip を展開し、`.claude/` と `docs/` をリポジトリ直下に配置して commit / push。
（すでに `.claude/` がある場合は `agents/` `commands/` の中身だけを足す）

### C. ファイルを個別に添付
zip を展開して、必要なファイルをセッションに添付する。

## 前提は確定済み

`docs/review/personas.md` の「確定した前提」に反映済み（2026-08-31）。
レシートは自分の分のみ / 第一の利用者は自分 / メルマガ・レシートとも正規 /
メルマガは全国共通 / サイトは公開するが購入日は月単位・店舗と数量は出さない。

**前提が変わったら、まず personas.md を直してからレビューを回すこと。**

## 動かす

`costco-sale-digest` のセッションで：

```
/review-site
```
→ 現フェーズの標準5体（receipt-pipeline / data-integrity / docs-drift / ops-resilience / mobile-a11y）+ 反証役

```
/review-site all          # 9体すべて（公開前・大規模変更後）
/review-site legal-risk   # サイト公開の直前に必須
```

`.claude/agents/` のファイルは Claude Code が自動認識します（`/agents` で一覧・編集可）。

## 最初の1回におすすめの順番

1. `/review-site docs-drift` — CLAUDE.md が実装とズレていないか先に確認する。
   ここがズレていると、以降のレビュー全部が誤った前提で動く
2. `/review-site receipt-pipeline data-integrity` — 一番深刻な問題が出るのはここ
3. 出た指摘を潰してから `/review-site`（標準5体）
4. **サイトを公開する前に `/review-site legal-risk`**（Git履歴の個人情報まで見る）
5. 公開に踏み切るときに `blackbox/` を別セッションで

提案レーン（`/propose`）は、データがある程度溜まってから。
薄いうちに回すと「溜まったらできること」しか出ない。

