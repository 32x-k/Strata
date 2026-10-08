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
- batch、PCIe経由のExpert処理、複数GPU、layer split
- Ringを使った通常の外部OpenAI互換・Anthropic互換サーバー

ローカルブラウザーチャットは別経路です。`strata --serve`を固定プロファイルと単一GPUの条件で起動し、モデルとRAM領域を会話の間も保持します。

## RAMの上限

`--expert-ram-gb N`はExpert用の容量をN GiBに制限します。これはPC全体やプロセス全体のRAM使用量を制限する設定ではありません。Windowsのcommitや、ほかのプロセスが使うRAMまでは保証しません。

Resident RAMとRingは同じ上限を分け合えます。たとえば`--expert-ram-gb 14 --resident-budget-gib 6`では、最大6 GiBをResidentに使い、残りをRingに割り当てます。Resident RAMを先に確保する仕組みです。確保に失敗した場合はResident分を解放して、全量をRingに回します。6 GiBを14 GiBに追加する設定ではありません。

実機の32 GB RAM環境では17 GiBを使いました。14〜16 GiBにすると、WindowsのRAMとcommitにより多くの余裕が残ります。これは安全な最低容量ではありません。1層で必要になるExpertの作業集合がリングに収まる必要があります。

## セットアップと起動

Strataフォルダーで次のコマンドを実行してください。

```bat
START-HERE.bat --yes --backend hip --family qwen --model Q2_0 --expert-ram-gb 17
```

セットアップはフォーク版エンジンを`build-hip-win`から選び、`run-q2_0-ring.bat`と`run-q2_0-ring-chat.bat`を作ります。公開版エンジンにRing実装がない場合でも、自動で切り替えることはありません。別のビルドを使う場合は`--ring-engine DIR`を指定します。

通常のRing起動スクリプトはエンジンを直接起動し、`--tokens`で入力を受け取ります。静的ExpertプロファイルをGPUキャッシュへ読み込むため、GPUにあるExpertはRingから読みません。

ブラウザーチャットは`run-q2_0-ring-chat.bat`から起動します。こちらはモデルとHybrid/Ringの領域をターン間で保持します。初期状態では再読み込みしません。1回のリクエストごとにモデルを再起動する診断モードは、`--reload-each-turn`で選べます。このローカル画面は外部向けの互換APIではありません。

## R9700での測定

測定はRadeon AI PRO R9700（31.9 GiB、gfx1201）で行いました。値はこのカードと記載した条件での結果で、ほかのPCやプロンプトにそのまま当てはまるとは限りません。

| 条件 | 結果 |
| --- | --- |
| 17 GiB Ring、GPUキャッシュなし | 16生成トークンの全体で6.77 tok/s |
| 17 GiB Ring、静的GPUキャッシュあり、7 pool workers、`int8` KV | 4生成トークンで18.87 tok/s。ルーティングされたExpertの3,786/3,840件がGPUキャッシュにありました |

この2つは生成長が違うため、速度を直接比較できるベンチマークではありません。

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
