"""コストコの安売り情報をまとめる静的サイトのビルダー。

外部依存ゼロ（標準ライブラリのみ）。`python -m costco.cli --help` を参照。
"""

__all__ = ["models", "store", "parse", "sources", "build"]
