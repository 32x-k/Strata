# Windows AMD向けのbounded Ring

[英語の技術詳細](DETAILS.md#speed-measured) · [セットアップ手順（日本語）](AI_SETUP.ja.md)

このブランチでは、WindowsのAMD GPUでQ2_0モデルを動かす実験的なRing経路を追加しました。モデル全体をRAMへ置く代わりに、Expertを固定容量のRAMリングへ読み込み、必要に応じて入れ替えます。

## 対応範囲

この経路が対応するのは、Windows、AMD GPU 1枚、標準形式のインデックス付きQ2_0 `experts.bin`です。ほかのモデル形式や複数GPUでは使えません。

GPUキャッシュを併用する場合は、`--expert-cache`と静的な`--expert-profile`を両方指定します。起動時にプロファイル上位のExpertをGPUキャッシュへ読み込み、その後の動的な追加や追い出しは行いません。GPUキャッシュにないExpertはCPU側で処理します。

Ringでは通常のtoken graphを無効にします。MTPのspeculative decodingには、静的GPUキャッシュと固定verifier slotが必要です。VerifierのCUDA Graphは通常どおり有効です。`--no-verify-graph`または`STRATA_VERIFY_NO_GRAPH=1`を指定すると、Graphを使わない遅い診断経路に切り替わります。共有Expertをverifier stream上で処理するため、処理順を確認しやすくなります。

未対応の機能は次のとおりです。

- GGUFからのExpert組み立てと、標準Q2_0以外のExpert形式
- 実行中のGPUキャッシュ追加・追い出し
- `--batch`推論、PCIe経由のExpert処理、複数GPU、layer split
- このセットアップ経路での画像入力

OpenAI互換・Anthropic互換APIには、既存の`serve.server`と`StrataEngine`を使います。固定プロファイル、単一GPUの`strata --serve`プロセスを会話の間も保持します。API serverはテキスト入力専用で、リクエストを一件ずつ処理します。

## RAMの上限

`--expert-ram-gb N`はExpert用の容量をN GiBに制限します。これはPC全体やプロセス全体のRAM使用量を制限する設定ではありません。Windowsのcommitや、ほかのプロセスが使うRAMまでは保証しません。

Resident RAMとRingは同じ上限を分け合えます。たとえば`--expert-ram-gb 14 --resident-budget-gib 6`では、最大6 GiBをResidentに使い、残りをRingに割り当てます。Resident RAMを先に確保する仕組みです。確保に失敗した場合はResident分を解放して、全量をRingに回します。6 GiBを14 GiBに追加する設定ではありません。

実機の32 GB RAM環境では17 GiBを使いました。14〜16 GiBにすると、WindowsのRAMとcommitにより多くの余裕が残ります。これは安全な最低容量ではありません。1層で必要になるExpertの作業集合がリングに収まる必要があります。

## セットアップと起動

Strataフォルダーで次のコマンドを実行してください。

```bat
START-HERE.bat --yes --backend hip --family qwen --model Q2_0 --expert-ram-gb 17
```

セットアップはフォーク版エンジンを`build-hip-win`から選び、`run-q2_0-ring.bat`、`run-q2_0-ring-chat.bat`、`run-q2_0-ring-api.bat`を作ります。公開版エンジンにRing実装がない場合でも、自動で切り替えることはありません。別のビルドを使う場合は`--ring-engine DIR`を指定します。

通常のRing起動スクリプトはエンジンを直接起動し、`--tokens`で入力を受け取ります。静的ExpertプロファイルをGPUキャッシュへ読み込むため、GPUにあるExpertはRingから読みません。

ブラウザーチャットは`run-q2_0-ring-chat.bat`から、外部アプリ向けAPIは`run-q2_0-ring-api.bat`から起動します。どちらもモデルとHybrid/Ringの領域をターン間で保持します。APIのOpenAI base URLは`http://127.0.0.1:8080/v1`、Anthropic endpointは`http://127.0.0.1:8080/v1/messages`です。OpenAI互換の`/v1/chat/completions`とAnthropic互換の`/v1/messages`を利用できます。両方同時に起動するとポートが競合するため、片方だけを使ってください。

初期設定は`127.0.0.1`だけで待ち受けます。ほかのPCから接続する場合は、API設定JSONの`host`を変更し、`api_key`を設定するか、`STRATA_API_KEY`をセットしてください。API serverは、API keyなしでloopback以外にbindしようとすると起動を拒否します。`--reload-each-turn`はブラウザーチャットだけの診断モードで、1回のリクエストごとにモデルを再起動します。

## ファイル読み込み

Windowsでは、RAMリングに入らないExpertを`experts.bin`から読み込みます。起動時に選ぶ方式はOSのファイルキャッシュか、`FILE_FLAG_NO_BUFFERING`経由の読み込みです。`STRATA_UNBUFFERED_LOAD=1`は非バッファードI/O、`=0`はファイルキャッシュ経由を指定します。直接読み込みに失敗した場合は、バッファード読み込みへ切り替えます。

非バッファード経路では、近接したExpertを4 KiB境界に合わせたoverlapped `ReadFile`でまとめて読み込む方式です。ページ固定できないRingではプリフィルのstagerを使います。既定のbatchは8件です。`STRATA_STAGER_BATCH=1`で一括化を無効にし、2〜32件のbatchも指定できます。ページ固定できるRingでは、レイヤーごとに最大32件をまとめて読み込みます。一時読み込みバッファーは`--expert-ram-gb`のExpert slot予算外です。したがって、PC全体やプロセス全体のRAM使用量は制限しません。

実装時に参照した本家リポジトリは[Niko1221/Strata](https://github.com/Niko1221/Strata)です。関連する読み込み変更は[PR #1323](https://github.com/Niko1221/Strata/pull/1323)、[PR #357](https://github.com/Niko1221/Strata/pull/357)、[PR #362](https://github.com/Niko1221/Strata/pull/362)にあります。ここで記載したRing向けの一括読み込みは、このブランチの実装です。

## R9700での測定

測定はRadeon AI PRO R9700（31.9 GiB、gfx1201）で行いました。値はこのカードと記載した条件での結果で、ほかのPCやプロンプトにそのまま当てはまるとは限りません。

| 条件 | 結果 |
| --- | --- |
| 17 GiB Ring、GPUキャッシュなし | 16生成トークンの全体で6.77 tok/s |
| 17 GiB Ring、静的GPUキャッシュあり、7 pool workers、`int8` KV | 4生成トークンで18.87 tok/s。ルーティングされたExpertの3,786/3,840件がGPUキャッシュにありました |

この2つは生成長が違うため、速度を直接比較できるベンチマークではありません。

### Q2_0の持続decode

2026-10-08にR9700で511-token promptの後に1,000 tokensを生成しました。CPUはRyzen AI 9 HX 370、物理RAMは27.6 GiBです。17 GiB Ring、static GPU cache 20,201 experts、7 pool workers、`int8` KV、非バッファードI/O、stager batch 8を使いました。入力には512個のtoken IDを渡し、エンジンはpromptを511 tokensと報告しています。ファイルキャッシュは消去せず、runを逐次実行しました。

traceを無効にした3 runは21.14、20.48、19.21 tok/sで、合計decode時間から計算した速度は20.24 tok/sでした。traceを有効にした別の3 runは21.30、21.37、21.61 tok/s、合計で21.43 tok/sでした。6 runの出力はすべて一致しましたが、測定群の間に約5.9%の速度差が残りました。温度や電源状態、バックグラウンド負荷を記録していないため、これをコード変更やtraceの影響とは判断していません。2,000-token runはtraceなしで20.32 tok/s、traceありで21.08 tok/sでした。両方とも先頭1,000 tokensは一致しました。

traceあり1,000-token runを合わせると、token latencyは平均46.67 ms、p50 46.09 ms、p95 53.52 ms、p99 58.12 msでした。traceあり2,000-token runでは、最初の1,000 tokensが平均46.84 ms、次の1,000 tokensが48.04 msでした。後半は約2.6%遅くなりました。

1,000-token runのRing missは各580件、payloadは801.8 MB、累積I/O waitは平均541 msでした。thrashはありませんでした。I/O waitは並行処理と重なる可能性のある累積値です。decode時間からそのまま差し引けません。2,000-token runではmissが710件、payloadが981.5 MB、累積I/O waitが683 msで、こちらもthrashはありませんでした。今回の測定ではディスク待ちが主な律速要因だとは確認できませんでした。

trace時のprocess working setの最大値は18,236 MiBです。空き物理RAMは一時1,329 MiBまで減りました。`--expert-ram-gb 17`はExpert slotの上限であり、process working setの上限ではありません。`Drive::cpu_ms`で計測したexpert pool dispatchのwall timeは平均9.54 ms/tokenです。この値にはRing解放時の`cudaEventSynchronize`、source準備、activation quantization、pool処理が含まれ、CPU expert計算だけの時間ではありません。engineの`host after ring`は22.98 ms/tokenです。一方、別経路のGPU-only graph replayは10.76 ms/tokenです。測定経路が違うため、これらを足し引きしてdecode内訳とはみなしません。

pool worker数を変えたtraceありrunでは、7 workersが21.43 tok/s、5 workersが21.04 tok/s、2 workersが19.94 tok/sでした。出力はいずれも7-worker baselineと一致しました。今回の範囲では7 workersが最速です。`--spec 4`は34.68 tok/sでしたが、出力はtoken 6から異なりました。出力一致が確認できていないため、この速度は採用していません。

WindowsのGPU utilization countersは0%を返し、使用率の計測としては妥当性を確認できませんでした。GPU stageの差分計測にも負値があり、段階別の内訳には使っていません。測定条件とrunごとの値は[持続decodeの記録](../bench/results/2026-10-08-q2-ring-sustained/README.md)にあります。

### HIP共有Expert stream

HIPでは共有Expertのverifier side streamを初期状態で無効にしました。`STRATA_SH_STREAM=1`を設定すると有効になります。

511-token promptの後にMTP spec4で128 tokenを生成した測定では、side stream無効で50.01 tok/s、有効で37.37 tok/sでした。どちらも39 roundsで、107 draftのうち90を採用しました。

12-token promptの後に512 tokenを生成した測定は、2回の平均で無効時32.03 tok/s、有効時26.82 tok/sでした。それぞれの設定内では同じtoken列になりましたが、設定間では143番目のtokenから結果が分かれました。出力がbit単位で一致するという意味ではありません。

### hipBLASLt tuning table

R9700とhipBLASLt 1.5.0で、`tools/hip/gfx1201-hipblaslt-100500.txt`の32行を読み込みました。511-token promptでは782 launches、fallback 0でした。

同じpromptを2回ずつ測ると、tableありの平均は2.292秒、なしは2.271秒を記録しました。この条件ではtableによる速度向上を確認できませんでした。tableありの実行範囲は2.195〜2.389秒、なしは2.245〜2.297秒です。長いpromptや別のGPUで同じ結果になるとは限りません。

### WMMA

WMMAは`STRATA_HIP_WMMA=1`で有効になる試験的な経路です。`int8` KVを使うgfx12 GPUが必要です。511-token model promptでは、tableありでWMMA無効の平均2.292秒に対し、有効時は2.240秒でした。この小さな比較では約2.3%の差でした。

別の合成attention試験では、context 2048、128 queriesの1 chunkが5.240 msから0.411 msになりました。モデル全体のprompt測定で見えた差はこれより小さく、WMMAは初期状態で無効です。Attentionの加算順序が変わるため、token出力の一致試験とは分けて測りました。

## Resident RAMとの比較

Resident RAMはRingとは別の経路です。`--expert-ram-gb`を指定せず`--resident-experts`を使うと、固定GPUプロファイルにないExpertを匿名RAMへコピーします。AMDでは`STRATA_RESIDENT_PIN=0`が、pageable RAMを使う設定です。

同じR9700と11-token promptで1,024 greedy tokensを生成した測定は、`--no-token-graph`で25.12 tok/s、token graph有効時で54.46 tok/sでした。Resident copyはprompt loanなしで5.63 GiB、Graph経路のcache slotsを借りた状態で8.90 GiBでした。trace-reordered profileを使ったGraph測定では59.27 tok/s、GPU cache hit rateは91.80%から99.52%になりました。いずれも1つのpromptでの測定です。

Resident RAMはWindowsがページングすることがあります。起動時の空きRAM確認も、ほかのプロセスによる使用を防ぐ予約ではありません。Expert用の上限を設定したい場合はRingを使います。RingもPC全体のRAMを制限するものではありません。

## ログと診断

`--stats`を付けると、Ring slotのhit、miss、eviction、thrashなどを確認できます。Verifier Graphを使わない経路は、結果と処理順を確かめる診断用です。通常の経路より遅いため、速度の比較には使わないでください。

`STRATA_RING_TRACE=<JSONLファイル>`を起動前に設定すると、Ringを使う非speculative decodeの診断traceを記録します。tokenごとのlatency、100-token bins、Ring/I/O統計、HIP memoryを出力します。通常利用では設定せず、MTPなどのspeculative decodingとの比較にも使わないでください。

Ringの要求順、hit/miss、退避victim、解放、I/O batchを調べる場合は、`STRATA_RING_ACCESS_TRACE=<JSONLファイル>`を設定します。eventを受け取ったrelease行には、query結果と`cudaEventSynchronize`のホスト時間も記録する仕様です。`python tools/ring_release_sync_report.py <trace.jsonl>`でdecode中の同期回数、未完了event数、待ち時間を集計できます。測定中はreleaseごとにqueryを追加するため、traceの大きさに加えて処理時間も通常実行と比較できません。speculative decoding、`--serve`、Residentとのhybrid実行では使えません。生traceを公開する前にローカルパスや実行情報を確認してください。

[`tools/ring_policy_sim.py`](../tools/ring_policy_sim.py)で`python tools/ring_policy_sim.py <trace.jsonl> --phase decode --capacity <slots>`を実行すると、同じ要求列をLRU、SLRU、decaying-LFU、trace-cost-weighted LFUでoffline再生できます。これはhardware-independentな診断ツールで、実行時のRing policyはLRUのままです。今回のR9700で得た2,000-token traceでは、17 GiBで全方式が710 misses、2.57 GiB相当のoffline replayではLRUより他方式のmissが増えました。別容量で実機確認した結果ではありません。集計と制限は[測定記録](../bench/results/2026-10-08-q2-ring-sustained/README.md)を参照してください。
