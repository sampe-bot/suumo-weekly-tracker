# suumo-weekly-tracker

指定した検索条件に合う物件をSUUMOから週次でクロールし、同一住戸の名寄せ・坪単価/平米単価の算出・前回差分検知を行ったうえで、Googleスプレッドシートの更新とSlackへのサマリー通知を自動実行するツールです。

## ディレクトリ構成

```text
suumo-weekly-tracker/
├── pyproject.toml                              # uv プロジェクト・依存関係定義
├── .python-version                             # Pythonバージョン指定 (3.12)
├── .env.example                                # 環境変数テンプレート
├── .gitignore                                  # Git除外設定 (.env, service_account.json 等)
├── src/
│   └── suumo_weekly_tracker/
│       ├── __init__.py
│       └── main.py                             # クローリング・集計・シート更新・Slack通知本体
└── templates/
    └── com.user.suumo-tracker.plist.example    # macOS launchd 週次実行用テンプレート
```

## セットアップ手順

### 1. 依存パッケージのインストール (`uv`)

```bash
cd suumo-weekly-tracker
uv sync
```

### 2. Googleスプレッドシート & サービスアカウントの準備

1. Google Cloud Console でプロジェクトを作成し、**Google Sheets API** を有効化します。
2. サービスアカウントを作成して JSON キーをダウンロードし、プロジェクトルートに `service_account.json` として配置します（`.gitignore` に登録済みです）。
3. データを出力したい Google スプレッドシートを新規作成し、サービスアカウントのメールアドレス（`xxx@xxx.iam.gserviceaccount.com`）に「編集者」権限で共有します。

### 3. 環境変数の設定

`.env.example` をコピーして `.env` を作成し、各種設定値を記入します。

```bash
cp .env.example .env
```

- `SUUMO_SEARCH_URL`: 検索条件を適用したPC版SUUMO一覧ページのURL
- `SPREADSHEET_ID`: スプレッドシートURLの `/d/` と `/edit` の間のID
- `GOOGLE_CREDENTIALS_PATH`: `service_account.json` のパス
- `SLACK_WEBHOOK_URL`: Slack Incoming Webhook のURL

### 4. 手動実行テスト

```bash
uv run suumo-tracker
```

実行後、スプレッドシートに `Summary_Ranking` と `Raw_History` の2つのタブが生成され、Slackにレポートが通知されることを確認してください。

## 週次自動実行の設定（macOS `launchd`）

自宅ネットワークから毎週日曜 8:00 に自動実行する場合、`templates/com.user.suumo-tracker.plist.example` のパスを環境に合わせて書き換え、`~/Library/LaunchAgents/` に配置してロードします。

```bash
cp templates/com.user.suumo-tracker.plist.example ~/Library/LaunchAgents/com.user.suumo-tracker.plist
# ※ エディタで /REPLACE/WITH/PATH/TO/... を実際のパス（`which uv` や `pwd` の結果）に置換してください
launchctl load ~/Library/LaunchAgents/com.user.suumo-tracker.plist
```
