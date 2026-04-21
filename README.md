# Kakehashi-bot

X(Twitter)の指定アカウントの投稿を Misskey に自動転載するボットです。

## 特徴
- [twikit (フォーク)](https://github.com/Magatama1000/twikit) の採用により、事実上個人利用が困難になったXのAPIキー不要で利用できます。
- Misskeyの機能を利用し、Xのポストをなるべく再現します(引用, ツリー, 固定など)。
- X内の@ポストはXへのMFMリンクに変換します。

## 注意事項
> [!WARNING]  
> Xの非公式API(Web版のAPI)を利用する[twikit (フォーク)](https://github.com/Magatama1000/twikit) を採用しています。**自己責任下**でご利用ください。  
> そのため、メインアカウントの利用はなるべく避けることを強く推奨します。

- OSは、Windows 11, Ubuntu 24.04, Debian 12 以降およびその派生のみ正式サポートしております。  
Issueは受けますが、対応が困難な可能性があります。 
- ご利用になるMisskeyインスタンスの規約に沿い、過剰投稿を避け、アカウントをbot指定してください。NSFWに注意してください。
- ノート投稿が連続3回失敗したツイートはスキップされます。
- クロール間隔は120~300秒以上を推奨します。

## 必要環境

- Python 3.11 以上 (3.13 / 3.14 推奨)
- [uv](https://docs.astral.sh/uv/) (パッケージ管理)
- [FFmpeg](https://ffmpeg.org/) (動画・GIF処理)

---

## 標準的なセットアップ方法

### 1. uv のインストール

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Kakehshi-botのダウンロード

インストールしたいディレクトリ(例: C:\Kakehashi-bot)でターミナルを開いてください。
```bash
# ブラウザでのダウンロードでも可
git clone https://github.com/Magatama1000/Kakehashi-bot.git

cd Kakehashi-bot
```

### 3. 依存パッケージのインストール

```bash
uv sync
```

### 4. FFmpeg のインストール

Pathの通ったFFmpegが必要です。ない場合は以下の手順でインストールしてください。

```powershell
# Windows (winget)
winget install Gyan.FFmpeg
```

```bash
# Ubuntu / Debian
sudo apt install ffmpeg

# macOS (Homebrew)
brew install ffmpeg
```

### 5. 認証情報の設定

```bash
uv run python login.py
```

X (Twitter) の認証と Misskey の MiAuth 認証を対話形式で設定します。  
`auth.json` が生成されます(認証情報が含まれているのでお取扱いにご注意ください)。

#### X (Twitter) の認証方法について

> [!WARNING]  
> メインアカウントの利用はお避けください。  
> 近年認証が厳格化されています。基本的にはCookieをブラウザから取得する方法を利用してください。  
> アカウント新規登録直後のCookieを利用すると制限を受けにくい可能性があります (要検証)。

login.py 起動時に以下の2つから選択できます。  

**方法1: Cookie手動入力**  
ブラウザの開発者ツールから Cookie を直接コピーして入力します。

| Cookie名 | 場所 | 説明 |
|----------|------|------|
| `auth_token` | F12 → Application → Cookies → https://x.com | 約40文字 |
| `ct0` | 同上 | 約160文字 |

**方法2: twikitによる直接ログイン (非推奨)**  
ユーザー名・パスワードを入力してログインします。  
失敗した場合、自動的に方法1への切り替えを提案します。

### 6. 設定の編集

`config_default.toml`をコピーして`config.toml`にリネームしてください。  
`config.toml` を編集します(各項目にコメントあり)。

特にNSFW周りに注意してください。掲載画像に少しでも対象が含まれる場合は、すべてNSFWとして投稿することを推奨します(デフォルト)。

### 7. 起動テスト

```bash
uv run python main.py
```

---

## 自動起動の設定

### Windows — タスクスケジューラ

ログオン不要でバックグラウンド常駐させる場合は**タスクスケジューラ**を使います。

#### 設定手順

1. **タスクスケジューラ**を開く(スタートメニューで検索)
2. 右ペインの「**タスクの作成**」をクリック
3. 各タブを以下のように設定する：

**全般タブ**
- 名前: 任意(例: `Kakehashi-bot`)
- 「ユーザーがログオンしているかどうかにかかわらず実行する」を選択

**トリガータブ**  
「新規」→「タスクの開始: スタートアップ時」

**操作タブ**  
「新規」→ 以下を入力：

| 項目 | 値 |
|------|-----|
| プログラム/スクリプト | `C:\Users\<ユーザー名>\.local\bin\uv.exe` |
| 引数の追加 | `run python main.py` |
| 開始(オプション) | インストール先(例: `C:\Kakehashi-bot`) |

**条件タブ**
- すべてのチェックを外す

**設定タブ**
- 「タスクを停止するまでの時間」のチェックを外す
- 「タスクが失敗した場合の再起動の間隔」: 5分(任意)
- 「再起動試行の最大数」: 10回(任意)

4. OK → パスワード入力で完了

#### ログの確認・操作

```powershell
# ログをリアルタイムで確認
Get-Content kakehashi-bot.log -Wait -Tail 50

# 手動起動
schtasks /Run /TN "Kakehashi-bot" #設定名

# 手動停止
schtasks /End /TN "Kakehashi-bot" #設定名
```

---

### Linux — systemd

systemd サービスとして登録することでシステム起動時に自動起動します。

#### サービスファイルの作成

```bash
sudo nano /etc/systemd/system/Kakehashi-bot.service
```

以下を記述(`youruser` とパスは環境に合わせて変更)：

```ini
[Unit]
Description=Kakehashi-bot - X to Misskey crosspost bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/Kakehashi-bot
ExecStart=/home/youruser/.local/bin/uv run python main.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

#### 有効化と起動

```bash
# サービスを登録・有効化
sudo systemctl daemon-reload
sudo systemctl enable Kakehashi-bot
sudo systemctl start Kakehashi-bot

# 状態確認
sudo systemctl status Kakehashi-bot

# ログの確認 (リアルタイム)
sudo journalctl -u Kakehashi-bot -f
```

#### 手動での停止・再起動

```bash
sudo systemctl stop Kakehashi-bot
sudo systemctl restart Kakehashi-bot
```

---

## ファイル構成

```
Kakehashi-bot/
├── main.py               # エントリーポイント・常駐ループ
├── login.py              # 認証セットアップ(対話式)
├── config.toml           # 動作設定
├── pyproject.toml        # uv 用パッケージ定義
├── auth.json             # 認証情報(自動生成・gitignore済み)
├── lib/
│   ├── crawler.py        # クロール・投稿ロジック
│   ├── ffmpeg.py         # FFmpeg 非同期ラッパー(進捗ログ付き)
│   ├── logger_setup.py   # ロギング設定
│   ├── media.py          # メディア処理(非同期)
│   ├── misskey_client.py # Misskey API 独自実装クライアント
│   ├── retry.py          # リトライユーティリティ
│   └── text.py           # テキスト処理(URL展開・MFM変換)
└── data/                 # 自動生成・gitignore済み
    ├── state_{name}.json # アカウントごとの処理状態
    └── id_data.db        # tweet_id ↔ note_id マッピング(SQLite)
```

## config.toml 主な設定

| セクション | キー | 説明 |
|-----------|------|------|
| `[crawl]` | `crawl_duration` | クロール間隔(秒) |
| `[note]` | `retweet` | RTを転載するか |
| `[note]` | `visibility` | 公開範囲 public/home/followers |
| `[note]` | `mfm_mention` | @メンションをMFMリンクに変換 |
| `[media]` | `video_encode` | 動画エンコード copy/x265 |
| `[media]` | `pic_encode_avif` | 静止画をAVIF変換 |
| `[nsfw]` | `nsfw_forced` | 全メディアに強制NSFW |
| `[nsfw]` | `nsfw_forced_video` | 動画・GIFに強制NSFW |
| `[log]` | `level` | DEBUG/INFO/WARNING/ERROR |
| `[log]` | `file` | ログファイルパス |

## License

[MIT License](LICENSE)