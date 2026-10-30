"""樂譜照片的可攜式專案資產管理。

專案檔只保存 POSIX 格式的相對路徑；本模組是唯一將它們解析成本機路徑的地方。
這樣專案可以整個搬移，也避免被惡意或損壞的 ``..`` 路徑帶出
專案目錄。
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import struct
from typing import Iterable, Mapping


ORIGINAL_ASSET_DIRECTORY = "assets/original"
SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png"})


class PhotoAssetError(ValueError):
    """樂譜照片無法安全匯入或解讀。"""


class UnsupportedImageError(PhotoAssetError):
    """檔案不是可識別的 JPEG 或 PNG。"""


class UnsafeProjectPathError(PhotoAssetError):
    """相對路徑可能離開專案目錄。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_project_relative_path(relative_path: str | os.PathLike[str]) -> str:
    """驗證並回傳可儲存在 JSON 的 POSIX 相對路徑。

    Windows 與 POSIX 的絕對路徑都會被拒絕；反斜線會先當作路徑
    分隔符處理，所以 ``..\\secret`` 在 macOS/Linux 也不會漏網。
    """

    raw = os.fspath(relative_path)
    if not isinstance(raw, str):
        raise TypeError("relative_path 必須是文字路徑")
    if not raw or "\x00" in raw:
        raise UnsafeProjectPathError("專案相對路徑不可為空或含 NUL")

    windows_path = PureWindowsPath(raw)
    portable = raw.replace("\\", "/")
    path = PurePosixPath(portable)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise UnsafeProjectPathError("專案內不可保存絕對路徑")

    # PurePath 會悄悄收摺 '.'，因此要從原始切分結果檢查。
    raw_parts = portable.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise UnsafeProjectPathError("專案相對路徑不可含空段、. 或 ..")
    return path.as_posix()


def safe_project_path(
    project_directory: str | Path,
    relative_path: str | os.PathLike[str],
    *,
    must_exist: bool = False,
) -> Path:
    """在專案目錄內安全解析相對路徑。

    除了語法檢查，``resolve`` 後還會再檢查一次邊界，阻擋已存在
    的符號連結離開專案目錄。
    """

    normalized = normalize_project_relative_path(relative_path)
    root = Path(project_directory).expanduser().resolve()
    candidate = (root / Path(*PurePosixPath(normalized).parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise UnsafeProjectPathError("資產路徑離開了專案目錄") from error
    if must_exist and not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_png(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as file:
        header = file.read(24)
    if not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if len(header) < 24 or header[12:16] != b"IHDR":
        raise UnsupportedImageError("PNG 缺少有效的 IHDR")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise UnsupportedImageError("PNG 尺寸無效")
    return width, height


_JPEG_START_OF_FRAME_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _probe_jpeg(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as file:
        if file.read(2) != b"\xff\xd8":
            return None
        while True:
            prefix = file.read(1)
            while prefix and prefix != b"\xff":
                prefix = file.read(1)
            if not prefix:
                break
            marker_byte = file.read(1)
            while marker_byte == b"\xff":
                marker_byte = file.read(1)
            if not marker_byte:
                break
            marker = marker_byte[0]
            if marker in {0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)}:
                if marker == 0xD9:
                    break
                continue
            length_bytes = file.read(2)
            if len(length_bytes) != 2:
                break
            segment_length = int.from_bytes(length_bytes, "big")
            if segment_length < 2:
                raise UnsupportedImageError("JPEG 區段長度無效")
            if marker in _JPEG_START_OF_FRAME_MARKERS:
                frame = file.read(segment_length - 2)
                if len(frame) < 5:
                    break
                height = int.from_bytes(frame[1:3], "big")
                width = int.from_bytes(frame[3:5], "big")
                if width < 1 or height < 1:
                    raise UnsupportedImageError("JPEG 尺寸無效")
                return width, height
            # SOS 後是壓縮像素流；若此前沒有 SOF，就不是可用照片。
            if marker == 0xDA:
                break
            file.seek(segment_length - 2, os.SEEK_CUR)
    raise UnsupportedImageError("JPEG 缺少可讀取的尺寸資訊")


def probe_image(path: str | Path) -> dict:
    """不依賴副檔名偵測 JPEG/PNG 類型與原始像素尺寸。"""

    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    png_size = _probe_png(source)
    if png_size is not None:
        width, height = png_size
        return {"mime_type": "image/png", "width": width, "height": height, "extension": ".png"}
    jpeg_size = _probe_jpeg(source)
    if jpeg_size is not None:
        width, height = jpeg_size
        return {"mime_type": "image/jpeg", "width": width, "height": height, "extension": ".jpg"}
    # _probe_jpeg 只會在有 JPEG SOI 時繼續解析；不是 JPEG 會回傳 None。
    raise UnsupportedImageError("只支援 JPEG 與 PNG 樂譜照片")


def import_image_asset(
    project_directory: str | Path,
    source_path: str | Path,
    *,
    imported_at: str | None = None,
) -> dict:
    """將照片複製進 ``assets/original`` 並回傳 JSON 可序列化紀錄。

    檔名是完整 SHA-256，因此內容相同的照片只會保存一份。不論
    來源副檔名為何，儲存副檔名都由實際內容決定。
    """

    project_root = Path(project_directory).expanduser().resolve()
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    metadata = probe_image(source)
    digest = _sha256(source)
    relative_path = f"{ORIGINAL_ASSET_DIRECTORY}/{digest}{metadata['extension']}"
    destination = safe_project_path(project_root, relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        if not destination.is_file() or _sha256(destination) != digest:
            raise PhotoAssetError(f"去重目標已被其他內容佔用：{destination}")
    elif source != destination:
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            shutil.copyfile(source, temporary)
            # 複製後驗雜湊，避免在讀取期間來源檔被取代。
            if _sha256(temporary) != digest:
                raise PhotoAssetError("複製期間來源照片發生變化")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    return {
        "asset_id": f"IMG-{digest[:24]}",
        "kind": "source_image",
        "relative_path": relative_path,
        "sha256": digest,
        "byte_size": source.stat().st_size,
        "width": metadata["width"],
        "height": metadata["height"],
        "mime_type": metadata["mime_type"],
        "original_filename": source.name,
        "imported_at": imported_at or _now_iso(),
        # The project migration assigns the next 1-based value once this
        # record joins project["assets"].  Keeping the provenance explicit
        # lets a later thumbnail UI distinguish import order from a user-
        # confirmed order without interpreting the file name.
        "page_sequence": None,
        "page_sequence_source": "import",
        "printed_page_number": None,
    }


def asset_file_path(
    project_directory: str | Path,
    asset: Mapping[str, object] | str | os.PathLike[str],
    *,
    must_exist: bool = False,
) -> Path:
    """將資產紀錄（或其相對路徑）解析成專案內檔案。"""

    if isinstance(asset, (str, os.PathLike)):
        relative_path = os.fspath(asset)
    else:
        relative_path = asset.get("relative_path")
        if not isinstance(relative_path, str):
            raise PhotoAssetError("資產紀錄缺少 relative_path")
    return safe_project_path(project_directory, relative_path, must_exist=must_exist)


def asset_exists(
    project_directory: str | Path,
    asset: Mapping[str, object] | str | os.PathLike[str],
) -> bool:
    """路徑安全且檔案存在時回傳 True；損壞路徑視同缺圖。"""

    try:
        return asset_file_path(project_directory, asset).is_file()
    except (PhotoAssetError, OSError, TypeError):
        return False


def find_missing_assets(
    project_directory: str | Path,
    assets: Iterable[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """回傳缺少檔案或路徑不安全的資產紀錄。"""

    return [asset for asset in assets if not asset_exists(project_directory, asset)]


__all__ = [
    "ORIGINAL_ASSET_DIRECTORY",
    "PhotoAssetError",
    "SUPPORTED_IMAGE_MIME_TYPES",
    "UnsafeProjectPathError",
    "UnsupportedImageError",
    "asset_exists",
    "asset_file_path",
    "find_missing_assets",
    "import_image_asset",
    "normalize_project_relative_path",
    "probe_image",
    "safe_project_path",
]
