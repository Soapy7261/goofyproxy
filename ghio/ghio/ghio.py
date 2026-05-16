"""
provides `GhIo`, a `StorageBasedGoofyIo` that creates and reads files on a
GitHub repository to send and receive binary data.
"""

import logging
import requests
import base64
from goofyproxy import StorageBasedGoofyIo
from goofyproxy.common import *


MAX_FILE_AGE: float = 29.5
"delete files older than this many seconds."


class GhIo(StorageBasedGoofyIo):
    """
    a `StorageBasedGoofyIo` that creates and reads files on a GitHub repository
    to send and receive binary data.

    Args:
        github_token (str):
            GitHub Personal Access Token. avoid using tokens with full access
            permissions. use fine-grained tokens with access to only the
            repository used for data transfer with only the permission to read
            and modify its content.

        repo_owner (str):
            repository owner name

        repo_name (str):
            repository name

        branch (str):
            branch name

        id (str):
            sender ID to include in outgoing files so the other side knows who
            sent it.

        peer_id (str):
            sender ID of the peer. any incoming file with a different sender ID
            will be ignored.

        max_out_size (int):
            maximum outgoing file size in bytes.

        interval (float):
            minimum delay in seconds between outgoing files.

        log_level (int | None):
            logging level (e.g. `logging.INFO`)
    """

    github_token: str
    repo_owner: str
    repo_name: str
    branch: str

    _log: logging.Logger

    _base_url: str
    _headers: dict
    _session: requests.Session

    def __init__(
        self,
        github_token: str,
        repo_owner: str,
        repo_name: str,
        branch: str,
        id: str,
        peer_id: str,
        max_out_size: int = 200 * 1024,
        interval: float = .2,
        log_level: int | None = None
    ):
        self._log = make_logger(f"ghio", log_level)
        self._log.warning(
            "[IMPORTANT NOTICE] your system clock must be accurate to the "
            "second for ghio to work properly."
        )

        self.github_token = github_token
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.branch = branch

        self._base_url = \
            f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}"
        self._headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        }

        # initialize requests.Session
        self._session = requests.Session()

        # initialize super
        super().__init__(
            id,
            peer_id,
            max_out_size,
            interval,
            MAX_FILE_AGE,
            self._log
        )

    def _name(self) -> str:
        return "ghio"

    def _format_path(
        self,
        sender_id: str,
        peer_id: str,
        packet_idx: int,
        timestamp: float
    ) -> str:
        return f"ghio {sender_id} {peer_id} {packet_idx} {timestamp:.4f}.png"

    def _unformat_path(self, path: str) -> tuple[str, str, int, float] | None:
        try:
            if not (path.startswith("ghio#") and path.endswith(".png")):
                return None
            path = path[:-4]

            parts = path.split("#")
            if len(parts) < 5:
                return None

            sender = parts[1]
            receiver = parts[2]
            packet_idx = int(parts[3])
            timestamp = float(parts[4])
            return sender, receiver, packet_idx, timestamp
        except Exception:
            return None

    def _list_files(self) -> list[StorageBasedGoofyIo.File]:
        tree_url = f"{self._base_url}/git/trees/{self.branch}?recursive=1"
        response = self._session.get(tree_url, headers=self._headers)
        response.raise_for_status()
        tree = response.json()

        files: list[StorageBasedGoofyIo.File] = []
        for item in tree.get("tree", []):
            try:
                if item["type"] != "blob":
                    continue
                files.append(StorageBasedGoofyIo.File(item["path"]))
            except Exception:
                continue
        return files

    def _download_files(self, files: list[StorageBasedGoofyIo.File]):
        for file in files:
            url = f"{self._base_url}/contents/{file.path}"
            response = self._session.get(
                url,
                headers=self._headers,
                params={"ref": self.branch}
            )

            if response.status_code == 404:
                raise FileNotFoundError(
                    f"file \"{file}\" not found in repository "
                    f"{self.repo_owner}/{self.repo_name} on branch "
                    f"{self.branch}."
                )
            response.raise_for_status()

            # GitHub API returns base64 with newlines
            content_base64 = response.json()["content"]
            file.data = base64.b64decode(content_base64)

    def _upload_files(self, files: list[StorageBasedGoofyIo.File]):
        for file in files:
            if file.data is None:
                raise ValueError(
                    f"cannot upload file \"{file.path}\" with no data"
                )
            url = f"{self._base_url}/contents/{file.path}"

            # check if the file already exists to get its SHA (required for
            # updates) (disabled here because we don't do reuploads)
            sha = None
            if False:
                try:
                    response = self._session.get(
                        url,
                        params={"ref": self.branch},
                        headers=self._headers
                    )
                    if response.status_code == 200:
                        sha = response.json().get("sha")
                except requests.exceptions.RequestException:
                    # treat as new file if we can't check
                    pass

            content_base64 = base64.b64encode(file.data).decode("utf-8")
            payload = {
                "message": f"Upload/update {file.path}",
                "content": content_base64,
                "branch": self.branch
            }
            if sha:
                payload["sha"] = sha

            put_response = self._session.put(
                url,
                headers=self._headers,
                json=payload
            )
            put_response.raise_for_status()

    def _delete_files(self, paths: list[str]):
        for path in paths:
            try:
                # get the file's SHA (required for deletion)
                file_url = f"{self._base_url}/contents/{path}"
                resp = self._session.get(
                    file_url,
                    params={"ref": self.branch},
                    headers=self._headers
                )
                if resp.status_code != 200:
                    # file not found, skip silently
                    continue
                sha = resp.json()["sha"]

                # delete the file
                delete_url = f"{self._base_url}/contents/{path}"
                payload = {
                    "message": f"Delete {path}",
                    "sha": sha,
                    "branch": self.branch
                }
                del_resp = self._session.delete(
                    delete_url,
                    headers=self._headers,
                    json=payload
                )
                del_resp.raise_for_status()
            except requests.exceptions.RequestException:
                continue
