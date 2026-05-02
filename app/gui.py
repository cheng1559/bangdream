from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import tkinter as tk
from pathlib import Path

import customtkinter as ctk
import httpx

from . import auto_click_chart as clicker
from . import download_charts
from . import song_search


class QueueTextStream:
    def __init__(self, messages: "queue.Queue[str]"):
        self.messages = messages

    def write(self, value: str) -> int:
        if value:
            self.messages.put(value)
        return len(value)

    def flush(self) -> None:
        pass


class AutoClickApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("BangDream Auto Click")
        self.geometry("860x620")
        self.minsize(760, 520)

        self.messages: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.download_worker: threading.Thread | None = None
        self.controls: clicker.PlaybackControls | None = None
        self.stop_requested = False
        self.service = clicker.AutoClickChartService()
        self.song_records: list[song_search.SongRecord] = []
        self.song_labels: dict[str, song_search.SongRecord] = {}

        self.song_search_var = tk.StringVar(value="")
        self.song_select_var = tk.StringVar(value="Song list not loaded")
        self.song_id_var = tk.StringVar(value="")
        self.difficulty_var = tk.StringVar(value="expert")
        self.serial_var = tk.StringVar(value=clicker.DEFAULT_SERIAL)
        self.timing_noise_var = tk.StringVar(value="0")
        self.position_noise_var = tk.StringVar(value="0")
        self.dynamic_adjust_var = tk.BooleanVar(value=False)
        self.ignore_last_note_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Idle")
        self.timeline_var = tk.StringVar(value="+0 ms")
        self.config_widgets: list[ctk.CTkBaseClass] = []

        self._build_ui()
        self._load_song_records()
        self._bind_hotkeys()
        self._set_task_running(False)
        self.after(80, self._drain_messages)

    def _build_ui(self) -> None:
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        title = ctk.CTkLabel(self, text="BangDream Auto Click", font=ctk.CTkFont(size=22, weight="bold"))
        title.grid(row=0, column=0, padx=18, pady=(16, 8), sticky="w")

        downloader = ctk.CTkFrame(self, corner_radius=8)
        downloader.grid(row=1, column=0, padx=18, pady=8, sticky="ew")
        downloader.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(downloader, text="Charts", anchor="w").grid(
            row=0,
            column=0,
            padx=(12, 8),
            pady=10,
            sticky="w",
        )
        self.download_button = ctk.CTkButton(
            downloader,
            text="Download Charts",
            command=self.start_download,
            width=140,
        )
        self.download_button.grid(row=0, column=2, padx=8, pady=10, sticky="e")

        settings = ctk.CTkFrame(self, corner_radius=8)
        settings.grid(row=2, column=0, padx=18, pady=8, sticky="ew")
        settings.grid_columnconfigure((1, 3), weight=1)

        self._add_label(settings, "Song Search", 0, 0)
        self.song_search_entry = ctk.CTkEntry(
            settings,
            textvariable=self.song_search_var,
            placeholder_text="Search song name / ID",
        )
        self.song_search_entry.grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        self.song_search_var.trace_add("write", lambda *_args: self._refresh_song_options())

        self._add_label(settings, "Song", 0, 2)
        self.song_menu = ctk.CTkOptionMenu(
            settings,
            values=["Song list not loaded"],
            variable=self.song_select_var,
            command=self._select_song,
        )
        self.song_menu.grid(row=0, column=3, padx=8, pady=8, sticky="ew")

        self._add_label(settings, "Song ID", 1, 0)
        self.song_id_entry = ctk.CTkEntry(settings, textvariable=self.song_id_var)
        self.song_id_entry.grid(row=1, column=1, padx=8, pady=8, sticky="ew")

        self._add_label(settings, "Difficulty", 1, 2)
        self.difficulty_menu = ctk.CTkOptionMenu(
            settings,
            values=list(clicker.DIFFICULTIES),
            variable=self.difficulty_var,
        )
        self.difficulty_menu.grid(row=1, column=3, padx=8, pady=8, sticky="ew")

        self._add_label(settings, "ADB Serial", 2, 0)
        self.serial_entry = ctk.CTkEntry(settings, textvariable=self.serial_var)
        self.serial_entry.grid(
            row=2, column=1, columnspan=3, padx=8, pady=8, sticky="ew"
        )

        self._add_label(settings, "Timing Noise ms", 3, 0)
        self.timing_noise_entry = ctk.CTkEntry(settings, textvariable=self.timing_noise_var)
        self.timing_noise_entry.grid(
            row=3, column=1, padx=8, pady=8, sticky="ew"
        )

        self._add_label(settings, "Position Noise px", 3, 2)
        self.position_noise_entry = ctk.CTkEntry(settings, textvariable=self.position_noise_var)
        self.position_noise_entry.grid(
            row=3, column=3, padx=8, pady=8, sticky="ew"
        )

        self.dynamic_adjust_checkbox = ctk.CTkCheckBox(
            settings,
            text="Dynamic timing adjust",
            variable=self.dynamic_adjust_var,
            command=self._update_adjust_controls,
        )
        self.dynamic_adjust_checkbox.grid(row=4, column=1, padx=8, pady=(4, 10), sticky="w")
        self.ignore_last_note_checkbox = ctk.CTkCheckBox(
            settings,
            text="Ignore final note",
            variable=self.ignore_last_note_var,
        )
        self.ignore_last_note_checkbox.grid(row=4, column=2, padx=8, pady=(4, 10), sticky="w")
        self.config_widgets = [
            self.song_search_entry,
            self.song_menu,
            self.song_id_entry,
            self.difficulty_menu,
            self.serial_entry,
            self.timing_noise_entry,
            self.position_noise_entry,
            self.dynamic_adjust_checkbox,
            self.ignore_last_note_checkbox,
        ]

        controls = ctk.CTkFrame(self, corner_radius=8)
        controls.grid(row=3, column=0, padx=18, pady=8, sticky="nsew")
        controls.grid_columnconfigure(0, weight=1)
        controls.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(controls, fg_color="transparent")
        toolbar.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        toolbar.grid_columnconfigure(8, weight=1)

        self.run_button = ctk.CTkButton(toolbar, text="Start Task", command=self.start_task, width=110)
        self.run_button.grid(row=0, column=0, padx=(0, 8))
        self.begin_button = ctk.CTkButton(toolbar, text="Play / Space", command=self.request_start, width=110)
        self.begin_button.grid(row=0, column=1, padx=4)
        self.reset_button = ctk.CTkButton(toolbar, text="Reset / R", command=self.request_reset, width=96)
        self.reset_button.grid(row=0, column=2, padx=4)
        self.early_button = ctk.CTkButton(toolbar, text="Earlier / W", command=lambda: self.adjust_timing(-1), width=104)
        self.early_button.grid(row=0, column=3, padx=4)
        self.later_button = ctk.CTkButton(toolbar, text="Later / S", command=lambda: self.adjust_timing(1), width=96)
        self.later_button.grid(row=0, column=4, padx=4)
        ctk.CTkLabel(toolbar, textvariable=self.timeline_var, width=76).grid(row=0, column=5, padx=8)
        ctk.CTkLabel(toolbar, textvariable=self.status_var, anchor="e").grid(row=0, column=8, sticky="e")

        self.log = ctk.CTkTextbox(controls, wrap="word")
        self.log.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        self.log.configure(state="disabled")

    def _add_label(self, parent: ctk.CTkFrame, text: str, row: int, column: int) -> None:
        ctk.CTkLabel(parent, text=text, anchor="w").grid(row=row, column=column, padx=(12, 4), pady=8, sticky="w")

    def _bind_hotkeys(self) -> None:
        self.bind("<space>", lambda _event: self.request_start())
        self.bind("<Key-r>", lambda _event: self.request_reset())
        self.bind("<Key-R>", lambda _event: self.request_reset())
        self.bind("<Key-w>", lambda _event: self.adjust_timing(-1))
        self.bind("<Key-W>", lambda _event: self.adjust_timing(-1))
        self.bind("<Key-s>", lambda _event: self.adjust_timing(1))
        self.bind("<Key-S>", lambda _event: self.adjust_timing(1))

    def _load_song_records(self) -> None:
        try:
            self.song_records = song_search.load_song_records()
        except (OSError, ValueError) as exc:
            self.song_records = []
            self.song_labels = {}
            self.song_menu.configure(values=[f"Song list failed: {exc}"])
            self.song_select_var.set(f"Song list failed: {exc}")
            self._append_log(f"Song list load failed: {exc}\n")
            return

        self._refresh_song_options()
        self._append_log(f"Loaded {len(self.song_records)} songs from charts/all.1.json.\n")

    def _refresh_song_options(self) -> None:
        if not hasattr(self, "song_menu"):
            return
        if not self.song_records:
            self.song_labels = {}
            self.song_menu.configure(values=["Song list not loaded"])
            self.song_select_var.set("Song list not loaded")
            return

        records = song_search.filtered_song_records(self.song_records, self.song_search_var.get())
        if not records:
            self.song_labels = {}
            self.song_menu.configure(values=["No matching songs"])
            self.song_select_var.set("No matching songs")
            return

        self.song_labels = {record.label: record for record in records}
        labels = list(self.song_labels)
        self.song_menu.configure(values=labels)

        current_id = self.song_id_var.get().strip()
        current_label = next((record.label for record in records if record.id == current_id), "")
        selected_label = current_label or labels[0]
        self.song_select_var.set(selected_label)
        selected_record = self.song_labels[selected_label]
        if selected_record.id != current_id:
            self.song_id_var.set(selected_record.id)

    def _select_song(self, label: str) -> None:
        record = self.song_labels.get(label)
        if record is None:
            return
        self.song_id_var.set(record.id)

    def _update_adjust_controls(self) -> None:
        controls_enabled = (
            self.controls is not None
            and self.controls.enable_timeline_adjust
            and self.dynamic_adjust_var.get()
        )
        state = "normal" if controls_enabled else "disabled"
        self.early_button.configure(state=state)
        self.later_button.configure(state=state)

    def _set_config_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in self.config_widgets:
            widget.configure(state=state)

    def _set_playback_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.begin_button.configure(state=state)
        self.reset_button.configure(state=state)

    def _set_task_running(self, running: bool) -> None:
        self._set_config_enabled(not running)
        self.download_button.configure(state="disabled" if running else "normal")
        self._set_playback_controls_enabled(self.controls is not None and running)
        self._update_adjust_controls()

    def start_download(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self._append_log("Stop the auto-click task before downloading charts.\n")
            return
        if self.download_worker is not None and self.download_worker.is_alive():
            self._append_log("Chart download is already running.\n")
            return

        self.status_var.set("Downloading charts")
        self.download_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self._append_log("\n--- Downloading charts ---\n")
        self.download_worker = threading.Thread(
            target=self._run_download,
            daemon=True,
        )
        self.download_worker.start()

    def _run_download(self) -> None:
        stream = QueueTextStream(self.messages)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                counts = asyncio.run(
                    download_charts.download_all(
                        output_dir=Path("charts"),
                        concurrency=12,
                        overwrite=False,
                    )
                )
                print(
                    "Download finished: "
                    f"downloaded={counts['downloaded']} "
                    f"skipped={counts['skipped']} "
                    f"missing={counts['missing']} "
                    f"failed={counts['failed']}"
                )
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, OSError) as exc:
            self.messages.put(f"Download failed: {exc}\n")
        except Exception as exc:
            self.messages.put(f"Unexpected download error: {exc}\n")
        finally:
            self.after(0, self._mark_download_finished)

    def _mark_download_finished(self) -> None:
        self.download_button.configure(state="normal")
        self.run_button.configure(state="normal")
        self.status_var.set("Idle")
        self._load_song_records()

    def start_task(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.stop_task()
            return

        try:
            config = self._read_config()
        except ValueError as exc:
            self._append_log(f"Invalid config: {exc}\n")
            return

        self.status_var.set("Preparing")
        self.timeline_var.set("+0 ms")
        self.stop_requested = False
        self.run_button.configure(text="Stop Task", state="normal")
        self._set_task_running(True)
        self._append_log("\n--- Starting task ---\n")

        self.worker = threading.Thread(target=self._run_task, args=(config,), daemon=True)
        self.worker.start()

    def stop_task(self) -> None:
        self.stop_requested = True
        self.status_var.set("Stopping")
        self.run_button.configure(state="disabled")
        if self.controls is not None:
            self.controls.request_stop()
        self._set_playback_controls_enabled(False)
        self._update_adjust_controls()

    def _read_config(self) -> clicker.AutoClickConfig:
        song_text = self.song_id_var.get().strip()
        if not song_text:
            raise ValueError("Song ID is required.")

        return clicker.AutoClickConfig(
            song_id=int(song_text),
            difficulty=self.difficulty_var.get(),
            serial=self.serial_var.get().strip() or clicker.DEFAULT_SERIAL,
            timing_noise_ms=float(self.timing_noise_var.get() or 0),
            position_noise_px=float(self.position_noise_var.get() or 0),
            dynamic_adjust=self.dynamic_adjust_var.get(),
            ignore_last_note=self.ignore_last_note_var.get(),
        )

    def _run_task(self, config: clicker.AutoClickConfig) -> None:
        stream = QueueTextStream(self.messages)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                exit_code = self.service.play(
                    config,
                    controls_factory=self._create_controls,
                )
                print(f"Task finished with exit code {exit_code}.")
        except FileNotFoundError as exc:
            if clicker.is_chart_file_not_found(exc, config.song_id, config.difficulty):
                self.messages.put(clicker.chart_file_not_found_message(config.song_id, config.difficulty) + "\n")
            else:
                missing = exc.filename or clicker.DEFAULT_ADB
                self.messages.put(
                    f"Error: executable not found: {missing}. "
                    "Install Android platform-tools and add adb.exe to PATH.\n"
                )
        except (OSError, ValueError, clicker.AdbError) as exc:
            self.messages.put(f"Error: {exc}\n")
        except Exception as exc:
            self.messages.put(f"Unexpected error: {exc}\n")
        finally:
            self.controls = None
            self.stop_requested = False
            self.after(0, self._mark_idle)

    def _create_controls(self, enable_timeline_adjust: bool) -> clicker.PlaybackControls:
        controls = clicker.PlaybackControls(enable_timeline_adjust=enable_timeline_adjust)
        self.controls = controls
        if self.stop_requested:
            controls.request_stop()
        self.after(0, self._mark_ready)
        return controls

    def _mark_ready(self) -> None:
        if self.stop_requested:
            self.status_var.set("Stopping")
            return
        self.status_var.set("Ready: press Space or Play")
        self._set_task_running(True)

    def request_start(self) -> None:
        if self.controls is None:
            return
        self.controls.request_start()
        self.status_var.set("Playing")

    def request_reset(self) -> None:
        if self.controls is None:
            return
        self.controls.request_reset()
        self.status_var.set("Preparing")
        self._update_adjust_controls()

    def adjust_timing(self, direction: int) -> None:
        if self.controls is None:
            return
        if not self.controls.enable_timeline_adjust:
            return
        self.controls.adjust_timeline(direction * self.controls.adjust_step)
        current_ms = self.controls.timeline_adjust() * 1000
        self.timeline_var.set(f"{current_ms:+.0f} ms")

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_messages(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            self._append_log(message)
            if "Ready. Press Space" in message:
                self.status_var.set("Ready: press Space or Play")
            elif "Sent " in message:
                self.status_var.set("Finished")
        self.after(80, self._drain_messages)

    def _mark_idle(self) -> None:
        self.run_button.configure(text="Start Task", state="normal")
        self._set_task_running(False)
        if self.status_var.get() not in {"Finished"}:
            self.status_var.set("Idle")


def main() -> None:
    app = AutoClickApp()
    app.mainloop()


if __name__ == "__main__":
    main()
