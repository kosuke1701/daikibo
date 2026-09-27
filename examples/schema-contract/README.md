# 有限Schemaプロファイルのコマンド例

これは架空のレスポンス形状だけの例です。外部へ通信しません。残額、権限、二重返金、同時実行、providerとの実整合性は別の試験が必要です。

インストール済みのdev11環境で、チェックポイントのrootから実行します。

```bash
python -m daikibo.schema_cli --schema examples/schema-contract/response.schema.json \
  --instance examples/schema-contract/valid.json --expect valid --report /tmp/contract-positive.xml
python -m daikibo.schema_cli --schema examples/schema-contract/response.schema.json \
  --instance examples/schema-contract/invalid.json --expect invalid --report /tmp/contract-negative.xml
```

両方exit0は、それぞれ期待したデータ適合/不適合が観測された意味です。過去のJUnitや手で起動した結果を管理済みreceiptとして持ち込まず、正式な作業ではdocs/SCHEMA-CHECKS.mdのようにRuntime経由で実行します。
未対応機能がある場合は、負例を期待していてもerror/exit2です。元の契約を弱めてこの例へ合わせないでください。
