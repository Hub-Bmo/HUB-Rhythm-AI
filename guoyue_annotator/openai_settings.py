"""OpenAI 的公開設定與 API 金鑰保存。

公開設定存成 AppData 內的 JSON；API 金鑰則只從 ``OPENAI_API_KEY``
或作業系統的憑證儲存區取得。這兩條路徑刻意分開，避免金鑰誤入專案檔
或一般設定檔。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Mapping, Protocol


SETTINGS_SCHEMA_VERSION = "1.0"
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_IMAGE_DETAIL = "high"
IMAGE_DETAIL_OPTIONS = ("auto", "low", "high", "original")

OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
SETTINGS_PATH_ENV = "GUOYUE_ANNOTATOR_OPENAI_SETTINGS_PATH"
DEFAULT_SETTINGS_FILENAME = "openai-settings.json"

KEYRING_SERVICE_NAME = "GuoyueRhythmAnnotator.OpenAI"
KEYRING_ACCOUNT_NAME = "default"

API_KEY_SOURCE_ENVIRONMENT = "environment"
API_KEY_SOURCE_CREDENTIAL_STORE = "credential_store"

_SECRET_SETTING_NAMES = {
    "api_key",
    "apikey",
    "openai_api_key",
    "password",
    "secret",
    "token",
}
_UNSET = object()


class OpenAISettingsError(ValueError):
    """OpenAI 公開設定無效或無法讀寫。"""


class OpenAICredentialError(RuntimeError):
    """作業系統憑證儲存區無法使用。"""


class _KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None:
        ...

    def set_password(self, service_name: str, username: str, password: str) -> None:
        ...

    def delete_password(self, service_name: str, username: str) -> None:
        ...


@dataclass(frozen=True)
class ApiKeyStatus:
    """不含金鑰內容、可安全顯示在 UI 的狀態。"""

    configured: bool
    source: str | None


def default_settings_path() -> Path:
    """取得跨專案 OpenAI 公開設定的預設位置。"""
    configured = os.environ.get(SETTINGS_PATH_ENV)
    if configured:
        return Path(configured).expanduser()

    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home())
        return root / "GuoyueRhythmAnnotator" / DEFAULT_SETTINGS_FILENAME
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "GuoyueRhythmAnnotator"
            / DEFAULT_SETTINGS_FILENAME
        )
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "guoyue-rhythm-annotator" / DEFAULT_SETTINGS_FILENAME


def create_default_settings() -> dict:
    """建立只含非秘密欄位的預設設定。"""
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "model": DEFAULT_MODEL,
        "image_detail": DEFAULT_IMAGE_DETAIL,
    }


def _contains_secret_field(settings: Mapping[str, object]) -> bool:
    return any(str(key).strip().casefold() in _SECRET_SETTING_NAMES for key in settings)


def normalize_settings(settings: Mapping[str, object]) -> dict:
    """驗證並正規化公開設定，不保留未知欄位。"""
    if not isinstance(settings, Mapping):
        raise OpenAISettingsError("OpenAI 設定必須是 JSON object")
    if _contains_secret_field(settings):
        raise OpenAISettingsError("API 金鑰不得寫入一般設定檔")

    schema_version = str(
        settings.get("schema_version") or SETTINGS_SCHEMA_VERSION
    ).strip()
    if schema_version != SETTINGS_SCHEMA_VERSION:
        raise OpenAISettingsError(
            f"不支援的 OpenAI 設定版本：{schema_version!r}"
        )

    model = str(settings.get("model") or DEFAULT_MODEL).strip()
    if not model:
        raise OpenAISettingsError("OpenAI 模型名稱不可空白")
    if any(character.isspace() for character in model):
        raise OpenAISettingsError("OpenAI 模型名稱不可包含空白")

    image_detail = str(
        settings.get("image_detail") or DEFAULT_IMAGE_DETAIL
    ).strip().casefold()
    if image_detail not in IMAGE_DETAIL_OPTIONS:
        allowed = "、".join(IMAGE_DETAIL_OPTIONS)
        raise OpenAISettingsError(f"圖片 detail 必須是：{allowed}")

    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "model": model,
        "image_detail": image_detail,
    }


def load_settings(path: str | Path) -> dict:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise OpenAISettingsError(f"無法解析 OpenAI 設定 JSON：{source}") from error
    except OSError as error:
        raise OpenAISettingsError(f"無法讀取 OpenAI 設定：{source}") from error
    return normalize_settings(raw)


def save_settings(settings: Mapping[str, object], path: str | Path) -> Path:
    """以同目錄暫存檔原子寫入公開設定。"""
    destination = Path(path)
    clean = normalize_settings(settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(clean, ensure_ascii=False, indent=2) + "\n"

    descriptor = -1
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    except OSError as error:
        raise OpenAISettingsError(f"無法儲存 OpenAI 設定：{destination}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
    return destination


class OpenAISettingsStore:
    """AppData JSON 的路徑封裝，只處理非秘密設定。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_settings_path()

    def load(self, *, create_if_missing: bool = True) -> dict:
        if self.path.exists():
            return load_settings(self.path)
        if not create_if_missing:
            raise FileNotFoundError(self.path)
        settings = create_default_settings()
        self.save(settings)
        return settings

    def save(self, settings: Mapping[str, object]) -> Path:
        return save_settings(settings, self.path)

    def update(
        self,
        *,
        model: str | None = None,
        image_detail: str | None = None,
    ) -> dict:
        settings = self.load()
        if model is not None:
            settings["model"] = model
        if image_detail is not None:
            settings["image_detail"] = image_detail
        settings = normalize_settings(settings)
        self.save(settings)
        return settings


class OpenAICredentialStore:
    """從環境變數或系統憑證區存取 API key。

    ``keyring`` 只在第一次真的需要憑證區時匯入，因此僅以
    ``OPENAI_API_KEY`` 執行時不要求安裝 keyring。測試可傳入實作相同三個
    方法的 fake backend。
    """

    def __init__(
        self,
        *,
        keyring_backend: _KeyringBackend | None | object = _UNSET,
        environ: Mapping[str, str] | None = None,
        service_name: str = KEYRING_SERVICE_NAME,
        account_name: str = KEYRING_ACCOUNT_NAME,
    ):
        self._keyring_backend = keyring_backend
        self._environ = os.environ if environ is None else environ
        self.service_name = service_name
        self.account_name = account_name

    def _backend(self) -> _KeyringBackend:
        if self._keyring_backend is _UNSET:
            try:
                import keyring  # type: ignore[import-not-found]
            except ImportError as error:
                raise OpenAICredentialError(
                    "尚未安裝安全憑證元件 keyring；可改用 OPENAI_API_KEY 環境變數"
                ) from error
            self._keyring_backend = keyring
        if self._keyring_backend is None:
            raise OpenAICredentialError("安全憑證儲存區未啟用")
        return self._keyring_backend  # type: ignore[return-value]

    def _environment_key(self) -> str | None:
        value = self._environ.get(OPENAI_API_KEY_ENV)
        if value is None:
            return None
        clean = str(value).strip()
        return clean or None

    def get_stored_api_key(self) -> str | None:
        try:
            value = self._backend().get_password(
                self.service_name, self.account_name
            )
        except OpenAICredentialError:
            raise
        except Exception as error:
            raise OpenAICredentialError("無法讀取系統憑證中的 OpenAI API key") from error
        if value is None:
            return None
        clean = str(value).strip()
        return clean or None

    def get_api_key(self) -> str | None:
        """取得目前有效金鑰；環境變數永遠優先。"""
        environment_key = self._environment_key()
        if environment_key is not None:
            return environment_key
        return self.get_stored_api_key()

    def get_api_key_source(self) -> str | None:
        if self._environment_key() is not None:
            return API_KEY_SOURCE_ENVIRONMENT
        return (
            API_KEY_SOURCE_CREDENTIAL_STORE
            if self.get_stored_api_key() is not None
            else None
        )

    def get_status(self) -> ApiKeyStatus:
        source = self.get_api_key_source()
        return ApiKeyStatus(configured=source is not None, source=source)

    def has_api_key(self) -> bool:
        return self.get_api_key() is not None

    def has_stored_api_key(self) -> bool:
        return self.get_stored_api_key() is not None

    def set_api_key(self, api_key: str) -> None:
        """寫入系統憑證區；不會修改環境變數。"""
        clean = str(api_key).strip()
        if not clean:
            raise OpenAICredentialError("OpenAI API key 不可空白")
        if any(character.isspace() for character in clean):
            raise OpenAICredentialError("OpenAI API key 不可包含空白或換行")
        try:
            self._backend().set_password(
                self.service_name, self.account_name, clean
            )
        except OpenAICredentialError:
            raise
        except Exception as error:
            raise OpenAICredentialError("無法將 OpenAI API key 寫入系統憑證") from error

    def remove_api_key(self) -> bool:
        """移除系統憑證中的金鑰；環境變數不在程式內修改。"""
        if self.get_stored_api_key() is None:
            return False
        try:
            self._backend().delete_password(
                self.service_name, self.account_name
            )
        except OpenAICredentialError:
            raise
        except Exception as error:
            raise OpenAICredentialError("無法從系統憑證移除 OpenAI API key") from error
        return True


def credential_status_text(status: ApiKeyStatus) -> str:
    """產生不包含金鑰片段的繁體中文狀態文字。"""
    if status.source == API_KEY_SOURCE_ENVIRONMENT:
        return "已設定（由 OPENAI_API_KEY 環境變數提供，優先使用）"
    if status.source == API_KEY_SOURCE_CREDENTIAL_STORE:
        return "已設定（儲存在 Windows 認證管理員／系統憑證區）"
    return "尚未設定 OpenAI API key"


__all__ = [
    "API_KEY_SOURCE_CREDENTIAL_STORE",
    "API_KEY_SOURCE_ENVIRONMENT",
    "ApiKeyStatus",
    "DEFAULT_IMAGE_DETAIL",
    "DEFAULT_MODEL",
    "IMAGE_DETAIL_OPTIONS",
    "KEYRING_ACCOUNT_NAME",
    "KEYRING_SERVICE_NAME",
    "OPENAI_API_KEY_ENV",
    "OpenAICredentialError",
    "OpenAICredentialStore",
    "OpenAISettingsError",
    "OpenAISettingsStore",
    "create_default_settings",
    "credential_status_text",
    "default_settings_path",
    "load_settings",
    "normalize_settings",
    "save_settings",
]
