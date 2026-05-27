import io
import logging
import re

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/gif", "image/tiff"}
FOLDER_MIME = "application/vnd.google-apps.folder"


def _safe_name(drive_id: str, name: str) -> str:
    clean = re.sub(r"[^\w\-_\.]", "_", name)
    return f"{drive_id}_{clean}"


class DriveService:
    def __init__(self, credentials_path: str):
        creds = Credentials.from_service_account_file(
            credentials_path,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def list_images(self, folder_id: str) -> list[dict]:
        images: list[dict] = []
        self._scan(folder_id, images, [])
        return images

    def _scan(self, folder_id: str, images: list[dict], folder_path: list[str]):
        page_token = None
        while True:
            resp = (
                self.service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageToken=page_token,
                    pageSize=1000,
                )
                .execute()
            )
            for f in resp.get("files", []):
                if f["mimeType"] == FOLDER_MIME:
                    self._scan(f["id"], images, folder_path + [f["name"]])
                elif f["mimeType"] in IMAGE_MIMES:
                    f["folder_path"] = folder_path  # e.g. ["Necklace", "Gold"]
                    images.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def download(self, file_id: str, dest_path: str) -> bytes:
        request = self.service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        content = buf.getvalue()
        with open(dest_path, "wb") as f:
            f.write(content)
        return content

    @staticmethod
    def safe_filename(drive_id: str, name: str) -> str:
        return _safe_name(drive_id, name)
