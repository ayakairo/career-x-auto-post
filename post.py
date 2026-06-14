#!/usr/bin/env python3
"""
フォーエバー（キャリアコーチ）X自動投稿スクリプト
投稿ストックファイルを直接読んで次の未投稿を投稿し、[x] に更新する。
403（コンテンツ拒否）の場合は自動スキップして次の投稿を試みる。
CloudflareのHTMLブロック（一時的）は通常の失敗として扱い、次回再試行。
"""

import os
import re
import sys
import tweepy
from datetime import datetime, timezone, timedelta
from pathlib import Path

API_KEY = os.environ["X_API_KEY"]
API_SECRET = os.environ["X_API_SECRET"]
ACCESS_TOKEN = os.environ["X_ACCESS_TOKEN"]
ACCESS_TOKEN_SECRET = os.environ["X_ACCESS_TOKEN_SECRET"]

# 優先順位:
# 1. 環境変数 POSTS_FILE（GitHub Actions から指定）
# 2. ローカル開発: プロジェクトルートの投稿ストック
# 3. フォールバック: リポジトリ内の posts.md
if os.environ.get("POSTS_FILE"):
    POSTS_FILE = Path(os.environ["POSTS_FILE"])
else:
    _project_path = Path(__file__).parent.parent.parent / "in_フォーエバー/X/投稿ストック.md"
    _repo_path = Path(__file__).parent / "posts.md"
    POSTS_FILE = _project_path if _project_path.exists() else _repo_path

_rejected_log_project = Path(__file__).parent.parent.parent / "sys_scripts/career-x-auto-post/rejected_log.md"
_rejected_log_repo = Path(__file__).parent / "rejected_log.md"
REJECTED_LOG = _rejected_log_project if _rejected_log_project.exists() else _rejected_log_repo

HASHTAGS = ""
MAX_SKIP = 3  # 連続スキップ上限（これを超えたら一時エラーの可能性として止める）
JST = timezone(timedelta(hours=9))


def get_client():
    return tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )


def parse_checklist(text: str) -> list[dict]:
    """チェックリストから投稿順・投稿済みを取得"""
    rows = []
    for line in text.splitlines():
        m = re.match(r"- \[(x| )\] (\d+)\. (#[^\s]+)", line)
        if m:
            rows.append({
                "done": m.group(1) == "x",
                "order": int(m.group(2)),
                "num": m.group(3),
            })
    return rows


def parse_post_content(text: str, num: str) -> str:
    """素材プールから num（例: #X-1）の本文を取得。【元記事】行は除去する"""
    pattern = rf"\*\*{re.escape(num)}\*\*[^\n]*\n(.*?)(?=\n---|\n\*\*#|\Z)"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return ""
    content = m.group(1).strip()
    content = re.sub(r"\n?【元記事】.*$", "", content, flags=re.MULTILINE).strip()
    return content


def mark_done(posts_path: Path, order: int):
    """投稿済みとして該当行の [ ] を [x] に更新"""
    text = posts_path.read_text(encoding="utf-8")
    updated = re.sub(
        rf"(- \[ \] {order}\. )",
        lambda m: m.group(0).replace("[ ]", "[x]"),
        text,
        count=1,
    )
    posts_path.write_text(updated, encoding="utf-8")


def write_rejected_log(num: str, content: str, error_body: str):
    """拒否された投稿をrejected_log.mdに追記する"""
    jst_now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    entry = f"""
## {jst_now} | {num}（自動スキップ）

**投稿内容：**
```
{content}
```

**エラー：** 403 Forbidden（X API コンテンツ拒否）

**エラー詳細：**
```
{error_body[:300]}
```

**原因仮説：**
- 重複ツイート（同じ内容が過去に投稿済みの可能性）
- コンテンツフィルター（固有名詞・特定表現が引っかかった可能性）

**対処：**
- 自動スキップ済み（`[x]`マーク済み）
- 再投稿する場合：内容を修正して`投稿ストック.md`末尾に新番号で追加 → `posts.md`を更新してpush

---
"""
    if REJECTED_LOG.exists():
        existing = REJECTED_LOG.read_text(encoding="utf-8")
        marker = "<!-- 以下、自動追記エリア（post.pyが書き込む） -->"
        if marker in existing:
            updated = existing.replace(marker, marker + entry)
        else:
            updated = existing + entry
    else:
        updated = f"# 拒否投稿ログ\n{entry}"
    REJECTED_LOG.write_text(updated, encoding="utf-8")
    print(f"📝 rejected_log.mdに記録しました")


def is_duplicate_content(e) -> bool:
    """Xが「重複ツイート」として拒否しているか判定。
    重複＝実は投稿済みなので「投稿成功」と同じ扱いにする。
    """
    if not hasattr(e, "response") or e.response is None:
        return False
    if e.response.status_code != 403:
        return False
    return "duplicate" in e.response.text.lower()


def is_content_rejection(e) -> bool:
    """X APIがコンテンツを恒久的に拒否しているか判定。
    - JSON レスポンス → X API 本体の拒否（コンテンツ問題）→ スキップ対象
    - HTML レスポンス → Cloudflare等の一時ブロック → スキップしない（次回再試行）
    - 重複エラーは先に is_duplicate_content で処理するのでここでは除外
    """
    if not hasattr(e, "response") or e.response is None:
        return False
    if e.response.status_code != 403:
        return False
    if is_duplicate_content(e):
        return False
    return e.response.text.strip().startswith("{")


def main():
    if not POSTS_FILE.exists():
        print(f"❌ 投稿ストックが見つかりません: {POSTS_FILE}")
        sys.exit(1)

    print(f"📂 投稿ストック: {POSTS_FILE}")
    text = POSTS_FILE.read_text(encoding="utf-8")
    rows = parse_checklist(text)
    pending = [r for r in rows if not r["done"]]

    if not pending:
        print("✅ 全投稿完了！新しい投稿を追加してください。")
        sys.exit(0)

    client = get_client()
    skipped = 0

    for post in pending:
        if skipped >= MAX_SKIP:
            print(f"❌ {MAX_SKIP}件連続スキップ。一時的な問題の可能性があるため終了します。")
            sys.exit(1)

        content = parse_post_content(text, post["num"])
        if not content:
            print(f"⚠️  {post['num']} の本文が見つかりません。スキップします。")
            mark_done(POSTS_FILE, post["order"])
            text = POSTS_FILE.read_text(encoding="utf-8")
            skipped += 1
            continue

        tweet_text = content + HASHTAGS
        print(f"投稿試行: {post['num']} (順{post['order']})")
        print(f"文字数: {len(tweet_text)}")

        try:
            response = client.create_tweet(text=tweet_text)
        except Exception as e:
            print(f"❌ エラー詳細: {type(e).__name__}: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"   status: {e.response.status_code}")
                print(f"   body: {e.response.text[:300]}")

            if is_duplicate_content(e):
                print(f"✅ 重複エラー → すでに投稿済みと判定。{post['num']} を投稿済みにして次へ。")
                mark_done(POSTS_FILE, post["order"])
                text = POSTS_FILE.read_text(encoding="utf-8")
                sys.exit(0)
            elif is_content_rejection(e):
                print(f"⚠️  コンテンツ拒否（X API）と判定。{post['num']} を自動スキップして次へ。")
                error_body = e.response.text if hasattr(e, "response") and e.response else str(e)
                write_rejected_log(post["num"], content, error_body)
                mark_done(POSTS_FILE, post["order"])
                text = POSTS_FILE.read_text(encoding="utf-8")
                skipped += 1
                continue
            else:
                print("   → 一時的なエラーの可能性（Cloudflare等）。次回のスケジュール実行で再試行します。")
                sys.exit(1)

        if response.data:
            mark_done(POSTS_FILE, post["order"])
            print(f"✅ 投稿完了: {post['num']}")
            print(f"   Tweet ID: {response.data['id']}")
            print(f"   {content[:50]}...")
            sys.exit(0)
        else:
            print("❌ 投稿に失敗しました")
            sys.exit(1)

    print("✅ 全投稿完了！新しい投稿を追加してください。")
    sys.exit(0)


if __name__ == "__main__":
    main()
