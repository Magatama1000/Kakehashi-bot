"""
クローラーモジュール
Xアカウントのツイートを取得し、Misskeyにノートとして投稿する。
1アカウントペア分の処理を担当。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import sqlite3
import time
import traceback
from pathlib import Path

from twikit import Client

from lib.media import download_and_convert_to_avif, download_media
from lib.misskey_client import MisskeyAPIError, MisskeyClient
from lib.retry import retry_twikit
from lib.text import (
    build_tweet_url,
    process_quote_text,
    process_rt_text,
    process_tweet_text,
    remove_quote_url,
)

logger = logging.getLogger(__name__)

# ノート投稿失敗を何回でスキップするか
NOTE_SKIP_AFTER_FAILURES = 3


class _PinnedInvalidError(Exception):
    """固定ツイートが削除済みまたはパース不能で永続的に取得できないことを示す内部例外"""


# ------------------------------------------------------------------ #
# DB 操作
# ------------------------------------------------------------------ #

def get_db_connection(db_path: str = "data/id_data.db") -> tuple[sqlite3.Connection, sqlite3.Cursor]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tweet_notes (
            tweet_id  INTEGER PRIMARY KEY,
            note_id   TEXT,
            myself    INTEGER DEFAULT 1,
            user      INTEGER DEFAULT 0,
            account   TEXT    DEFAULT ''
        )
    """)
    conn.commit()
    return conn, c


def db_get_note_id(c: sqlite3.Cursor, tweet_id: str) -> str | None:
    c.execute("SELECT note_id FROM tweet_notes WHERE tweet_id = ?", (int(tweet_id),))
    result = c.fetchone()
    return result[0] if result else None


def db_save_mapping(
    c: sqlite3.Cursor,
    conn: sqlite3.Connection,
    tweet_id: str,
    note_id: str,
    myself: int = 1,
    user: int = 0,
    account: str = "",
) -> None:
    c.execute(
        "INSERT OR REPLACE INTO tweet_notes (tweet_id, note_id, myself, user, account) "
        "VALUES (?, ?, ?, ?, ?)",
        (int(tweet_id), note_id, myself, user, account),
    )
    conn.commit()


# ------------------------------------------------------------------ #
# 状態ファイル
# ------------------------------------------------------------------ #

def load_state(screen_name: str) -> dict:
    path = Path(f"data/state_{screen_name}.json")
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(screen_name: str, state: dict) -> None:
    path = Path(f"data/state_{screen_name}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4, ensure_ascii=False)


# ------------------------------------------------------------------ #
# タイムゾーン補完
# ------------------------------------------------------------------ #

def ensure_utc(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# ------------------------------------------------------------------ #
# プロフィール更新
# ------------------------------------------------------------------ #

async def update_profile_if_changed(
    twitter_user,
    mk_client: MisskeyClient,
    state: dict,
    config_profile: dict,
) -> dict:
    profile_image_url = re.sub(
        r"_normal(\.(jpg|png|gif|webp))$", r"_400x400\1",
        getattr(twitter_user, "profile_image_url", "") or ""
    )
    profile_banner_url = getattr(twitter_user, "profile_banner_url", None)

    if config_profile.get("profile", True) and profile_image_url:
        if state.get("profile_image") != profile_image_url:
            logger.info("プロフィール画像を更新します")
            try:
                file_data = await download_and_convert_to_avif(profile_image_url, quality=60)
                drv = mk_client.drive_files_create(file_data, is_sensitive=False)
                mk_client.i_update(avatar_id=drv["id"])
                state["profile_image"] = profile_image_url
                logger.info("プロフィール画像の更新完了")
            except Exception:
                logger.exception("プロフィール画像の更新失敗")

    if config_profile.get("header", True) and profile_banner_url:
        if state.get("profile_banner") != profile_banner_url:
            logger.info("ヘッダー画像を更新します")
            try:
                file_data = await download_and_convert_to_avif(profile_banner_url, quality=60)
                drv = mk_client.drive_files_create(file_data, is_sensitive=False)
                mk_client.i_update(banner_id=drv["id"])
                state["profile_banner"] = profile_banner_url
                logger.info("ヘッダー画像の更新完了")
            except Exception:
                logger.exception("ヘッダー画像の更新失敗")

    return state


# ------------------------------------------------------------------ #
# ノート投稿（リトライ & スキップ付き）
# ------------------------------------------------------------------ #

def _post_note_with_retry(
    mk_client: MisskeyClient,
    tweet_id: str,
    **kwargs,
) -> dict | None:
    """
    Misskey へのノート投稿をリトライし、NOTE_SKIP_AFTER_FAILURES 回失敗したらスキップ。
    スキップ時は None を返す。
    """
    for attempt in range(1, NOTE_SKIP_AFTER_FAILURES + 1):
        try:
            return mk_client.notes_create(**kwargs)
        except MisskeyAPIError as e:
            # 4xx のうち 429 以外は回復見込みなし → 即スキップ
            if e.status_code and 400 <= e.status_code < 500 and e.status_code != 429:
                logger.error(
                    "ノート投稿 %s: API が %d を返しました。スキップします。 body=%s",
                    tweet_id, e.status_code, e.body
                )
                return None
            logger.warning(
                "ノート投稿 %s 失敗 (attempt %d/%d): %s",
                tweet_id, attempt, NOTE_SKIP_AFTER_FAILURES, e
            )
            if attempt < NOTE_SKIP_AFTER_FAILURES:
                time.sleep(5.0 * attempt)
        except Exception as e:
            logger.warning(
                "ノート投稿 %s 失敗 (attempt %d/%d): %s",
                tweet_id, attempt, NOTE_SKIP_AFTER_FAILURES, e
            )
            if attempt < NOTE_SKIP_AFTER_FAILURES:
                time.sleep(5.0 * attempt)

    logger.error("ノート投稿 %s: %d 回失敗。スキップします。", tweet_id, NOTE_SKIP_AFTER_FAILURES)
    return None


# ------------------------------------------------------------------ #
# メイン: 1アカウントペアのクロール
# ------------------------------------------------------------------ #

def _collect_self_replies(
    gotten_new_tweets: list[dict],
    collected_ids: set[str],
    user_id: str,
    last_tweet_time: datetime.datetime,
    screen_name: str,
    max_depth: int = 20,
    seed_tweets: list | None = None,
) -> None:
    """
    ツイートの自己リプライを再帰的に収集する。

    seed_tweets が指定された場合はそこから開始する（浮上した旧ツイートのチェック用）。
    未指定の場合は gotten_new_tweets の全ツイートを走査する。

    検証結果より、自己リプライは replies リストの先頭に来る。
    先頭から連続する自己リプライを収集し、自己リプライ以外が出たら打ち切り。
    get_tweet_by_id を使わず replies だけで完結するため API 呼び出しなし。
    """
    seeds = seed_tweets if seed_tweets is not None else None
    i = 0
    while True:
        if seeds is not None:
            if i >= len(seeds):
                break
            tweet = seeds[i]
        else:
            if i >= len(gotten_new_tweets):
                break
            tweet = gotten_new_tweets[i]["tweet"]
        i += 1
        _follow_self_replies_sync(
            tweet, user_id, last_tweet_time, gotten_new_tweets,
            collected_ids, screen_name, depth=0, max_depth=max_depth,
        )


def _follow_self_replies_sync(
    tweet,
    user_id: str,
    last_tweet_time: datetime.datetime,
    gotten_new_tweets: list[dict],
    collected_ids: set[str],
    screen_name: str,
    depth: int,
    max_depth: int,
) -> None:
    """
    tweet.replies の先頭から連続する自己リプライを収集し、再帰的に辿る（同期・内部関数）。

    検証結果より自己リプライは replies の先頭に来る。
    枝分かれ（複数の自己リプライ）にも対応するため、先頭から順に確認し、
    自己リプライでない要素が出た時点で打ち切る。
    API 呼び出しなしで replies だけで完結する。
    """
    if depth >= max_depth:
        return

    replies = getattr(tweet, "replies", None) or []
    if not replies:
        return

    for reply in replies:
        if getattr(getattr(reply, "user", None), "id", None) != user_id:
            break  # 自己リプライ以外が出たら打ち切り

        if reply.id in collected_ids:
            # 処理済みでも更に先を辿る
            _follow_self_replies_sync(
                reply, user_id, last_tweet_time, gotten_new_tweets,
                collected_ids, screen_name, depth + 1, max_depth,
            )
            continue

        created = ensure_utc(reply.created_at_datetime)
        if created > last_tweet_time:
            gotten_new_tweets.append({"tweet": reply, "created_at": created})
            logger.info(
                "[%s] 自己リプライ（ツリー続き）を追加: %s (depth=%d)",
                screen_name, reply.id, depth + 1,
            )
        collected_ids.add(reply.id)

        # さらに先の自己リプライを辿る（API呼び出しなし）
        _follow_self_replies_sync(
            reply, user_id, last_tweet_time, gotten_new_tweets,
            collected_ids, screen_name, depth + 1, max_depth,
        )


async def crawl_account(
    account: dict,
    twitter_client: Client,
    config: dict,
    url_cleaner=None,
) -> None:
    screen_name: str = account["twitter_screen_name"]
    misskey_url: str = account["misskey_url"]
    misskey_token: str = account["misskey_token"]

    config_note: dict = config.get("note", {})
    config_media: dict = config.get("media", {})
    config_nsfw: dict = config.get("nsfw", {})
    config_profile: dict = config.get("profile", {})

    note_duration: int = config_note.get("note_duration", 10)
    do_retweet: bool = config_note.get("retweet", True)
    visibility: str = config_note.get("visibility", "public")
    localonly: bool = config_note.get("localonly", False)
    mfm_mention: bool = config_note.get("mfm_mention", True)
    mfm_tweeturl: bool = config_note.get("mfm_tweeturl", True)

    logger.info("=== [%s] クロール開始 ===", screen_name)

    mk_client = MisskeyClient(misskey_url, misskey_token)
    state = load_state(screen_name)

    # ユーザー情報取得
    twitter_user = await retry_twikit(
        twitter_client.get_user_by_screen_name,
        screen_name,
        label=f"get_user_by_screen_name({screen_name})",
    )
    user_id = twitter_user.id
    logger.info("[%s] user_id=%s", screen_name, user_id)

    # 初回: last_tweet_time を現在の最新ツイートに合わせる
    if not state.get("last_tweet_time"):
        tweets_init = await retry_twikit(
            twitter_client.get_user_tweets,
            user_id, "Tweets", count=1,
            label=f"get_user_tweets({screen_name}) init",
        )
        if tweets_init:
            state["last_tweet_time"] = ensure_utc(
                tweets_init[0].created_at_datetime
            ).isoformat()
        else:
            state["last_tweet_time"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
        save_state(screen_name, state)
        logger.info("[%s] 初回実行: last_tweet_time を初期化しました", screen_name)
        return

    last_tweet_time = datetime.datetime.fromisoformat(state["last_tweet_time"])
    last_tweet_time = ensure_utc(last_tweet_time)

    # プロフィール更新チェック
    logger.info("[%s] プロフィールを確認します", screen_name)
    state = await update_profile_if_changed(twitter_user, mk_client, state, config_profile)
    save_state(screen_name, state)

    # 新規ツイート収集
    logger.info("[%s] 新規ツイートを取得します (last: %s)", screen_name, last_tweet_time.isoformat())
    gotten_new_tweets: list[dict] = []
    reach_latest = False

    new_tweet_page = await retry_twikit(
        twitter_client.get_user_tweets,
        user_id, "Tweets", count=20,
        label=f"get_user_tweets({screen_name})",
    )

    # 収集済みツイートIDセット（重複防止）
    collected_ids: set[str] = set()

    # タイムラインで「古いツイートが浮上してきた」もの（処理済みツリー元候補）
    surfaced_old_tweets: list = []

    # ページング上限（全部旧ツイートでも無限ループしないよう上限を設ける）
    MAX_PAGES = 20
    page_count = 0

    while page_count < MAX_PAGES:
        page_count += 1
        if not new_tweet_page or len(new_tweet_page) == 0:
            logger.debug("[%s] ツイートページが空", screen_name)
            break

        has_new = False
        for tweet in new_tweet_page:
            created = ensure_utc(tweet.created_at_datetime)
            if created > last_tweet_time:
                has_new = True
                if tweet.id not in collected_ids:
                    gotten_new_tweets.append({"tweet": tweet, "created_at": created})
                    collected_ids.add(tweet.id)
            else:
                # last_tweet_time 以前のツイートが出現 → ツリー元として浮上した可能性がある。
                # reach_latest にはしない（新規ツイートがまだ後ろにある可能性）。
                # ただし浮上した旧ツイートは自己リプライチェック対象として保持する。
                if tweet.id not in collected_ids and len(surfaced_old_tweets) < 5:
                    surfaced_old_tweets.append(tweet)

        # 1ページに新規ツイートが1件もなければ終了
        if not has_new:
            logger.debug("[%s] 新規ツイートなし、ページング終了", screen_name)
            break

        try:
            await asyncio.sleep(1)
            new_tweet_page = await retry_twikit(
                new_tweet_page.next,
                label=f"tweet_page.next({screen_name})",
            )
            if not new_tweet_page:
                logger.debug("[%s] 次ページが空（None または []）、ページング終了", screen_name)
                break
        except Exception:
            logger.exception("[%s] 次ページ取得失敗", screen_name)
            break

    # ツリー（自己リプライ）の収集 その1:
    # タイムラインで取得した新規ツイートの replies を辿る
    _collect_self_replies(
        gotten_new_tweets, collected_ids, user_id, last_tweet_time, screen_name,
    )

    # ツリー（自己リプライ）の収集 その2:
    # 過去処理済みのツリー元が浮上してきた場合、その replies に新規自己リプライがないか確認
    # （タイムラインに自己リプライ自体は出てこないが、ツリー元が浮上することで検知できる）
    for old_tweet in surfaced_old_tweets:
        if old_tweet.id in collected_ids:
            continue
        logger.debug(
            "[%s] 浮上した旧ツイートの自己リプライを確認: %s", screen_name, old_tweet.id
        )
        _collect_self_replies(
            gotten_new_tweets, collected_ids, user_id, last_tweet_time, screen_name,
            seed_tweets=[old_tweet],
        )

    # 固定ツイートチェック
    logger.info("[%s] 固定ツイートを確認します", screen_name)
    pinned_ids = getattr(twitter_user, "pinned_tweet_ids", None) or []
    pinned_updated = False
    pinned_tweet_id: str | None = None

    if not pinned_ids:
        if state.get("pinned", "") != "":
            logger.info("[%s] 固定ツイートを解除します", screen_name)
            old_note = state.get("pinned_note", "")
            if old_note:
                try:
                    mk_client.i_unpin(old_note)
                except Exception:
                    logger.exception("[%s] ピン解除失敗", screen_name)
            state["pinned"] = ""
            state["pinned_note"] = ""
    else:
        current_pinned_id = str(pinned_ids[0])
        if current_pinned_id != state.get("pinned", ""):
            logger.info("[%s] 固定ツイートの変更を検出: %s", screen_name, current_pinned_id)

            # 削除済みツイートが pinned_tweet_ids に残り続けるケースがある。
            # invalid_pinned_ids に記録して以降はスキップする。
            invalid_pinned = set(state.get("invalid_pinned_ids", []))
            if current_pinned_id in invalid_pinned:
                logger.info(
                    "[%s] 固定ツイート(%s)は以前取得失敗済みのためスキップします",
                    screen_name, current_pinned_id,
                )
            else:
                # 取得を試みる（通信エラーはリトライ、パース失敗・削除済みはスキップ）
                try:
                    pinned = await retry_twikit(
                        twitter_client.get_tweet_by_id,
                        current_pinned_id,
                        label=f"get_tweet_by_id(pinned={current_pinned_id})",
                    )
                    if pinned is None:
                        # None はパース失敗 or 削除済み → invalid 扱い
                        raise _PinnedInvalidError("get_tweet_by_id returned None")

                    # 取得成功 → 通常処理
                    pinned_tweet_id = current_pinned_id
                    pinned_updated = True
                    state["pinned"] = current_pinned_id

                    pinned_created = ensure_utc(pinned.created_at_datetime)
                    if pinned_created > last_tweet_time:
                        already = any(t["tweet"].id == pinned.id for t in gotten_new_tweets)
                        if not already:
                            gotten_new_tweets.append({"tweet": pinned, "created_at": pinned_created})
                            logger.info("[%s] 固定ツイートをリストに追加: %s", screen_name, pinned.id)

                except _PinnedInvalidError as e:
                    # パース失敗・削除済み → invalid_pinned_ids に永続記録してスキップ
                    logger.warning(
                        "[%s] 固定ツイート(%s)は削除済みまたは取得不能です。以降スキップします: %s",
                        screen_name, current_pinned_id, e,
                    )
                    invalid_pinned.add(current_pinned_id)
                    state["invalid_pinned_ids"] = list(invalid_pinned)
                    # state["pinned"] は更新しない（次回も検出されるが invalid でスキップ）

                except Exception as e:
                    # 通信エラー等の一時的な失敗 → 今回はスキップ、次回リトライ
                    logger.warning(
                        "[%s] 固定ツイート(%s)の取得に失敗しました（一時的なエラーの可能性）。次回再試行します: %s",
                        screen_name, current_pinned_id, e,
                    )
                    # state["pinned"] を更新しないことで次回も再試行される

    # 古い順に並び替え
    gotten_new_tweets.sort(key=lambda x: x["created_at"])
    logger.info("[%s] 新規ツイート数: %d 件", screen_name, len(gotten_new_tweets))

    if not gotten_new_tweets:
        logger.info("[%s] 新規ツイートなし", screen_name)
        save_state(screen_name, state)
        return

    # DB 接続
    conn, c = get_db_connection()

    try:
        for idx, item in enumerate(gotten_new_tweets):
            if idx > 0:
                logger.debug("[%s] ノート間隔待機: %ds", screen_name, note_duration)
                await asyncio.sleep(note_duration)

            tweet = item["tweet"]
            tweet_id = tweet.id
            logger.info(
                "[%s] ツイート処理 [%d/%d]: %s",
                screen_name, idx + 1, len(gotten_new_tweets), tweet_id
            )

            try:
                await _process_single_tweet(
                    tweet=tweet,
                    tweet_id=tweet_id,
                    item=item,
                    screen_name=screen_name,
                    twitter_client=twitter_client,
                    mk_client=mk_client,
                    c=c,
                    conn=conn,
                    state=state,
                    config_note=config_note,
                    config_media=config_media,
                    config_nsfw=config_nsfw,
                    do_retweet=do_retweet,
                    visibility=visibility,
                    localonly=localonly,
                    mfm_mention=mfm_mention,
                    mfm_tweeturl=mfm_tweeturl,
                    url_cleaner=url_cleaner,
                )
            except Exception:
                logger.exception(
                    "[%s] ツイート処理中に予期せぬエラー: %s", screen_name, tweet_id
                )

        # 固定ピン更新
        if pinned_updated and pinned_tweet_id:
            logger.info("[%s] 固定ノートを更新します", screen_name)
            pinned_note_id = db_get_note_id(c, pinned_tweet_id)
            if pinned_note_id:
                old_pinned_note = state.get("pinned_note", "")
                if old_pinned_note:
                    try:
                        mk_client.i_unpin(old_pinned_note)
                    except Exception:
                        logger.exception("[%s] 旧ピン解除失敗", screen_name)
                try:
                    mk_client.i_pin(pinned_note_id)
                    state["pinned_note"] = pinned_note_id
                    logger.info("[%s] 固定ノート更新完了: %s", screen_name, pinned_note_id)
                except Exception:
                    logger.exception("[%s] 固定ノート設定失敗", screen_name)
            else:
                logger.warning("[%s] 固定ツイートの対応ノートが見つかりませんでした", screen_name)

    finally:
        save_state(screen_name, state)
        conn.close()
        logger.info("=== [%s] クロール完了 ===", screen_name)


# ------------------------------------------------------------------ #
# 個別ツイート処理（内部関数）
# ------------------------------------------------------------------ #

async def _process_single_tweet(
    tweet,
    tweet_id: str,
    item: dict,
    screen_name: str,
    twitter_client: Client,
    mk_client: MisskeyClient,
    c: sqlite3.Cursor,
    conn: sqlite3.Connection,
    state: dict,
    config_note: dict,
    config_media: dict,
    config_nsfw: dict,
    do_retweet: bool,
    visibility: str,
    localonly: bool,
    mfm_mention: bool,
    mfm_tweeturl: bool,
    url_cleaner=None,
) -> None:
    """1ツイートを処理してMisskeyにノートする"""

    file_ids: list[str] | None = None
    reply_id: str | None = None
    renote_id: str | None = None
    tweet_text: str = ""
    urls: list | None = None

    retweeted = getattr(tweet, "retweeted_tweet", None)

    # ---- 通常 RT ----
    if retweeted is not None and do_retweet:
        logger.info("  RT を検出: %s", retweeted.id)
        rt_note_id = db_get_note_id(c, retweeted.id)

        if rt_note_id:
            # 対応ノートあり → リノート
            logger.info("  対応ノートあり → リノート: %s", rt_note_id)
            result = _post_note_with_retry(
                mk_client,
                tweet_id,
                renote_id=rt_note_id,
                visibility=visibility,
                local_only=localonly,
            )
            if result:
                note_id = result["createdNote"]["id"]
                db_save_mapping(c, conn, tweet_id, note_id, account=screen_name)
            state["last_tweet_time"] = item["created_at"].isoformat()
            return

        else:
            # 対応ノートなし → テキストとして投稿
            logger.info("  対応ノートなし → テキストRT")
            rt_screen_name = retweeted.user.screen_name
            rt_urls = getattr(retweeted, "urls", None)
            tweet_text = process_rt_text(
                rt_screen_name=rt_screen_name,
                rt_text=retweeted.full_text or retweeted.text or "",
                rt_urls=rt_urls,
                mfm_mention=mfm_mention,
                url_cleaner=url_cleaner,
            )
            file_ids, tweet_text = await download_media(
                media=getattr(retweeted, "media", None),
                misskey_client=mk_client,
                text=tweet_text,
                tweet_id=retweeted.id,
                config_media=config_media,
                config_nsfw=config_nsfw,
            )
            # RT の場合は末尾リンクを元ツイートURLで付与
            from lib.text import build_tweet_url, append_tweet_link
            rt_url = build_tweet_url(rt_screen_name, retweeted.id)
            if mfm_tweeturl:
                tweet_text = append_tweet_link(tweet_text, rt_url, suppress_preview=True)
            else:
                tweet_text = append_tweet_link(tweet_text, rt_url, suppress_preview=False)

    else:
        # ---- 通常ツイート ----
        tweet_text = tweet.full_text or tweet.text or ""
        urls = getattr(tweet, "urls", None)

        # メディア処理
        logger.info("  メディアを処理します")
        file_ids, tweet_text = await download_media(
            media=getattr(tweet, "media", None),
            misskey_client=mk_client,
            text=tweet_text,
            tweet_id=tweet_id,
            config_media=config_media,
            config_nsfw=config_nsfw,
        )

        # 返信チェック
        in_reply_to = getattr(tweet, "in_reply_to", None)
        if in_reply_to:
            logger.info("  返信先を検出: %s", in_reply_to)
            rp_note_id = db_get_note_id(c, in_reply_to)
            if rp_note_id:
                reply_id = rp_note_id
                logger.info("  返信先ノート: %s", reply_id)
            else:
                tweet_text = (
                    f"Reply to : https://x.com/x/status/{in_reply_to}\n\n{tweet_text}"
                )
                logger.info("  返信先ノートが見つからないためURLを埋め込みます")

        # 引用チェック
        quote_tweet = getattr(tweet, "quote", None)
        if quote_tweet:
            logger.info("  引用ツイートを検出: %s", quote_tweet.id)
            tweet_text = remove_quote_url(tweet_text, quote_tweet.id)

            qt_note_id = db_get_note_id(c, quote_tweet.id)
            if qt_note_id:
                renote_id = qt_note_id
                logger.info("  引用先ノート: %s", renote_id)
            else:
                logger.info("  引用先ノートが見つからないためテキストに埋め込みます")
                try:
                    qt_screen_name = quote_tweet.user.screen_name
                    qt_text = quote_tweet.full_text or quote_tweet.text or ""
                    qt_urls = getattr(quote_tweet, "urls", None)

                    qt_media_ids, qt_text_processed = await download_media(
                        media=getattr(quote_tweet, "media", None),
                        misskey_client=mk_client,
                        text=qt_text,
                        tweet_id=quote_tweet.id,
                        config_media=config_media,
                        config_nsfw=config_nsfw,
                    )
                    qt_suffix = process_quote_text(
                        qt_screen_name=qt_screen_name,
                        qt_text=qt_text_processed,
                        qt_urls=qt_urls,
                        mfm_mention=mfm_mention,
                        url_cleaner=url_cleaner,
                    )
                    tweet_text = f"{tweet_text}\n\n{qt_suffix}"

                    # 引用元メディアを合成
                    if qt_media_ids:
                        file_ids = list(file_ids or []) + qt_media_ids

                except Exception:
                    logger.exception("  引用先処理失敗")
                    tweet_text += "\n\nQT: (引用先の取得に失敗しました)"

        # テキスト最終処理（URL展開 / MFM / 末尾リンク）
        tweet_text = process_tweet_text(
            text=tweet_text,
            screen_name=screen_name,
            tweet_id=tweet_id,
            urls=urls,
            mfm_mention=mfm_mention,
            mfm_tweeturl=mfm_tweeturl,
            url_cleaner=url_cleaner,
        )

    # ---- Misskey にノート投稿 ----
    logger.info(
        "  ノートを作成します (file_ids=%s reply=%s renote=%s text=%s...)",
        file_ids, reply_id, renote_id, repr(tweet_text[:30])
    )

    result = _post_note_with_retry(
        mk_client,
        tweet_id,
        text=tweet_text,
        visibility=visibility,
        local_only=localonly,
        file_ids=file_ids,
        reply_id=reply_id,
        renote_id=renote_id,
    )

    if result is None:
        logger.warning("  ノート投稿をスキップしました: tweet_id=%s", tweet_id)
        # スキップしても last_tweet_time は更新して二度と処理しない
        state["last_tweet_time"] = item["created_at"].isoformat()
        return

    note_id = result["createdNote"]["id"]
    logger.info("  ノート作成完了: %s", note_id)

    # DB 保存
    db_save_mapping(c, conn, tweet_id, note_id, myself=1, account=screen_name)
    if retweeted is not None and retweeted.id != tweet_id:
        db_save_mapping(c, conn, retweeted.id, note_id, myself=0, account=screen_name)

    state["last_tweet_time"] = item["created_at"].isoformat()