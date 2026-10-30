"""Human correction UI and pure helpers for recognition event mappings.

The selected mapping is deliberately represented by stable instrument/stroke
IDs.  A future cross-project mapping store can consume ``symbol_key`` from the
dialog result without coupling persistence to the Tk dialog.
"""

from __future__ import annotations

from copy import deepcopy
import re
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Iterable, Mapping
import unicodedata

from .instrument_library import UNKNOWN_ID, get_instrument, get_stroke
from .recognition import review_candidate, update_recognition_run
from .symbol_mappings import MAPPABLE_NOTATION_PROFILES, normalize_observed_symbol


def effective_candidate_data(candidate: Mapping[str, object]) -> Mapping[str, object]:
    """Return the latest complete data payload without mutating audit history."""

    if candidate.get("review_status") == "corrected":
        review = candidate.get("review")
        if isinstance(review, Mapping):
            corrected = review.get("corrected_data")
            if isinstance(corrected, Mapping):
                return corrected
    data = candidate.get("data")
    return data if isinstance(data, Mapping) else {}


def candidate_visible_symbol(candidate: Mapping[str, object]) -> str:
    """Return the visual symbol used for same-symbol matching and display."""

    # ``raw_symbol`` is the provider's event-level musical symbol.  The wider
    # observed-text crop is only a fallback and can contain instrument names
    # or neighboring print that would make an unsafe global rule key.
    data = candidate.get("data")
    if isinstance(data, Mapping):
        value = data.get("raw_symbol")
        if isinstance(value, str) and value.strip():
            return value.strip()
    evidence = candidate.get("source_evidence")
    if isinstance(evidence, Mapping):
        observed = evidence.get("observed_text")
        if isinstance(observed, str) and observed.strip():
            return observed.strip()
    return ""


def candidate_symbol_key(candidate: Mapping[str, object]) -> str:
    """Normalize harmless typography differences while retaining punctuation."""

    return normalize_observed_symbol(candidate_visible_symbol(candidate))


def is_numbered_rest_candidate(
    candidate: Mapping[str, object], notation_profile: object | None = None
) -> bool:
    """Recognize a standalone zero only on confirmed numbered notation."""

    if notation_profile != "numbered":
        return False

    value = unicodedata.normalize("NFKC", candidate_visible_symbol(candidate))
    if re.findall(r"\d", value) != ["0"]:
        return False
    # Do not treat descriptions such as "bar 0" or "定音鼓 0" as a score rest.
    return not any(character.isalpha() for character in value)


def run_notation_profile(run: Mapping[str, object]) -> str | None:
    """Resolve a concrete profile without turning ``auto`` into a rule key."""

    configured = str(run.get("notation_profile") or "").strip()
    if configured in MAPPABLE_NOTATION_PROFILES:
        return configured
    detected = str(run.get("detected_profile") or "").strip()
    return detected if detected in MAPPABLE_NOTATION_PROFILES else None


def candidate_original_instrument_id(candidate: Mapping[str, object]) -> str | None:
    """Return the provider's known instrument ID used by reusable rule keys."""

    data = candidate.get("data")
    if not isinstance(data, Mapping):
        return None
    instrument_id = str(data.get("instrument_id") or "").strip()
    return instrument_id if instrument_id and instrument_id != UNKNOWN_ID else None


def candidate_mapping_key(
    candidate: Mapping[str, object], notation_profile: object | None
) -> tuple[str, str, str] | None:
    """Return the safe profile/instrument/symbol key used for bulk corrections."""

    profile = str(notation_profile or "")
    instrument_id = candidate_original_instrument_id(candidate)
    symbol_key = candidate_symbol_key(candidate)
    if (
        profile not in MAPPABLE_NOTATION_PROFILES
        or instrument_id is None
        or not symbol_key
        or is_numbered_rest_candidate(candidate, profile)
    ):
        return None
    return profile, instrument_id, symbol_key


def corrected_event_data(
    candidate: Mapping[str, object],
    instrument_id: str,
    stroke_id: str,
    *,
    notation_profile: object | None = None,
) -> dict:
    """Copy the complete event payload and change only its stable mapping IDs."""

    if candidate.get("kind") != "event":
        raise ValueError("只有 event 候選可以對應樂器與擊法")
    if is_numbered_rest_candidate(candidate, notation_profile):
        raise ValueError("數字譜的 0 是休止，不能對應成敲擊事件")
    if not instrument_id or instrument_id == UNKNOWN_ID:
        raise ValueError("請選擇樂器")
    if not stroke_id or stroke_id == UNKNOWN_ID:
        raise ValueError("請選擇擊法")
    result = deepcopy(dict(effective_candidate_data(candidate)))
    result["instrument_id"] = instrument_id
    result["stroke_id"] = stroke_id
    return result


def matching_event_candidate_count(
    runs: Iterable[Mapping[str, object]],
    target_candidate: Mapping[str, object],
    *,
    target_run_id: str | None = None,
) -> int:
    """Count safe same-profile/instrument/symbol pending matches."""

    source_runs = list(runs)
    target_run = next(
        (
            run
            for run in source_runs
            if (target_run_id is None or str(run.get("run_id") or "") == target_run_id)
            and any(candidate is target_candidate for candidate in run.get("candidates", []))
        ),
        None,
    )
    target_key = candidate_mapping_key(
        target_candidate,
        run_notation_profile(target_run or {}),
    )
    if target_key is None:
        return 1
    return sum(
        1
        for run in source_runs
        for candidate in run.get("candidates", [])  # type: ignore[union-attr]
        if isinstance(candidate, Mapping)
        and candidate.get("kind") == "event"
        and candidate_mapping_key(candidate, run_notation_profile(run)) == target_key
        and (
            candidate is target_candidate
            or candidate.get("review_status", "pending") == "pending"
        )
    )


def correct_event_candidates(
    runs: Iterable[Mapping[str, object]],
    *,
    target_run_id: str,
    target_candidate_id: str,
    instrument_id: str,
    stroke_id: str,
    apply_same_symbol: bool,
    note: str = "",
    reviewed_at: str | None = None,
) -> tuple[list[dict], int]:
    """Return corrected run copies and the number of candidates changed.

    Every matched candidate keeps its own bar/tick/modifier data.  Rejected
    already reviewed candidates are not silently changed by a bulk mapping,
    though the explicitly selected candidate can always be corrected.
    """

    source_runs = list(runs)
    target: Mapping[str, object] | None = None
    for run in source_runs:
        if str(run.get("run_id") or "") != target_run_id:
            continue
        matches = [
            candidate
            for candidate in run.get("candidates", [])  # type: ignore[union-attr]
            if isinstance(candidate, Mapping)
            and str(candidate.get("candidate_id") or "") == target_candidate_id
        ]
        if len(matches) > 1:
            raise ValueError("辨識結果含有重複的候選 ID，為避免改錯資料，請重新辨識")
        target = matches[0] if matches else None
        if target is not None:
            break
    if target is None:
        raise ValueError("找不到選取的候選，請重新選擇")
    target_run = next(
        run
        for run in source_runs
        if str(run.get("run_id") or "") == target_run_id
    )
    target_profile = run_notation_profile(target_run)
    # Validate the selected mapping even when no same-symbol candidates exist.
    corrected_event_data(
        target,
        instrument_id,
        stroke_id,
        notation_profile=target_profile,
    )
    mapping_key = candidate_mapping_key(target, target_profile)

    corrected_count = 0
    output: list[dict] = []
    for run in source_runs:
        run_id = str(run.get("run_id") or "")
        changed = False
        candidates: list[object] = []
        for item in run.get("candidates", []):  # type: ignore[union-attr]
            candidate = item
            if not isinstance(candidate, Mapping):
                candidates.append(deepcopy(candidate))
                continue
            selected = (
                run_id == target_run_id and candidate is target
            )
            same_symbol = (
                apply_same_symbol
                and mapping_key is not None
                and candidate.get("kind") == "event"
                and candidate.get("review_status", "pending") == "pending"
                and candidate_mapping_key(
                    candidate, run_notation_profile(run)
                )
                == mapping_key
            )
            if selected or same_symbol:
                data = corrected_event_data(
                    candidate,
                    instrument_id,
                    stroke_id,
                    notation_profile=run_notation_profile(run),
                )
                candidate = review_candidate(
                    candidate,
                    "corrected",
                    corrected_data=data,
                    note=note,
                    reviewed_at=reviewed_at,
                )
                corrected_count += 1
                changed = True
            else:
                candidate = deepcopy(dict(candidate))
            candidates.append(candidate)
        if changed:
            output.append(update_recognition_run(run, candidates, updated_at=reviewed_at))
        else:
            output.append(deepcopy(dict(run)))
    return output, corrected_count


class CandidateMappingDialog(tk.Toplevel):
    """Select one library instrument/stroke for an event candidate."""

    def __init__(
        self,
        master: tk.Misc,
        candidate: Mapping[str, object],
        library: Mapping[str, object],
        *,
        matching_count: int,
        notation_profile: str | None,
    ) -> None:
        super().__init__(master)
        self.title("更正／對應辨識候選")
        self.geometry("640x510")
        self.minsize(560, 460)
        self.transient(master)
        self.candidate = candidate
        self.library = library
        self.result: dict | None = None
        self.instrument_ids: list[str] = []
        self.stroke_ids: list[str] = []
        self.instrument_var = tk.StringVar()
        self.stroke_var = tk.StringVar()
        self.scope_var = tk.BooleanVar(value=matching_count > 1)
        self.save_rule_var = tk.BooleanVar(value=False)
        self.matching_count = max(1, int(matching_count))
        self.notation_profile = notation_profile
        self.source_instrument_id = candidate_original_instrument_id(candidate)
        self.can_save_rule = (
            notation_profile in MAPPABLE_NOTATION_PROFILES
            and self.source_instrument_id is not None
            and get_instrument(library, self.source_instrument_id) is not None
            and bool(candidate_symbol_key(candidate))
            and not is_numbered_rest_candidate(candidate, notation_profile)
        )
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        symbol = candidate_visible_symbol(self.candidate) or "（沒有文字證據）"
        ttk.Label(outer, text="辨識到的符號", font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(outer, text=symbol, wraplength=560).pack(anchor="w", pady=(2, 12))

        ttk.Label(outer, text="樂器", font=("", 10, "bold")).pack(anchor="w")
        self.instrument_combo = ttk.Combobox(
            outer,
            textvariable=self.instrument_var,
            state="readonly",
        )
        self.instrument_combo.pack(fill="x", pady=(3, 10))
        self.instrument_combo.bind("<<ComboboxSelected>>", self._instrument_selected)

        ttk.Label(outer, text="擊法", font=("", 10, "bold")).pack(anchor="w")
        self.stroke_combo = ttk.Combobox(
            outer,
            textvariable=self.stroke_var,
            state="readonly",
        )
        self.stroke_combo.pack(fill="x", pady=(3, 12))

        self.scope_check = ttk.Checkbutton(
            outer,
            text=(
                "同時更正目前照片／段落中的相同符號"
                f"（共 {self.matching_count} 筆；其他已複核者不變）"
            ),
            variable=self.scope_var,
        )
        self.scope_check.pack(anchor="w")
        if self.matching_count <= 1:
            self.scope_check.state(["disabled"])

        self.save_rule_check = ttk.Checkbutton(
            outer,
            text=(
                "保存成跨專案辨識建議規則（未來仍要人工按「套用建議」）"
            ),
            variable=self.save_rule_var,
        )
        self.save_rule_check.pack(anchor="w", pady=(8, 0))
        if not self.can_save_rule:
            self.save_rule_check.state(["disabled"])

        ttk.Label(
            outer,
            text=(
                "這次只會更正候選中的樂器與擊法；小節、拍點、重音、裝飾音與輪奏"
                "都會保留。批次更正只會處理相同譜種、相同原始樂器與相同符號。\n"
                "只有勾選保存規則時，之後的樂譜才會顯示相同對應建議；程式不會自動接受。\n"
                "如果需要的擊法不在清單，請先到「設定 → 樂器／擊法庫」新增。"
            ),
            foreground="#6F736F",
            justify="left",
            wraplength=560,
        ).pack(fill="x", pady=(14, 8))

        buttons = ttk.Frame(outer)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="套用更正", command=self._submit).pack(
            side="right", padx=(0, 7)
        )

        current = effective_candidate_data(self.candidate)
        instruments = [
            instrument
            for instrument in self.library.get("instruments", [])  # type: ignore[union-attr]
            if isinstance(instrument, Mapping)
            and isinstance(instrument.get("instrument_id"), str)
        ]
        self.instrument_ids = [str(item["instrument_id"]) for item in instruments]
        self.instrument_combo["values"] = [str(item.get("name") or item["instrument_id"]) for item in instruments]
        if self.instrument_ids:
            current_id = str(current.get("instrument_id") or "")
            index = self.instrument_ids.index(current_id) if current_id in self.instrument_ids else 0
            self.instrument_combo.current(index)
            self._refresh_strokes(str(current.get("stroke_id") or ""))

    def _instrument_selected(self, _event=None) -> None:
        self._refresh_strokes("")

    def _selected_instrument_id(self) -> str | None:
        index = self.instrument_combo.current()
        return self.instrument_ids[index] if 0 <= index < len(self.instrument_ids) else None

    def _selected_stroke_id(self) -> str | None:
        index = self.stroke_combo.current()
        return self.stroke_ids[index] if 0 <= index < len(self.stroke_ids) else None

    def _refresh_strokes(self, selected_id: str) -> None:
        instrument_id = self._selected_instrument_id()
        instrument = get_instrument(self.library, instrument_id) if instrument_id else None
        strokes = [
            stroke
            for stroke in (instrument or {}).get("strokes", [])
            if isinstance(stroke, Mapping) and isinstance(stroke.get("stroke_id"), str)
        ]
        self.stroke_ids = [str(item["stroke_id"]) for item in strokes]
        self.stroke_combo["values"] = [
            f"{item.get('name') or item['stroke_id']}  [{item.get('code') or '—'}]"
            for item in strokes
        ]
        if self.stroke_ids:
            index = self.stroke_ids.index(selected_id) if selected_id in self.stroke_ids else 0
            self.stroke_combo.current(index)
        else:
            self.stroke_var.set("")

    def _submit(self) -> None:
        instrument_id = self._selected_instrument_id()
        stroke_id = self._selected_stroke_id()
        if instrument_id is None or stroke_id is None:
            messagebox.showerror("無法套用更正", "請選擇同時具有可用擊法的樂器。", parent=self)
            return
        if get_stroke(self.library, instrument_id, stroke_id) is None:
            messagebox.showerror("無法套用更正", "選取的擊法不屬於這件樂器。", parent=self)
            return
        if self.save_rule_var.get() and not self.can_save_rule:
            messagebox.showerror(
                "無法保存規則",
                "這筆候選缺少明確的譜種、原始樂器或辨識符號，只能做單次人工更正。",
                parent=self,
            )
            return
        if self.save_rule_var.get() and instrument_id != self.source_instrument_id:
            messagebox.showerror(
                "無法保存規則",
                "跨專案規則只能替同一件已辨識樂器對應擊法。\n"
                "這次仍可更換樂器，但請取消勾選「保存成跨專案規則」。",
                parent=self,
            )
            return
        self.result = {
            "instrument_id": instrument_id,
            "stroke_id": stroke_id,
            "apply_same_symbol": bool(self.scope_var.get()) and self.matching_count > 1,
            "symbol_key": candidate_symbol_key(self.candidate),
            "save_rule": bool(self.save_rule_var.get()),
        }
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


__all__ = [
    "CandidateMappingDialog",
    "candidate_symbol_key",
    "candidate_mapping_key",
    "candidate_original_instrument_id",
    "candidate_visible_symbol",
    "correct_event_candidates",
    "corrected_event_data",
    "effective_candidate_data",
    "is_numbered_rest_candidate",
    "matching_event_candidate_count",
    "run_notation_profile",
]
