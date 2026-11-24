"""
メディア処理モジュール（非同期版）
inspect_video 結果に基づく twikit 実際の構造:
  - Photo.media_url     → 画像URL（media_url_https は公開属性なし）
  - Video/AnimatedGif.video_info["variants"] → dict のリスト（オブジェクトではない）
    各 variant: {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://..."}
    m3u8 は bitrate キーなし → mp4 のみ対象
  - profile_image_url は _normal.jpg → _400x400.jpg で高画質版
"""

from __future__ import annotations

import asyncio
import io
import logging
import re

import httpx
from PIL import Image

from lib.ffmpeg import (
    encode_gif_to_gif,
    encode_gif_to_video,
    encode_video_from_url,
)
from lib.misskey_client import MAX_NOTE_FILES, MisskeyClient

try:
    import pillow_avif
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# NSFW 判定
# ------------------------------------------------------------------ #

def check_nsfw(text: str, config_nsfw: dict) -> bool:
    forced_words: list = config_nsfw.get("nsfw_word_forced", [])
    safe_words: list = config_nsfw.get("nsfw_word_safe", [])
    if forced_words and any(w in text for w in forced_words):
        return True
    if safe_words and any(w in text for w in safe_words):
        return False
    return config_nsfw.get("nsfw_forced", False)


def check_nsfw_video(text: str, config_nsfw: dict) -> bool:
    forced_words: list = config_nsfw.get("nsfw_word_forced", [])
    safe_words: list = config_nsfw.get("nsfw_word_safe", [])
    if forced_words and any(w in text for w in forced_words):
        return True
    if safe_words and any(w in text for w in safe_words):
        return False
    return config_nsfw.get("nsfw_forced_video", True)


# ------------------------------------------------------------------ #
# 画像URL取得
# ------------------------------------------------------------------ #

def get_photo_url(item) -> str:
    """
    Photo オブジェクトから画像URLを取得する。
    twikit の公開属性: media_url（例: https://pbs.twimg.com/media/XXX.jpg）
    """
    url = getattr(item, "media_url", None)
    if not url:
        # _data 経由のフォールバック
        data = getattr(item, "_data", {}) or {}
        url = data.get("media_url_https") or data.get("media_url", "")
    return url or ""


def normalize_photo_url(url: str) -> str:
    """
    pbs.twimg.com/media/ の画像URLを高画質版に変換する。
    ?format=jpg&name=large を付与（拡張子除去が必要）。
    profile_images / tweet_video_thumb 等は変換しない。
    """
    if not url:
        return url
    if "pbs.twimg.com/media/" in url:
        url = re.sub(r"\.(jpg|jpeg|png|webp)(\?.*)?$", "", url, flags=re.IGNORECASE)
        url = f"{url}?format=jpg&name=large"
    return url


def get_profile_image_url(user) -> str:
    """
    ユーザーのプロフィール画像URLを高画質版で取得する。
    _normal.jpg → _400x400.jpg に変換。
    """
    url = getattr(user, "profile_image_url", "") or ""
    if not url:
        return ""
    return re.sub(r"_normal(\.(jpg|png|gif|webp))$", r"_400x400\1", url)


# ------------------------------------------------------------------ #
# 動画URL取得
# ------------------------------------------------------------------ #

def get_video_url(item) -> str | None:
    """
    Video / AnimatedGif オブジェクトから最高ビットレートの動画URLを取得する。

    inspect_video 結果より:
      item.video_info = {
          "variants": [
              {"content_type": "application/x-mpegURL", "url": "...m3u8"},
              {"content_type": "video/mp4", "bitrate": 256000, "url": "...mp4"},
              {"content_type": "video/mp4", "bitrate": 2176000, "url": "...mp4"},
          ]
      }
    mp4 のみ対象（bitrate キーがあるもの）、最大 bitrate を選択。
    """
    video_info = getattr(item, "video_info", None) or {}
    variants: list[dict] = video_info.get("variants", [])

    if not variants:
        # フォールバック: item.variants が復活したフォーク版対応
        fallback = getattr(item, "variants", None)
        if fallback:
            variants = [
                {"bitrate": getattr(v, "bitrate", 0), "url": getattr(v, "url", "")}
                for v in fallback
            ]

    if not variants:
        logger.warning("動画の variants が空です")
        return None

    best_url: str | None = None
    best_bitrate: int = -1

    for v in variants:
        # dict 形式（現行 twikit）
        if isinstance(v, dict):
            bitrate = v.get("bitrate", None)
            url = v.get("url", "")
            content_type = v.get("content_type", "")
        else:
            # オブジェクト形式（フォーク版など）
            bitrate = getattr(v, "bitrate", None)
            url = getattr(v, "url", "")
            content_type = getattr(v, "content_type", "")

        # m3u8 は除外（bitrate キーなし or content_type で判定）
        if "mpegURL" in content_type or "m3u8" in (url or ""):
            continue
        if bitrate is None:
            continue

        if url and bitrate >= best_bitrate:
            best_bitrate = bitrate
            best_url = url

    if best_url:
        logger.debug("動画URL選択: %s (bitrate=%d)", best_url, best_bitrate)
    else:
        logger.warning("mp4 の variant が見つかりませんでした（variants=%s）", variants)

    return best_url


# ------------------------------------------------------------------ #
# 画像ダウンロード / AVIF変換
# ------------------------------------------------------------------ #

async def download_image_async(url: str) -> bytes:
    """httpx で非同期に画像をダウンロードする"""
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def convert_to_avif(image_data: bytes, quality: int = 60) -> bytes:
    """Pillow で AVIF に変換する（pillow-avif-plugin 必要）"""
    image = Image.open(io.BytesIO(image_data))
    buf = io.BytesIO()
    image.save(buf, format="AVIF", quality=quality)
    return buf.getvalue()


async def download_and_convert_to_avif(url: str, quality: int = 60) -> bytes:
    """URLから画像をダウンロードしてAVIFに変換する"""
    raw = await download_image_async(url)
    try:
        return convert_to_avif(raw, quality=quality)
    except Exception as e:
        logger.warning("AVIF変換失敗、元データを使用: %s", e)
        return raw


# ------------------------------------------------------------------ #
# メイン: メディア処理 & Misskey アップロード
# ------------------------------------------------------------------ #

async def download_media(
    media: list | None,
    misskey_client: MisskeyClient,
    text: str,
    tweet_id: str = "unknown",
    config_media: dict | None = None,
    config_nsfw: dict | None = None,
    *,
    max_files: int = MAX_NOTE_FILES,
) -> tuple[list | None, str]:
    """
    twikit の Tweet.media を処理して Misskey にアップロードする。

    Returns:
        (file_ids: list | None, text: str)
    """
    if config_media is None:
        config_media = {}
    if config_nsfw is None:
        config_nsfw = {}

    if not media:
        logger.debug("メディアなし (tweet_id=%s)", tweet_id)
        return None, text

    logger.info("メディア処理開始: %d 件 (tweet_id=%s)", len(media), tweet_id)

    video_encode: str = config_media.get("video_encode", "copy")
    gif_encode: str = config_media.get("gif_encode", "gif")
    gif_fpsmax: int = config_media.get("gif_encode_fpsmax", 15)
    pic_avif: bool = config_media.get("pic_encode_avif", True)

    file_ids: list[str] = []

    for idx, item in enumerate(media):
        if len(file_ids) >= max_files:
            logger.warning(
                "メディア上限で残りを省略: tweet_id=%s total=%d processed=%d skipped=%d",
                tweet_id, len(media), idx, len(media) - idx,
            )
            break
        media_type = getattr(item, "type", None)
        logger.info("  [%d/%d] type=%s", idx + 1, len(media), media_type)

        try:
            if media_type == "photo":
                nsfw = check_nsfw(text, config_nsfw)
                raw_url = get_photo_url(item)
                url = normalize_photo_url(raw_url)
                logger.debug("  画像URL: %s", url)

                if pic_avif:
                    file_data = await download_and_convert_to_avif(url, quality=50)
                    filename = f"{tweet_id}_{idx}.avif"
                else:
                    file_data = await download_image_async(url)
                    filename = f"{tweet_id}_{idx}.jpg"

                drv = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda fd=file_data, fn=filename, ns=nsfw:
                        misskey_client.drive_files_create(fd, name=fn, is_sensitive=ns)
                )
                file_ids.append(drv["id"])
                logger.info("  画像アップロード完了: %s (id=%s)", filename, drv["id"])

            elif media_type == "video":
                nsfw = check_nsfw_video(text, config_nsfw)
                url = get_video_url(item)
                if not url:
                    logger.warning("  動画URLが取得できませんでした (idx=%d)", idx)
                    continue

                logger.debug("  動画URL: %s", url)
                file_data = await encode_video_from_url(url, encode_mode=video_encode)
                filename = f"{tweet_id}_{idx}.mp4"
                drv = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda fd=file_data, fn=filename, ns=nsfw:
                        misskey_client.drive_files_create(fd, name=fn, is_sensitive=ns)
                )
                file_ids.append(drv["id"])
                logger.info("  動画アップロード完了: %s (id=%s)", filename, drv["id"])

            elif media_type == "animated_gif":
                nsfw = check_nsfw_video(text, config_nsfw)
                url = get_video_url(item)
                if not url:
                    logger.warning("  GIF URLが取得できませんでした (idx=%d)", idx)
                    continue

                logger.debug("  GIF URL: %s", url)
                if gif_encode == "gif":
                    file_data = await encode_gif_to_gif(url, fpsmax=gif_fpsmax)
                    filename = f"{tweet_id}_{idx}.gif"
                elif gif_encode == "x265":
                    file_data = await encode_gif_to_video(url)
                    filename = f"{tweet_id}_{idx}.mp4"
                else:
                    file_data = await encode_video_from_url(url, encode_mode="copy")
                    filename = f"{tweet_id}_{idx}.mp4"

                drv = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda fd=file_data, fn=filename, ns=nsfw:
                        misskey_client.drive_files_create(fd, name=fn, is_sensitive=ns)
                )
                file_ids.append(drv["id"])
                logger.info("  GIFアップロード完了: %s (id=%s)", filename, drv["id"])

            else:
                logger.warning("  未知のメディアタイプ: %s (idx=%d)", media_type, idx)

        except Exception:
            logger.exception("  メディア処理失敗 [idx=%d tweet_id=%s]", idx, tweet_id)

    # 末尾の t.co を除去
    text = re.sub(r"\s*https://t\.co/\S+$", "", text).rstrip()

    return (file_ids if file_ids else None), text