---
description: costco-sale-digest を9観点のレビュワーで並列監査し、反証まで通して最終レポートを出す
---

costco-sale-digest の定期レビューを実行します。

## 前提確認

まず `CLAUDE.md`・`docs/review/personas.md`・`docs/review/review-format.md`・`docs/review/checklist.md` を読む。

`personas.md` の「確定した前提」と、実装が明らかに矛盾している場合（例: 店舗別価格比較を作り込んでいる、
公開ポリシーで出さないはずの項目が公開データに含まれている）は、**その矛盾自体を最初に私に報告すること。**

## 実行

引数がない場合は、**現時点（自分用フェーズ）の標準セット5体**を並列で起動する:
   - receipt-pipeline-reviewer
   - data-integrity-reviewer
   - docs-drift-reviewer
   - ops-resilience-reviewer
   - mobile-a11y-reviewer

`/review-site all` と指定された場合のみ、以下の9体すべてを**並列で**起動する:
   - ux-task-reviewer
   - ia-reviewer
   - data-integrity-reviewer
   - receipt-pipeline-reviewer
   - ops-resilience-reviewer
   - docs-drift-reviewer
   - mobile-a11y-reviewer
   - legal-risk-reviewer
   - competitive-reviewer

   （ux-task / ia は画面の作り込みを見るため、legal-risk は公開直前に、competitive は方向性を見直すときに使う）

   各エージェントには「担当セクションを checklist.md から確認し、review-format.md の様式で出力せよ」とだけ伝える。
   実装の意図や設計の背景を**説明してはいけない**。先入観を与えるとレビューが甘くなる。

2. 全結果を `docs/review/reports/<今日の日付>-raw.md` に、エージェントごとの見出しを付けてそのまま集約する。
   この時点では要約・取捨選択をしない。

3. 続けて review-critic エージェントを**単独で**起動し、raw レポートを検証させて
   `docs/review/reports/<今日の日付>-final.md` を作らせる。

4. final.md に「CLAUDE.md への追記提案」があれば、その内容を私に提示する。
   **私の承認なく CLAUDE.md を書き換えないこと。**

5. 私への報告は以下だけにする（レポート本文を会話に貼らない）:
   - 生成した2ファイルのパス
   - Critical / Major の件数
   - 「今すぐ直すべき3件」の一行要約
   - 棄却された指摘の件数
   - CLAUDE.md への追記提案の有無

## 引数

$ARGUMENTS が指定されている場合は、その観点のエージェントだけを走らせる。
例: `/review-site receipt-pipeline data-integrity`
`all` を指定すると9体すべてを走らせる。

