# 原文付きSchema・実データ検査 — dev11

これは、既存の標準契約inventoryに加える**実データの構造診断**です。
取り込み、診断、実テストの実行、意味判断、完成認定を分けます。

## 利用する場面

1. ユーザー/API提供元から渡されたJSON Schemaと、検査するJSONデータを`document.import`で保存する。
2. `contract.schema_capabilities`で対応規則・上限を確認する。
3. `contract.check_schema`で、指定Schemaをこのエンジンが解釈できるか調べる。
4. `contract.check_instance`で原文のhashを指定して実データを検査する。
5. 正常例・異常例、境界値、利用者の意図に沿った検証方法はAgentが設計する。元の仕様を緩めない。
6. 実Taskには後述のCLIを通常の`kind=junit`検証コマンドとして登録し、管理Runtimeで実行する。
7. Test adequacy/spec/quality等の別レビューと通常のGateを通す。

API診断はread操作です。呼出し成功だけではrun/receipt/review/waiver/artifact採用を作りません。
一つのサンプルが適合しても、利用側全体への互換性、HTTP上の振る舞い、業務要件、配備可能性は証明しません。

## 操作

```
contract.schema_capabilities()
contract.check_schema(document, expected_digest, entry="#", dialect=None)
contract.check_instance(document, expected_digest,
                        instance_document, instance_digest,
                        entry="#", instance_entry="#", dialect=None)
```

既存CLI/RPC/同会話のnative.actions/Supervisorから利用できます。新しいサーバーやschedulerはありません。
Schemaとinstanceは同じprojectの原文DOCを参照します。版違い・他project・破損した原文は拒否します。
結果には原文hashと選択位置、engineと製品版、結果digestを含めます。

## 判定を混同しない

| result.status | result.valid | 意味 |
|---|---|---|
| `supported` | null | Schemaの対応範囲を確認しただけ。データは未検査 |
| `valid` | true | 指定したデータが対応規則を満たす |
| `invalid` | false | データが対応規則に違反する |
| `invalid_schema` | null | 規則の型・参照などが不正。データの反例ではない |
| `unsupported` | null | 未対応keyword/dialect/ref等がある。成功でもデータ不適合でもない |
| `invalid_json` / `invalid_instance_selection` | null | JSON/選択位置が不正。データ不適合と数えない |
| `limit_exceeded` | null | 有限容量を超えた。切り捨てて合格にしない |

全応答は`complete_standard_conformance=false`, `compatibility_proven=false`です。
Schema内で未対応規則を見つけた場合、使わない分岐/定義にある場合も保守的に判定不能にします。
レポートのissue一覧は上限付きで、全件数と省略有無を明記します。エラー表示が切れたことを合格に変えません。
データ不適合の場所は最初に確定した一件を返します。全違反一覧の生成器ではありません。

## 対応範囲

**JSON Schema 2020-12の次の静的機能**を実装しています。

- Boolean schema、type/type union、const/enum、JSONの等価性。
- minimum/maximum/exclusive境界、multipleOf、文字数、配列/プロパティ件数。
- properties/required/additionalProperties/propertyNames/dependentRequired/dependentSchemas。
- prefixItems/items/contains/minContains/maxContains/uniqueItems。
- allOf/anyOf/oneOf/not/if/then/else。
- $defs、同じ原文内の静的$ref、静的anchor。データを辿る再帰。
- root $idの同一文書URI参照。一つのSchemaで$ref以外の隣接条件も検査する。
- title/description/$comment/default/examples/readOnly/writeOnly/deprecatedは注釈。型変更、defaultの自動挿入、HTTP方向判定には使わない。

**実装していないもの**: pattern/patternProperties (ECMA-262)、format assertion、content系、
unevaluated系、dynamic scope、vocabulary、nested $id、外部文書参照の解決、別draft/dialect、
完全な規格検証・互換性証明。未知keywordを「たぶん注釈」と解釈しません。
完全な標準支援はGAP-06の残作業で、サブセット成功によって範囲を削減したものではありません。

OpenAPI3.1では、JSON Pointerで選択したSchema部分とそのlocal refだけを検査します。
文書全体、HTTPパラメータのserialization、認証、ステータス選択、readOnly/writeOnlyの方向、
consumer互換性をこの診断で検証済みにしません。OpenAPI3.0のSchemaを2020-12へ自動変換しません。
Schemaの場所をAgentが正しく選んだかもレビュー対象です。

## 数値と原文

Schema/instanceの原文をDecimalで読み、binary floatへの丸めを挟みません。
`true`と`1`は別、`1`と`1.0`は数として同値、`multipleOf:0.01`と`4.02`は正確に評価します。
元の数値をJSON出力し直して改訂したり、重複キーを後勝ちで受理したりしません。

最大: 各原文6,000,000bytes、JSON200,000node、Schema20,000node、depth64、
evaluation200,000step、number1024digit/exp±2048、pointer16,000文字。
評価stepはSchema訪問と主要ループの回数で、厳密なCPU時間上限ではありません。大きな値の等価性判定にも計算量があるため、実テストにはRuntimeの実行期限も適用します。
非進行の循環$refは成功と仮定せず判定不能。正規表現の任意実行・外部参照取得はしません。

## 管理された実テストへ組み込む

```json
{
  "id": "contract-valid-response",
  "kind": "junit",
  "argv": ["python", "-m", "daikibo.schema_cli", "--schema", "contracts/response.json",
           "--instance", "tests/samples/valid.json", "--expect", "valid",
           "--name", "valid-response", "--report", "contract-valid.xml"],
  "report": "contract-valid.xml",
  "required_tests": ["valid-response"]
}
```

不正データの試験は`--expect invalid`にします。ただし未対応Schema、JSON解析失敗、容量超過は
`error`/exit2であり、不正データを正しく拒否した試験にはなりません。
データ適合/不適合が確定して期待に反する場合はfailure/exit1です。
Schemaが$schemaを持たなければ`--dialect https://json-schema.org/draft/2020-12/schema`を明示します。
期待の指定を含め、テスト計画の意味を別Reviewerが確認します。

JUnitは1つのsampleの検査記録で、schema/instanceのSHA-256と選択位置を保存します。
Runtimeは実process、終了コード、入力版、レポート、入力書換えを観測します。直接CLIを起動して
できたXMLを「管理されたreceipt」として登録できる新しい抜け道は作っていません。

## 検証の境界

独自の意味/数値/負例と、RPC/native/実CLI/JUnit/既存Task Gateを試験しています。
公式JSON Schema Test SuiteはDNS制約で取得できず、実行済みとはしません。
環境に元々あるjsonschema 4.26.0とは別途有限の生成例で比較しますが、製品依存へ追加しません。
その比較も規格全体の準拠や独立レビューの代用ではありません。

