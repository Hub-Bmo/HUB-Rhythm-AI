"""Windows、macOS 與 Linux 共用的 pygame 音訊後端。

背景預覽使用 ``pygame.mixer.music``，即時擊打使用可重疊的 ``Sound``
channel。兩者共用同一個 SDL mixer，因此不會互相中斷。
"""

from __future__ import annotations

import os
from pathlib import Path
import threading


class AudioBackend:
    def __init__(self) -> None:
        self._pygame = None
        self._initialized = False
        self._lock = threading.RLock()
        self.error: str | None = None

    @property
    def available(self) -> bool:
        return self.ensure_initialized()

    def ensure_initialized(self) -> bool:
        with self._lock:
            if self._initialized:
                return True
            try:
                os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
                import pygame

                pygame.mixer.pre_init(frequency=22_050, size=-16, channels=1, buffer=512)
                pygame.mixer.init(
                    frequency=22_050,
                    size=-16,
                    channels=1,
                    buffer=512,
                    allowedchanges=0,
                )
                pygame.mixer.set_num_channels(32)
                self._pygame = pygame
                self._initialized = True
                self.error = None
                return True
            except Exception as exc:
                self.error = str(exc)
                return False

    def play_file(self, path: str | Path, *, loop: bool = False) -> None:
        if not self.ensure_initialized() or self._pygame is None:
            detail = f"：{self.error}" if self.error else ""
            raise RuntimeError(f"音訊系統無法啟動{detail}")
        with self._lock:
            # Windows 會持續鎖住上一個由 mixer.music 載入的 WAV；先卸載才能
            # 讓下一次預覽安全地重建或清除舊檔。
            try:
                self._pygame.mixer.music.unload()
            except Exception:
                pass
            self._pygame.mixer.music.load(str(Path(path).resolve()))
            self._pygame.mixer.music.play(loops=-1 if loop else 0)

    def make_sound(self, pcm: bytes):
        if not self.ensure_initialized() or self._pygame is None:
            return None
        with self._lock:
            return self._pygame.mixer.Sound(buffer=pcm)

    def play_sound(self, sound) -> None:
        if sound is not None and self.ensure_initialized():
            sound.play()

    def stop_all(self) -> None:
        with self._lock:
            if not self._initialized or self._pygame is None:
                return
            self._pygame.mixer.music.stop()
            try:
                self._pygame.mixer.music.unload()
            except Exception:
                pass
            self._pygame.mixer.stop()

    def shutdown(self) -> None:
        with self._lock:
            if not self._initialized or self._pygame is None:
                return
            self.stop_all()
            self._pygame.mixer.quit()
            self._initialized = False
            self._pygame = None


_BACKEND = AudioBackend()


def get_audio_backend() -> AudioBackend:
    return _BACKEND
