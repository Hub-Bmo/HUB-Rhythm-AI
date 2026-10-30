"""Windows 原生文字辨識元件以純英文路徑讀取語言資料；不搬移樂譜。"""
from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
import struct

from .contract import ContractError


def verify_legacy_models(folder, languages):
    """Audiveris 5.11 使用 OEM 0，僅 LSTM 的 fast/best 資料不能用。"""
    for language in languages:
        path=Path(folder)/(language+".traineddata")
        try:
            with path.open("rb") as stream:
                header=stream.read(4)
                count=struct.unpack("<i",header)[0]
                if not 6 <= count <= 128:
                    raise ValueError()
                offsets=struct.unpack("<"+"q"*count,stream.read(count*8))
            size=path.stat().st_size
            if any(not 4+8*count <= offsets[index] < size for index in (1,3,4,5)):
                raise ValueError()
        except (OSError,ValueError,struct.error) as error:
            raise ContractError(f"OCR 語言資料 {language} 不支援 Audiveris 的 legacy 模式；需使用官方 tessdata 的相容版本，不能使用僅 LSTM 的 fast／best 版。") from error


def _ascii_path(path):
    path=Path(path)
    if str(path).isascii():
        return path
    try:
        import ctypes
        buffer=ctypes.create_unicode_buffer(32768)
        size=ctypes.windll.kernel32.GetShortPathNameW(str(path),buffer,len(buffer))
        if 0 < size < len(buffer) and buffer.value.isascii():
            return Path(buffer.value)
    except (OSError,AttributeError):
        pass
    return None


def prepare_tessdata(folder, *, cache_root=None, windows=None):
    source=Path(folder).resolve()
    if not source.is_dir():
        raise ContractError("找不到本機文字辨識語言資料，請檢查 OMR 設定")
    if windows is None:
        windows=sys.platform=="win32"
    if not windows:
        return source
    direct=_ascii_path(source)
    if direct:
        return direct
    # Java 能讀中文路徑，內部 Tesseract 原生元件卻可能只得到空的語言清單。
    # 只快取公開的語言模型，不改使用者的專案或圖片所在路徑。
    files=sorted(source.glob("*.traineddata"))
    if not files:
        raise ContractError("本機文字辨識語言資料是空的，請安裝 OCR 語言資料")
    resources={p.name:p.read_bytes() for p in files}
    if (source/"LICENSE").is_file():
        resources["LICENSE"]=(source/"LICENSE").read_bytes()
    fingerprint=sha256()
    for name,data in sorted(resources.items()):
        fingerprint.update(name.encode("utf-8")+b"\0"+data)
    base=Path(cache_root or Path(tempfile.gettempdir())/"musicai-ocr-cache").resolve()
    base.mkdir(parents=True,exist_ok=True)
    safe=_ascii_path(base)
    if not safe:
        raise ContractError("Windows 文字辨識元件無法讀取目前的中文暫存路徑，請改用純英文的系統暫存位置後重開程式")
    target=safe/fingerprint.hexdigest()[:24]
    target.mkdir(parents=True,exist_ok=True)
    for name,data in resources.items():
        destination=target/name
        expected=sha256(data).digest()
        if destination.is_file() and sha256(destination.read_bytes()).digest()==expected:
            continue
        fd,temporary=tempfile.mkstemp(prefix=".ocr-data-",dir=target)
        temporary=Path(temporary)
        try:
            with os.fdopen(fd,"wb") as stream:
                stream.write(data)
            try:
                os.replace(temporary,destination)
            except OSError:
                # 兩份樂譜同時啟動時，另一個視窗可能已完成同一份資料。
                if not destination.is_file() or sha256(destination.read_bytes()).digest()!=expected:
                    raise
        finally:
            temporary.unlink(missing_ok=True)
    return target
