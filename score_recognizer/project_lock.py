"""UI 專用作業系統鎖；程式退出即釋放，唯讀分析不必鎖定。"""
import os
from pathlib import Path
from .contract import ContractError


class ProjectLock:
    def __init__(self, path):
        path = Path(path).resolve()
        self.root = path if path.is_dir() else path.parent
        self.file = None
        handle = (self.root / ".recognizer.lock").open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            handle.close()
            raise ContractError("這份辨識專案已在另一個視窗開啟。可另開不同樂譜的專案；請勿讓兩台電腦同時編輯同一個雲端同步專案。") from error
        self.file = handle

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def load_locked(path):
    from .workspace import Workspace
    lock = ProjectLock(path)
    try:
        return Workspace.load(path), lock
    except Exception:
        lock.close()
        raise
