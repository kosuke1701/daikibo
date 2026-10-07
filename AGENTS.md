# 開発情報

- 開発対象リポジトリ: https://github.com/kosuke1701/daikibo
- 開発者: Kosuke Akimoto <koske1701@gmail.com>
- Git の作成者・コミッター情報には上記を使用し、設定はこのリポジトリ内に限定する。

## 開発環境

- Linux、CPython 3.13、Git を使用する（詳細は README.md）。
- 開発用インストール: `python3.13 -m venv .venv`、`.venv/bin/python -m pip install '.[dev]'`。
- テスト: `.venv/bin/python -m pytest -q`。変更に応じて対象テストを選ぶ。

ユーザーが継続利用する開発方針・環境情報を指定した場合は、必要に応じてこのファイルを更新する。一時的なタスクの指示や調査結果はここに蓄積しない。
