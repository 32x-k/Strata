# Q2_0 Ringの持続decode測定

2026-10-08にWindowsのAMD Radeon AI PRO R9700（gfx1201）で行った測定です。目的は、17 GiBのbounded Expert Ringで511-token promptから1,000 tokenと2,000 tokenを生成し、速度、RAM、GPU memory、Ring I/Oの傾向を確認することです。

## 実行条件

CPUにはAMD Ryzen AI 9 HX 370（12 cores / 24 logical processors）を搭載し、物理RAMは27.6 GiBあります。モデルはcanonical indexed Q2_0 `experts.bin`を使いました。入力には512個のtoken IDを渡し、エンジンの`--stats`はpromptを511 tokensと報告しました。

全runでgreedy sampling、17 GiB Ring、static profile-filled GPU cache 20,201 experts、7 pool workers、`--expert-cache auto`、`--prefill auto`、`--kv int8`、`--max-context 32768`を使っています。Windowsでは`STRATA_UNBUFFERED_LOAD=1`と`STRATA_STAGER_BATCH=8`を設定しています。通常のbaselineではWMMAとspeculative decodingを使っていません。ファイルキャッシュは消去せず、runは逐次実行です。

## Decode速度

| 測定 | runごとの速度 | 合計token数 ÷ 合計decode時間 |
| --- | --- | ---: |
| traceなし、1,000 tokensを3回 | 21.14 / 20.48 / 19.21 tok/s | 20.24 tok/s |
| Ring traceあり、1,000 tokensを3回 | 21.30 / 21.37 / 21.61 tok/s | 21.43 tok/s |
| traceなし、2,000 tokensを1回 | 20.32 tok/s | 20.32 tok/s |
| Ring traceあり、2,000 tokensを1回 | 21.08 tok/s | 21.08 tok/s |

traceなしとtraceありの1,000-token runは全て同じ出力でした。2000-token runも先頭1,000 tokensが同じでした。2つの測定群は設定が同じですが、群の間で速度差が残りました。温度、電源状態、バックグラウンド負荷を記録していないため、この約5.9%の差を性能改善やtraceの影響とは判断できません。群をまたいだ速度比較には使わず、traceありの数値は診断結果として扱います。

traceありの1,000-token runを合わせた3,000 tokenでは、token latencyの平均46.67 ms、p50 46.09 ms、p95 53.52 ms、p99 58.12 msでした。各runの速度は21.30〜21.61 tok/sです。prompt prefillは平均2.045 s、time to first tokenは平均2.172 sでした。

traceありの2,000-token runは平均47.44 ms/token、p50 46.92 ms、p95 54.34 ms、p99 58.44 msです。最初の1,000 tokensは平均46.84 ms、次の1,000 tokensは48.04 msまで上がりました。後半は約2.6%遅くなりましたが、急激な低下は見られませんでした。`decode-bins.csv`には各runの100-token区間を記録しています。

## Ring I/OとRAM

1,000-token trace runでは、decode中のRing miss/readが580件、payloadが801,792,000 bytes、要求I/Oが802,975,744 bytesでした。累積I/O waitは3 runの平均で541 ms、Ring thrashは0でした。最大outstanding readは32、stagerの最大copy batchは8、scratch bufferは43 MiBです。I/O waitは要求ごとの累積値で、並行処理と重なる場合があるため、decode時間の内訳として単純に差し引けません。

2,000-token runでは710件のmiss/read、981,504,000 bytesのpayload、累積I/O waitは683 msで、thrashは0でした。最初の1,000 tokensのmissは580件、次の1,000 tokensは130件でした。今回のdecode時間に比べてRing I/O waitの合計は小さく、ディスク待ちが主な律速要因と判断できる結果は得られていません。

Ringは13,204 slotsで、slotあたり1,382,400 bytes、合計17.00 GiBです。100-token traceのHIP memory queryは約31.29 / 31.86 GiBを示しました。WindowsのGPU utilizationとGPU process-memory countersは0を返し、妥当な使用率を得られませんでした。0%をGPUがアイドルだった証拠として扱っていません。

traceありrunのprocess working setは最大約18,236 MiB（17.81 GiB）、空き物理RAMは最小1,329 MiB（1.30 GiB）でした。I/O scratchはExpert slot budgetには含まれませんが、process RAMには含まれます。`--expert-ram-gb 17`はプロセス全体やWindows全体のRAM上限ではありません。

## Ring要求traceと置換方式の再生

別の2,000-token runで`STRATA_RING_ACCESS_TRACE`を有効にし、expert要求、hit/miss、victim、release、I/O batchを記録しました。生成token列は既存のbaselineと2,000 tokens全て一致しました。traceには起動時のprofile fillも含まれますが、decode区間は405,128 requests、404,418 hits、710 missesでした。missは最初の1,000 tokensで580件、次の1,000 tokensで130件です。trace全体をLRUで再生した結果はC++側summaryと一致しました。

同じrequest traceを容量別にoffline再生したmiss数です。小さい容量は実機で動かした値ではなく、LRUで記録した要求順を再生した結果です。

| Ring容量 | LRU | SLRU | decay LFU | cost-aware LFU |
| ---: | ---: | ---: | ---: | ---: |
| 17.00 GiB（13,204 slots） | 710 | 710 | 710 | 710 |
| 6.44 GiB（5,000 slots） | 710 | 710 | 710 | 713 |
| 2.57 GiB（2,000 slots） | 710 | 760 | 1,104 | 1,191 |

このtraceでは17 GiB時に方式間の差がなく、容量を小さくした再生では試した方式がLRUを上回りませんでした。現状はLRUを変更しません。cost-aware LFUは全traceから推定したexpert別I/O costを使うoffline実験で、実行時のオンライン推定ではありません。

top-4の次層request-ID予測はprecision 56.6%、recall 68.4%でしたが、予測IDのうちRing missだったものは0件です。これは要求IDの一致率であり、既存のrouter lookaheadの評価ではありません。先読みがI/O待ちを隠すかどうかも測っていないため、専用prefetch slotや新しいretention機構を導入する根拠にはしません。

実行時のRingは引き続きLRUです。一般的な置換方式の実験はhardware-independentな[`tools/ring_policy_sim.py`](../../../tools/ring_policy_sim.py)に分離し、R9700/Windows固有のI/O条件やbenchmark設定とは混ぜていません。trace記録は`STRATA_RING_ACCESS_TRACE=<JSONLファイル>`で明示的に有効にする診断機能です。Windowsの非バッファードI/Oは既存のplatform-specific pathで、今回新しいI/O最適化を採用したわけではありません。集計は`ring-policy-replay.json`にあります。生trace、log、telemetryはローカルに残し、Gitには含めません。

## CPU・GPU側の診断

traceありの1,000-token runで、CPU expert poolは平均9.54 ms/token、engineの`host after ring`計測は平均22.98 ms/token、Ring latencyは約0.44 ms/layerでした。R9700のGPU-only graph replayは10.76 ms/tokenでしたが、これは通常decodeと異なる診断経路です。これらの値を足し引きして通常decodeの内訳とはみなしません。

GPU stageの差分計測は整合しませんでした。3-graphの合計は11.662 ms/tokenでしたが、5 stageの差分値には負値があり、合計は7.191 msでした。prefix sweepも8.996 msとなったため、stage別の内訳から結論を出していません。

pool worker数を変えたtraceありrunでは、7 workersが合計21.43 tok/s、5 workersが21.04 tok/s、2 workersが19.94 tok/sでした。どの設定もbaselineと同じ出力でした。今回比較した範囲では7 workersが最速です。

CPU poolの処理中にhost threadもExpertを計算する既定動作を、`--no-host-worker`で止めるA/Bを行いました。7 workersのまま3 runずつ交互に実行し、既定動作は20.76、21.43、21.50 tok/s、`--no-host-worker`は21.25、21.23、21.03 tok/sでした。合計速度はそれぞれ21.22 tok/sと21.17 tok/sで、出力はすべて一致しました。CPU pool計測は9.68 ms/tokenから10.06 ms/tokenへ、`host after ring`は23.21 ms/tokenから23.44 ms/tokenへ変化しました。差は小さく、`--no-host-worker`でもhost側の区間は短くなりませんでした。既定動作を維持します。

`--spec 4`の1,000-token試験は34.68 tok/sでしたが、出力はtoken 6からbaselineと異なりました。この速度は同じ出力を保つ比較ではないため採用しません。MTP-onlyの128-token試験はdraft受理が2/375（0.5%）でtoken 8から分岐し、`--no-verify-graph`の試験もtoken 6から分岐しました。

## 解釈とファイル

今回、decode性能を上げるコード変更は評価していません。Ring I/Oは主な律速に見えず、CPU pool数の比較では7 workersが最速でした。CPU/host側の負荷は追加調査の候補ですが、GPU utilization countersとstage timingに問題があるため、内訳の断定には使っていません。次の最適化は一つずつ変更し、同じ条件で出力一致と速度を比べます。

- `summary.json`は測定条件と集計値です。
- `runs.csv`はrun単位の速度、token latency、Ring I/O、RAM、出力一致を記録しています。
- `decode-bins.csv`はtraceありbaselineの100-token区間です。worker数のA/Bは`runs.csv`に記録しています。
- 生ログ、trace JSONL、telemetry CSVはこのフォルダーにローカル保存しています。絶対パスやprocess IDを含むファイルがあるため、Gitには含めません。

`STRATA_RING_TRACE=<JSONLファイル>`はRing利用時の非speculative decode向け診断です。token latency、100-token bins、Ring/I/O統計、HIP memoryを記録します。`STRATA_RING_ACCESS_TRACE=<JSONLファイル>`はRing要求とI/O batchを記録し、non-speculative、non-serve、non-hybrid実行に限定しています。どちらも通常利用では設定しないでください。
