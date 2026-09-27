#!/usr/bin/env python3
"""Generate the human API view from registered call signatures, not a second contract."""
from __future__ import annotations
import inspect
import tempfile
from pathlib import Path
from daikibo.control import Control
from daikibo import __version__


def main():
    lines=[f'# 公開API — {__version__}', '', 'GENERATED — DO NOT EDIT。`tools/generate_api.py`で実コードから生成。', '',
           '単一ユーザーのローカルRPC。認証/特権なし。read_onlyは通信の再送区分であり、監査記録の付随書込みまで禁止する意味ではありません。', '',
           '分割uploadの確定は未審査の提案です。TaskやProgramの状態は実証拠と現在版を照合して決まり、API呼出成功だけでは最終受入になりません。', '',
           'Consumer-Cのbuild_relation_request / build_review_assurance / evaluate_criteriaは内部controllerのin-process helperであり、公開routeではありません。このため生成API一覧には載せません。', '',
           'run.work_changes / run.work_readはproject-scopedな通常run working-productを、run.recovery / run.recovery_readはtask-scopedなcollector失敗時のfailed-artifacts manifestを読む別契約です。changes entryはafter.kind=fileのときafter.blobをexpected SHAとして選び、after=nullの削除はread対象にしません。これはwork_read返答のsha256とは区別します。指定run・entry(repo/path)・SHA・pageを毎回固定し、全ページのdecoded bytesを保存してsize/SHAを再検証します。base64はtransport専用でconsoleへ本文を流さず、readは採用・成功・承認・再実行を行いません。owner権限へのすり替えやcurrent pathの推測はせず、旧手順は履歴として扱います。', '',
           '| 操作 | 引数 | 読取区分 |','|---|---|---|']
    with tempfile.TemporaryDirectory(prefix='daikibo-api-') as tmp:
        c=Control(Path(tmp)/'control',mode='validation',start_workers=False)
        try:
            for name,fn in sorted(c.routes.items()):
                signature=str(inspect.signature(fn)).replace('|','\\|')
                lines.append(f'| `{name}` | `{signature}` | {str(name in c.read_routes).lower()} |')
        finally:c.close()
    root=Path(__file__).resolve().parents[1]
    (root/'docs/API.md').write_text('\n'.join(lines)+'\n')
    print(len(lines)-10,'registered operations')

if __name__=='__main__':main()
