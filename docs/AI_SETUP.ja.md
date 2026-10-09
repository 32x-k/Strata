# ユーザーのPCにStrataをセットアップする

[English](AI_SETUP.md) · 日本語

このページは、ユーザーからPCへのStrataのセットアップを頼まれたAIコーディングアシスタント向けです。StrataはQwen3.8-Flash-NextをNVIDIAまたはAMDのGPU 1枚とシステムRAMで動かし、`http://127.0.0.1:8080`でOpenAI互換とAnthropic互換のAPIを提供します。画面を見ながら進める人向けの手順は[INSTALL.md](INSTALL.md)にあります。

Windows AMDのbounded Ringはこのフォーク向けの試験機能です。upstreamの公開版には含まれていない場合があります。

手順を上から進め、何をしているかを平易な言葉で伝えてください。ユーザーが技術に詳しいとは限りません。

## 0. 守ること

- **APIキーなしでサーバーを外部に公開しないでください。** 通常は`--host 127.0.0.1`のままにします。ユーザーが別の端末から接続したいと言った場合だけ、`--host 0.0.0.0`と`--api-key <長くランダムな秘密文字列>`を一緒に指定し、キーをユーザーに伝えてください。APIキーなしでトンネルやポート転送を設定してはいけません。
- セットアップ以外の理由でユーザーのシステムを変更しないでください。必要ならPythonをユーザーアカウントに入れ、その他のファイルはStrataフォルダーと`Strata-data`フォルダー内に置きます。ドライバーを入れる前にユーザーへ確認してください。
- モデルのダウンロードには約70 GB必要です。UnslothのUD-IQ4_XSは94 GB、UD-Q4_K_XLは111 GBです。特に従量制の回線では、ダウンロードを始めてよいか確認してください。
- セットアップは、遅い回線では1時間以上かかります。バックグラウンドで実行するか、長いタイムアウトを設定して出力を確認してください。しばらく表示がなくても終了させないでください。同じコマンドをもう一度実行すれば、止まったところから再開します。

## 1. PCを確認する

手早く確認するには、セットアップのチェック機能を使います。GPU、ドライバー、RAM、CPU、空きディスク容量と、Strataを動かせるかどうかを表示します。リポジトリを取得した後に必要なPythonと`.venv`以外はインストールしません。

```text
Windows:  START-HERE.bat --check
Linux:    ./setup.sh --check
```

手動で調べる場合は次のコマンドを使います。

| 項目 | Windows（PowerShell） | Linux |
| --- | --- | --- |
| OS | `[Environment]::OSVersion` | `cat /etc/os-release` |
| NVIDIA GPU、VRAM、ドライバー | `nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv` | 同じコマンド |
| AMD GPU | `Get-CimInstance Win32_VideoController \| Select-Object Name, AdapterRAM`（AdapterRAMは4 GBまでしか表示しないため、GPU名を確認します） | `lspci \| grep -i -E 'vga\|display'`。VRAMは`cat /sys/class/drm/card*/device/mem_info_vram_total`（単位はバイト）。ROCm導入済みの場合だけ`rocm-smi`も使えます |
| RAM | `(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB` | `free -g` |
| 空きディスク容量 | `Get-PSDrive -PSProvider FileSystem` | `df -h .` |

必要条件の詳しい説明は[INSTALL.md](INSTALL.md#what-you-need)を参照してください。

- **GPU：** NVIDIA GeForce RTX 20、30、40、50シリーズ、またはAMD Radeon RX 7900 XT / XTX、RX 7800 XT / 7700 XT、RX 9060 XT、RX 9070 / 9070 XT、Radeon AI PRO R9700、RX 6800 / 6900シリーズ。VRAMは12 GB以上が必要です。NVIDIAの8 GBカードも動きますが、かなり遅くなります。GTX 10シリーズ以前と内蔵GPUは非対応です。Pascal / Voltaカード（P40、V100）や一部の古いAMDカードには、ユーザーが選んで使う試験的な経路があります（[OLDER_GPUS.md](OLDER_GPUS.md)）。
- **ドライバー：** NVIDIAは580以降。AMDはLinuxではカーネルのamdgpuドライバー、Windowsでは最新のAMD Adrenalinドライバーが必要です。ドライバーがない、または古い場合は、ユーザーに更新と再起動を案内してください。ユーザーから頼まれない限り、ドライバーをこちらでインストールしないでください。
- **RAM：** 32 GB以上（手順3を参照）。**ディスク：** 約80 GBの空き容量。NVMe SSDを推奨します。**CPU：** AVX2対応のx86-64が必要です。AVX2非対応（Xeon E5 v1/v2以前）のPCにもセットアップできますが、試験的で遅く、PC上で10〜20分かけてビルドします。開始前に説明してください（[INSTALL.md](INSTALL.md#older-cpus-experimental)）。
- **OS：** Windows 10/11またはLinux。Ubuntu 22.04/24.04ではセットアップが自動で進みます。

条件を満たさない場合は、不足している項目を伝えて作業を止めてください。

## 2. Strataを取得する

```text
git clone https://github.com/Niko1221/Strata.git
```

Gitを使わない場合は、https://github.com/Niko1221/Strata/archive/refs/heads/main.zip をダウンロードして展開します。モデルファイルはStrataフォルダーの隣にある`Strata-data`へ保存します。別のドライブに置く場合は、`--data-dir <path>`を指定してください。保存先には十分な空き容量があるドライブを選びます。

## 3. RAMに合わせてモデルを選ぶ

主にコードを書く用途か、ユーザーに確認してください。コード用途なら、RAMの量にかかわらずCoderが候補になります。

| RAM | `--family` / `--model` | 備考 |
| --- | --- | --- |
| 32 GB | `--family coder`（サイズIQ1_M） | コード向け。VRAM 24 GBのカードならQ2_0とIQ2_XSも動きます。セットアップは低RAMモードを選びます |
| 48 GB | `--family qwen --model IQ2_XS` | Q2_0も選べます。こちらが最速です |
| 64 GB | `--family qwen --model IQ2_XS`（推奨） | IQ3_XXS / IQ3_Sは遅くなりますが、少し高品質です |
| 96 GB以上 | `--family qwen --model IQ3_S` | `--family unsloth --model UD-IQ4_XS`は約4-bitで94 GB。RAMが約80 GB未満の場合、Expertの一部をSSDから読むため遅くなります。`--model UD-Q4_K_XL`は試験的で、NVIDIAとNVMe SSDが必要です。RAM 64 GBで毎秒7〜8.5トークンです |

`--family swift`はQwenの代わりに使えるSwift 1.5です。考える時間を短くしたファインチューンで、サイズはQ2_0、IQ2_XS、IQ3_XXSです。`--yes`を指定し、`--model`を省略すると、セットアップがRAMに合うサイズを選びます。詳しくは[MODELS.md](MODELS.md)を参照してください。

## 4. 質問に答えずセットアップする

Strataフォルダーから実行します。WindowsのPowerShellでは、`cmd`を使うか、`cmd /c START-HERE.bat ...`の形で呼び出します。

```text
Windows:  START-HERE.bat --yes --family qwen --model IQ2_XS --no-start
Linux:    ./setup.sh --yes --family qwen --model IQ2_XS --no-start
```

オプション一覧は`START-HERE.bat --help`で確認してください。

| オプション | 内容 |
| --- | --- |
| `--yes` | すべての質問におすすめの設定で答えます |
| `--family qwen\|swift\|coder\|unsloth` | モデルの種類を選びます |
| `--model Q2_0\|IQ2_XS\|IQ3_XXS\|IQ3_S\|IQ1_M\|UD-IQ4_XS\|UD-Q4_K_XL` | サイズを選びます。CoderはIQ1_M、UnslothはUD-IQ4_XSまたはUD-Q4_K_XLです |
| `--context N` | コンテキスト長をトークン数で指定します。初期値はVRAM 14 GB未満で32768、20 GB未満で65536、それ以上で131072です |
| `--vision yes\|no\|gpu\|cpu` | 画像を読むか選びます。`--yes`では画像機能を無効にします。AMDカードでは`cpu`を使います |
| `--gpu N` / `--gpus 0,1` / `--gpus all` | GPUを1枚、または複数枚選びます。初期値はVRAMが最大のカードです |
| `--backend cuda\|hip` | NVIDIAはCUDA、AMDはHIPを使います。片方の種類のカードしかないPCでは自動選択します |
| `--data-dir PATH` | 70〜120 GBのモデルファイルを置く場所です |
| `--port N` | サーバーのポート番号です。初期値は8080です |
| `--host H --api-key K` | PC外から接続できるようにします。APIキーも必ず設定してください |
| `--no-start` | インストールだけを行い、サーバーは起動しません |
| `--setup` | 別のモデルを追加するか、インストール済みモデルの設定を変更します |
| `--check` | PCの確認だけを行います |
| `--expert-ram-gb N` | Windows AMDのQ2_0限定です。Expert用RAMをN GiBに制限する、直接実行・ブラウザーチャット・OpenAI/Anthropic APIサーバーの起動スクリプトを作ります。セットアップ中は起動しません |
| `--resident-budget-gib N` | `--expert-ram-gb`と一緒に使うと、同じ予算のうちN GiBをResident RAMに割り当て、残りをRingに使います。それ以外では通常のモデルセットアップに使います |
| `--ring-engine DIR` | `--expert-ram-gb`で使う、フォーク版エンジンのフォルダーです。初期値は`build-hip-win`です。公開版エンジンへは切り替えません |

AIエージェントには`--no-start`を推奨します。サーバーはウィンドウを閉じるまでフォアグラウンドで動くため、起動するとシェルが使えなくなる場合があります。手順6で別に起動してください。

### OS別の注意

- **Linux：** venv対応Pythonがない場合、セットアップは`sudo apt`（またはdnf/pacman）でインストールします。AMDでは`build-essential`と`git`も必要になる場合があります。`sudo`が必要でもユーザーのパスワードは入力できません。ユーザーに`sudo apt install python3-venv build-essential git`などを実行してもらい、その後セットアップを再実行してください。
- **LinuxのAMD：** システムにROCm 7がない場合、セットアップは`.venv`内に約10 GBのROCmをインストールします。sudoは不要です。その後GPU向けにエンジンをビルドします。初回は10〜20分かかります。
- **WindowsのAMD：** 0.1.34以降、セットアップはROCmを含む約550 MBのビルド済みエンジンをダウンロードします。ビルドは不要で、AMDドライバーだけが必要です。モデルをダウンロードする前に`engine\strata-device.exe --list-devices`を実行します。GPUが表示されない場合はAMD Softwareのドライバーを更新してください。現在、モデルごとに使えるGPUは1枚で、Windowsでは画像を読めません。新しい機能のため、動作状況を報告してください（[AMD_HIP.md](AMD_HIP.md)）。
#### Windows AMDのbounded Ring

この試験機能は、このフォークに追加したものです。upstreamの公開版には含まれていない場合があります。

次のコマンドは、インデックス付き標準Q2_0パックを準備し、`build-hip-win`にあるフォーク版エンジンから`run-q2_0-ring.bat`、`run-q2_0-ring-chat.bat`、`run-q2_0-ring-api.bat`を作ります。

```bat
START-HERE.bat --yes --backend hip --family qwen --model Q2_0 --expert-ram-gb 17
```

ブラウザー用は`run-q2_0-ring-chat.bat`、API用は`run-q2_0-ring-api.bat`から起動します。どちらもパックのTokenizerとチャットテンプレートを使い、Hybrid/Ringのプロセスをメッセージ間も保持します。APIは既存の互換サーバーを使い、OpenAIの`/v1/chat/completions`とAnthropicの`/v1/messages`を提供します。初期設定は`127.0.0.1`だけで待ち受け、テキスト入力を1件ずつ処理します。2つのサーバーは同じポートを使うため、片方だけを起動してください。PC外から接続するにはAPI keyが必要です。各ブラウザーメッセージでRingを起動し直す場合は`--reload-each-turn`を追加してください。別のフォーク版エンジンには`--ring-engine DIR`を指定します。

通常の起動スクリプトはエンジンを直接起動します。入力には`--tokens`を指定してください。静的ExpertプロファイルをGPUキャッシュに読み込み、キャッシュにないExpertはRingのCPU経路で処理します。

実機の32 GB PCでは17 GiBを使いました。14〜16 GiBにすると、RAMとWindowsのcommitに余裕ができます。ResidentとRingで予算を分けるには、`--expert-ram-gb 14`に`--resident-budget-gib 6`を追加します。合計が14 GiB以内になる設定です。Residentの確保に失敗すると、その分を解放して全量をRingに割り当てます。

この経路が対応するのは、静的プロファイルで埋めるGPUキャッシュと固定スロットのspeculative/MTP verifierです。実行中のキャッシュ追加・追い出し、batch、複数GPUは未対応です。Verifier確認には`--no-verify-graph`を使います。処理が遅くなり、共有Expertをverifier stream上で処理して順序を安定させます。詳しくは[Windows AMD Ringの説明](WINDOWS_AMD_RING.ja.md)を参照してください。
- **カード向けのビルド済みエンジンがないNVIDIA：** セットアップはビルドツールを入れてコンパイルするか確認します。20〜40分かかります。`--yes`を指定すると実行します。

## 5. ダウンロード中にユーザーへ伝えること

- Hugging Faceから約70 GBをダウンロードします。回線速度によって時間は変わります。100 Mbit/sなら約1.5〜2時間です。途中で止めても、再実行すれば続きから始まります。
- ダウンロード後にPC向けのモデルを準備します。数分かかり、LinuxのAMDではエンジンもビルドします。
- **モデルの初回起動時は、1〜3分ほどPCの動作が遅くなったり、応答しなくなったりします。** Strataは35〜55 GBをRAMに読み込み、GPU用の領域の一部を固定します。正常な動作なので、ウィンドウを閉じずに待つよう伝えてください。
- モデルはPC上で動きます。データは外部へ送られません。

## 6. サーバーを起動する

セットアップは、作成した起動スクリプトの名前を`start script: run-<model>.bat`のように表示します。ファイル名はモデルの種類とサイズから決まり、すべて小文字です。例は`run-iq2_xs`、`run-swift-iq2_xs`、`run-coder-iq1_m`、`run-unsloth-ud-iq4_xs`です。

```text
Windows（PowerShell）:  Start-Process -FilePath ".\run-iq2_xs.bat"  (別ウィンドウで開く)
Linux:                 nohup ./run-iq2_xs.sh > strata-server.out 2>&1 &
```

オプションなしで`START-HERE.bat`または`./setup.sh`を実行すると、インストール済みモデルが起動します。複数ある場合は選択画面が出ます。`--yes`指定時は最初のモデルが対象です。サーバーの準備ができると、ブラウザーで`http://127.0.0.1:8080`が開きます。起動したウィンドウを閉じるとモデルも停止します。

## 7. 動作を確認する

モデルの読み込み後、HTTPサーバーが利用可能な状態です。2回目以降は30〜90秒、初回は数分かかります。

```text
curl http://127.0.0.1:8080/health
```

`{"status": "ok", "model": ..., "max_context": ..., "images": ..., "api_key": ..., "loaded": true, ...}`のような応答が返ります。続けて次を実行してください。

```text
curl http://127.0.0.1:8080/v1/models
curl http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\": \"strata\", \"messages\": [{\"role\": \"user\", \"content\": \"Say hello in five words.\"}], \"max_tokens\": 64, \"reasoning_effort\": \"none\"}"
```

APIキーを設定した場合は、`-H "Authorization: Bearer <key>"`も付けます。準備ができるとサーバーのウィンドウに`ready: http://127.0.0.1:8080/v1`と表示されます。エンジンのログはStrataフォルダーにある`strata-<model>.log`です。

## 8. アプリを接続する

- **ブラウザー：** `http://127.0.0.1:8080`を開きます。Chat、リアルタイムのMonitor、設定とアドレスを表示するAboutがあります。
- **OpenAI互換のアプリやエージェント：** ベースURLを`http://127.0.0.1:8080/v1`にします。APIキーは設定済みの値か任意の値、モデル名も任意です。
- **Anthropic互換アプリ：** `http://127.0.0.1:8080/v1/messages`を使います。
- **Codex CLIなどResponses APIを使うアプリ：** `http://127.0.0.1:8080/v1/responses`を使います。APIはstatelessです。Codexの`config.toml`は[DETAILS.md](DETAILS.md#the-responses-api-and-codex-cli)を参照してください。
- **Claude Code：** `ANTHROPIC_BASE_URL=http://127.0.0.1:8080`を設定し、`ANTHROPIC_MODEL`にはClaude Codeが知っているモデル名を指定します。Strataはモデル名を無視します。`ANTHROPIC_AUTH_TOKEN`には任意の値か、設定済みのAPIキーを使います。
- **思考レベル：** `"reasoning_effort": "none" | "low" | "medium" | "high"`を指定します。初期値はhighです。
- Strataは初期設定では一度に1件のリクエストを処理します。モデル設定の`"parallel": N`またはセットアップの`--parallel N`で、最大N件を同時に処理できます（[BATCHING.md](BATCHING.md)）。APIの詳細は[DETAILS.md](DETAILS.md#using-it)を参照してください。

## 9. 問題が起きたとき

| 表示・症状 | 対応 |
| --- | --- |
| `the NVIDIA driver is too old` | NVIDIA Appまたはnvidia.com/driversからドライバーを更新し、PCを再起動してからセットアップを再実行します |
| ダウンロードやインストールが止まった | 同じセットアップコマンドを再実行します。続きから再開します |
| `port 8080 is already in use` | Strataがすでに動いている可能性があります。`/health`を確認するか、`--port 8081`を使います |
| 動作が非常に遅い、ディスクにアクセスし続ける、または`the engine stopped unexpectedly`と表示される | 空きRAMが不足しています。ほかのプログラムを閉じるか、小さいサイズ（`--setup --model Q2_0`またはIQ2_XS）を選びます |
| VRAMに空きがあるのに、Windowsで`ExpertCache: cudaMalloc(...) failed: out of memory`と表示される | ページファイルが無効、または小さすぎる可能性があります。「システム管理サイズ」に設定して再起動します |
| `prompt ... exceeds the context`と表示される | `--setup --context <大きい値>`を付けてセットアップを再実行します |
| LinuxでAMD GPUが見つからない | amdgpuドライバーがGPUを認識していません。カードとドライバーを確認します。内蔵GPUは非対応です |
| Pythonやビルドツールをインストールできない | エラーに表示されたものをインストールしてからセットアップを再実行します |

詳しくは[TROUBLESHOOTING.md](TROUBLESHOOTING.md)と[DETAILS.mdの一覧](DETAILS.md#troubleshooting)を参照してください。それでも直らない場合は、`strata-<model>.log`とセットアップの出力を集め、https://github.com/Niko1221/Strata/issues に報告するよう案内してください。

## 代わりにMCPサーバーを使う

StrataにはMCPサーバーもあります。AIツールから`strata_status`、`strata_models`、`strata_install`、`strata_start`、`strata_stop`、`strata_logs`を呼び出し、状態確認、インストール、起動、停止を行えます。詳細は[MCP_SERVER.md](MCP_SERVER.md)を参照してください。
