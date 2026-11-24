"""
Misskey API 独自実装クライアント
misskey-py の代替として、OpenAPI ドキュメントを元に必要なエンドポイントを直接実装する。
- drive/files/create: multipart/form-data, タイムアウト延長対応
- notes/create
- i/update / i/pin / i/unpin
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_NOTE_FILES = 16

# デフォルトタイムアウト設定（秒）
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=300.0, pool=10.0)
# ドライブアップロード用（大容量ファイル対応）
UPLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0)

# リトライ設定
RETRY_COUNT = 5
RETRY_BACKOFF_BASE = 2.0  # 指数バックオフの底（秒）
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


class MisskeyAPIError(Exception):
    """Misskey API からエラーが返った場合の例外"""
    def __init__(self, status_code: int, body: Any, endpoint: str):
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint
        super().__init__(f"Misskey API error [{status_code}] on {endpoint}: {body}")


class MisskeyClient:
    """
    Misskey API のシンプルな独自実装クライアント。
    全リクエストは JSON + Bearer 認証で行う（drive/files/create は multipart）。
    """

    def __init__(self, host: str, token: str):
        """
        Args:
            host: Misskeyサーバーのドメイン（例: "misskey.io"）
            token: アクセストークン（msk-xxx...）
        """
        # スキームが含まれていなければ https を付与
        if not host.startswith("http"):
            host = f"https://{host}"
        self.base_url = host.rstrip("/") + "/api"
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        endpoint: str,
        payload: dict | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        """
        JSON リクエストを送信し、レスポンスを返す。
        リトライ・指数バックオフ付き。
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        if payload is None:
            payload = {}
        payload["i"] = self.token
        t = timeout or DEFAULT_TIMEOUT

        last_exc: Exception | None = None
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                with httpx.Client(timeout=t) as client:
                    resp = client.post(url, json=payload, headers={"Authorization": f"Bearer {self.token}"})

                if resp.status_code in (200, 204):
                    if resp.content:
                        return resp.json()
                    return {}

                if resp.status_code in RETRY_STATUS_CODES:
                    wait = RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "Misskey API %s returned %d (attempt %d/%d), retry in %.1fs",
                        endpoint, resp.status_code, attempt, RETRY_COUNT, wait
                    )
                    time.sleep(wait)
                    last_exc = MisskeyAPIError(resp.status_code, resp.text, endpoint)
                    continue

                # 4xx など回復不能なエラー
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise MisskeyAPIError(resp.status_code, body, endpoint)

            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                wait = RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "Misskey API %s network error (attempt %d/%d): %s, retry in %.1fs",
                    endpoint, attempt, RETRY_COUNT, e, wait
                )
                time.sleep(wait)
                last_exc = e

        raise last_exc or MisskeyAPIError(0, "max retries exceeded", endpoint)

    def _upload_request(
        self,
        endpoint: str,
        file_data: bytes,
        filename: str,
        is_sensitive: bool = False,
        folder_id: str | None = None,
    ) -> dict:
        """
        multipart/form-data でファイルをアップロードする。
        タイムアウトを長めに設定し、リトライ付き。
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        last_exc: Exception | None = None
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                # Content-Type は httpx が multipart 時に自動付与
                files = {"file": (filename, file_data)}
                data = {
                    "i": self.token,
                    "isSensitive": str(is_sensitive).lower(),
                    "force": "true",  # 同名ファイルを上書き
                }
                if folder_id:
                    data["folderId"] = folder_id
                if filename:
                    data["name"] = filename

                with httpx.Client(timeout=UPLOAD_TIMEOUT) as client:
                    resp = client.post(
                        url,
                        files=files,
                        data=data,
                        headers={"Authorization": f"Bearer {self.token}"},
                    )

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code in RETRY_STATUS_CODES:
                    wait = RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "Upload %s returned %d (attempt %d/%d), retry in %.1fs",
                        filename, resp.status_code, attempt, RETRY_COUNT, wait
                    )
                    time.sleep(wait)
                    last_exc = MisskeyAPIError(resp.status_code, resp.text, endpoint)
                    continue

                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise MisskeyAPIError(resp.status_code, body, endpoint)

            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                wait = RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "Upload %s network error (attempt %d/%d): %s, retry in %.1fs",
                    filename, attempt, RETRY_COUNT, e, wait
                )
                time.sleep(wait)
                last_exc = e

        raise last_exc or MisskeyAPIError(0, "upload max retries exceeded", endpoint)

    # ------------------------------------------------------------------ #
    # Drive
    # ------------------------------------------------------------------ #

    def drive_files_create(
        self,
        file_data: bytes,
        name: str = "file",
        is_sensitive: bool = False,
        folder_id: str | None = None,
    ) -> dict:
        """ドライブにファイルをアップロードする"""
        logger.debug("drive_files_create: name=%s size=%d isSensitive=%s",
                     name, len(file_data), is_sensitive)
        result = self._upload_request(
            "drive/files/create",
            file_data=file_data,
            filename=name,
            is_sensitive=is_sensitive,
            folder_id=folder_id,
        )
        logger.debug("drive_files_create OK: id=%s", result.get("id"))
        return result

    def drive_files_delete(self, file_id: str) -> None:
        """ドライブのファイルを削除する"""
        self._request("drive/files/delete", {"fileId": file_id})

    # ------------------------------------------------------------------ #
    # Notes
    # ------------------------------------------------------------------ #

    def notes_create(
        self,
        text: str | None = None,
        visibility: str = "public",
        local_only: bool = False,
        file_ids: list[str] | None = None,
        reply_id: str | None = None,
        renote_id: str | None = None,
        cw: str | None = None,
    ) -> dict:
        """ノートを作成する"""
        if file_ids and len(file_ids) > MAX_NOTE_FILES:
            logger.warning(
                "notes_create: 添付を上限までに制限: total=%d limit=%d",
                len(file_ids), MAX_NOTE_FILES,
            )
            file_ids = file_ids[:MAX_NOTE_FILES]

        payload: dict[str, Any] = {
            "visibility": visibility,
            "localOnly": local_only,
        }
        if text is not None:
            payload["text"] = text
        if file_ids:
            payload["fileIds"] = file_ids
        if reply_id:
            payload["replyId"] = reply_id
        if renote_id:
            payload["renoteId"] = renote_id
        if cw is not None:
            payload["cw"] = cw

        logger.debug(
            "notes_create: visibility=%s localOnly=%s replyId=%s renoteId=%s fileIds=%s text=%s...",
            visibility, local_only, reply_id, renote_id, file_ids,
            repr(text[:30]) if text else None,
        )
        result = self._request("notes/create", payload)
        logger.debug("notes_create OK: noteId=%s", result.get("createdNote", {}).get("id"))
        return result

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #

    def i(self) -> dict:
        """自分のアカウント情報を取得する（read:account 権限が必要）"""
        return self._request("i")

    def i_update(
        self,
        avatar_id: str | None = None,
        banner_id: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """アカウント情報を更新する"""
        payload: dict[str, Any] = {}
        if avatar_id is not None:
            payload["avatarId"] = avatar_id
        if banner_id is not None:
            payload["bannerId"] = banner_id
        payload.update(kwargs)
        return self._request("i/update", payload)

    def i_pin(self, note_id: str) -> dict:
        """ノートをピン留めする"""
        return self._request("i/pin", {"noteId": note_id})

    def i_unpin(self, note_id: str) -> dict:
        """ノートのピン留めを外す"""
        return self._request("i/unpin", {"noteId": note_id})