"""OpenAI 模型與安全憑證設定對話框。"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from .openai_settings import (
    API_KEY_SOURCE_ENVIRONMENT,
    IMAGE_DETAIL_OPTIONS,
    OpenAICredentialError,
    OpenAICredentialStore,
    OpenAISettingsError,
    OpenAISettingsStore,
    credential_status_text,
    normalize_settings,
)


RECOMMENDED_MODELS = ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna")
OPENAI_USAGE_URL = "https://platform.openai.com/usage"


class OpenAISettingsDialog(tk.Toplevel):
    """編輯公開設定，並將新 API key 交給系統憑證區保存。

    既有金鑰絕不放入 ``Entry``；輸入框只接受新的或替換用金鑰。
    ``saved`` 表示本次對話框是否已經持久化任何變更。
    """

    def __init__(
        self,
        master: tk.Misc,
        settings_store: OpenAISettingsStore,
        credential_store: OpenAICredentialStore,
    ) -> None:
        super().__init__(master)
        self.title("OpenAI 辨識設定")
        self.geometry("620x500")
        self.minsize(570, 460)
        self.transient(master)

        self.settings_store = settings_store
        self.credential_store = credential_store
        self.settings = settings_store.load()
        self.saved = False
        self.result_settings: dict | None = None

        self.model_var = tk.StringVar(value=self.settings["model"])
        self.detail_var = tk.StringVar(value=self.settings["image_detail"])
        self.api_key_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="正在檢查金鑰設定…")
        self.note_var = tk.StringVar(value="")

        self._build()
        self._refresh_status()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="OpenAI 線上樂譜辨識",
            font=("", 14, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "模型與圖片精細度會存於本機 AppData；API key 只存於環境變數或"
                " Windows 認證管理員，不會寫入專案。"
            ),
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(5, 16))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="模型").grid(row=0, column=0, sticky="w", pady=5)
        self.model_entry = ttk.Combobox(
            form,
            textvariable=self.model_var,
            values=RECOMMENDED_MODELS,
        )
        self.model_entry.grid(row=0, column=1, sticky="ew", padx=(14, 0), pady=5)

        ttk.Label(form, text="圖片 detail").grid(row=1, column=0, sticky="w", pady=5)
        self.detail_combo = ttk.Combobox(
            form,
            textvariable=self.detail_var,
            values=IMAGE_DETAIL_OPTIONS,
            state="readonly",
        )
        self.detail_combo.grid(row=1, column=1, sticky="ew", padx=(14, 0), pady=5)

        ttk.Label(
            outer,
            text=(
                "模型參考（每 100 萬 input／output tokens）："
                "Astra US$10／50，6.1 Sol US$2／10，Luna US$0.10／0.50。\n"
                "Astra 品質優先、6.1 Sol 較均衡、Luna 成本最低；"
                "價格日期 2026-10-07，實際帳單仍以 OpenAI 為準。\n"
                "這三個模型目前不支援 Free API tier；帳戶需有可用 API 額度。"
            ),
            foreground="#6F736F",
            wraplength=575,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        separator = ttk.Separator(outer, orient="horizontal")
        separator.pack(fill="x", pady=14)

        ttk.Label(outer, text="API key", font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            textvariable=self.status_var,
            foreground="#3B6650",
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(4, 8))

        self.key_entry = ttk.Entry(
            outer,
            textvariable=self.api_key_var,
            show="•",
        )
        self.key_entry.pack(fill="x")
        ttk.Label(
            outer,
            text="輸入新金鑰可新增或替換；留空會保留既有金鑰，且不會將舊金鑰回顯。",
            foreground="#6F736F",
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(5, 5))
        ttk.Label(
            outer,
            textvariable=self.note_var,
            foreground="#8A5A24",
            wraplength=540,
            justify="left",
        ).pack(anchor="w")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(18, 0))
        self.remove_button = ttk.Button(
            actions,
            text="移除已儲存金鑰",
            command=self._remove_api_key,
        )
        self.remove_button.pack(side="left")
        ttk.Button(
            actions,
            text="查看官方用量／帳單",
            command=lambda: webbrowser.open_new_tab(OPENAI_USAGE_URL),
        ).pack(side="left", padx=(7, 0))
        ttk.Button(actions, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(actions, text="儲存", command=self._save).pack(
            side="right", padx=(0, 7)
        )

    def _refresh_status(self) -> None:
        try:
            status = self.credential_store.get_status()
        except OpenAICredentialError as error:
            self.status_var.set(f"無法存取安全憑證區：{error}")
            self.note_var.set("你仍可使用 OPENAI_API_KEY 環境變數。")
            return

        self.status_var.set(credential_status_text(status))
        if status.source == API_KEY_SOURCE_ENVIRONMENT:
            self.note_var.set(
                "目前環境變數優先；即使另存金鑰，也會等移除環境變數後才使用。"
            )
        else:
            self.note_var.set("")

    def _save(self) -> None:
        candidate = {
            "schema_version": self.settings.get("schema_version"),
            "model": self.model_var.get(),
            "image_detail": self.detail_var.get(),
        }
        try:
            clean = normalize_settings(candidate)
            new_api_key = self.api_key_var.get().strip()
            if new_api_key:
                self.credential_store.set_api_key(new_api_key)
                # 即使後續 UI 處理失敗，也不要讓金鑰繼續留在 widget 中。
                self.api_key_var.set("")
            self.settings_store.save(clean)
        except (OpenAISettingsError, OpenAICredentialError) as error:
            messagebox.showerror("OpenAI 設定儲存失敗", str(error), parent=self)
            return

        self.settings = clean
        self.result_settings = clean
        self.saved = True
        self.destroy()

    def _remove_api_key(self) -> None:
        if not messagebox.askyesno(
            "移除 API key",
            (
                "要移除 Windows 認證管理員／系統憑證區中的 OpenAI API key 嗎？\n\n"
                "OPENAI_API_KEY 環境變數不會被修改。"
            ),
            parent=self,
        ):
            return
        try:
            removed = self.credential_store.remove_api_key()
        except OpenAICredentialError as error:
            messagebox.showerror("無法移除 API key", str(error), parent=self)
            return

        self.saved = self.saved or removed
        self.api_key_var.set("")
        self._refresh_status()
        if not removed:
            messagebox.showinfo(
                "沒有已儲存金鑰",
                "系統憑證區中沒有可移除的 OpenAI API key。",
                parent=self,
            )

    def _cancel(self) -> None:
        self.api_key_var.set("")
        self.destroy()


def show_openai_settings_dialog(
    master: tk.Misc,
    settings_store: OpenAISettingsStore,
    credential_store: OpenAICredentialStore,
) -> bool:
    """顯示 modal 對話框，回傳是否有任何變更已持久化。"""
    dialog = OpenAISettingsDialog(master, settings_store, credential_store)
    master.wait_window(dialog)
    return dialog.saved


__all__ = [
    "OpenAISettingsDialog",
    "OPENAI_USAGE_URL",
    "RECOMMENDED_MODELS",
    "show_openai_settings_dialog",
]
