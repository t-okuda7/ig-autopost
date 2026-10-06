"""Google Drive の読み書き。サービスアカウントで、共有されたフォルダだけを触る。

サービスアカウントは新しいファイルを作れない（容量を持たないため）ので、
ここでは「読む・名前を変える・説明を書く・移動する」しかしない。
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SCOPES = ["https://www.googleapis.com/auth/drive"]
_FIELDS = "id,name,mimeType,parents,description,appProperties"


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    parents: list[str] = field(default_factory=list)
    description: str = ""
    props: dict = field(default_factory=dict)


def _to_file(raw: dict) -> DriveFile:
    return DriveFile(
        id=raw["id"], name=raw["name"], mime=raw.get("mimeType", ""),
        parents=raw.get("parents", []), description=raw.get("description", ""),
        props=raw.get("appProperties", {}) or {},
    )


class Drive:
    def __init__(self, service_account_json: str = "", http=None):
        """http はテスト用。ふだんはサービスアカウントの鍵（JSONの中身）を渡す。"""
        from googleapiclient.discovery import build

        if http is not None:
            self._api = build("drive", "v3", http=http, cache_discovery=False)
            return
        from google.oauth2 import service_account

        info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        self._api = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def get(self, file_id: str) -> DriveFile:
        raw = self._api.files().get(fileId=file_id, fields=_FIELDS, supportsAllDrives=True).execute()
        return _to_file(raw)

    def children(self, folder_id: str) -> list[DriveFile]:
        files: list[DriveFile] = []
        token = None
        while True:
            page = self._api.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=f"nextPageToken, files({_FIELDS})",
                pageSize=200, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            files.extend(_to_file(raw) for raw in page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                return files

    def export_text(self, file_id: str) -> str:
        data = self._api.files().export(fileId=file_id, mimeType="text/plain").execute()
        return data.decode("utf-8-sig") if isinstance(data, bytes) else str(data)

    def download(self, file_id: str, dest: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload

        dest.parent.mkdir(parents=True, exist_ok=True)
        request = self._api.files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.FileIO(dest, "wb")
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buffer.close()
        return dest

    def update(self, file_id: str, *, name: str | None = None, description: str | None = None,
               props: dict | None = None) -> None:
        body: dict = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if props is not None:
            body["appProperties"] = props
        if body:
            self._api.files().update(fileId=file_id, body=body, supportsAllDrives=True).execute()

    def move(self, file_id: str, new_parent: str, old_parent: str) -> None:
        self._api.files().update(
            fileId=file_id, addParents=new_parent, removeParents=old_parent,
            supportsAllDrives=True,
        ).execute()
