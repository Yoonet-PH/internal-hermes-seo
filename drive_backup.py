#!/usr/bin/env python3
"""Upload an encrypted Hermes backup blob to ai@'s Google Drive folder and prune
to the last N. Uses the Hermes service account impersonating ai@yoonet.io (the
folder lives in ai@'s My Drive). Called by config-backup.sh.

Usage: drive_backup.py <enc_file> <folder_id> <keep>
"""
import os
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

KEY = os.path.expanduser("~/.hermes/google_service_account.json")
SUBJECT = "ai@yoonet.io"


def main():
    enc, folder, keep = sys.argv[1], sys.argv[2], int(sys.argv[3])
    creds = service_account.Credentials.from_service_account_file(
        KEY, scopes=["https://www.googleapis.com/auth/drive"], subject=SUBJECT)
    d = build("drive", "v3", credentials=creds)

    name = os.path.basename(enc)
    media = MediaFileUpload(enc, mimetype="application/octet-stream", resumable=False)
    f = d.files().create(
        body={"name": name, "parents": [folder]},
        media_body=media, fields="id,name,size").execute()
    print(f"uploaded {f['name']} ({int(f.get('size', 0))} bytes) id={f['id']}")

    # prune: keep the newest <keep> hermes-config-* blobs in the folder
    q = f"'{folder}' in parents and trashed=false and name contains 'hermes-config-'"
    files = d.files().list(
        q=q, orderBy="createdTime desc",
        fields="files(id,name)").execute().get("files", [])
    for old in files[keep:]:
        d.files().delete(fileId=old["id"]).execute()
        print(f"pruned {old['name']}")


if __name__ == "__main__":
    main()
