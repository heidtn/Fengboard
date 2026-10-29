"""The main application window."""
from __future__ import annotations

import time

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox,
                               QComboBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QTableView,
                               QTabWidget, QVBoxLayout, QWidget)

from .audio import AudioRouter, PTTPort, list_serial_ports
from .chirp_handlers import (CHIRP_OK, COLUMNS, ChannelModel, CloneWorker,
                             ComboDelegate, chirp_directory)
from .constants import APP_NAME, DEFAULT_MODEL


class MainWindow(QMainWindow):
    # Re-emitted from the clone worker so handlers always run on the GUI thread.
    clone_finished = Signal(object, str)

    def __init__(self):
        super().__init__()
        self._clone_handler = None
        self.clone_finished.connect(self._on_clone_finished)
        self.setWindowTitle(APP_NAME)
        self.settings = QSettings("baofeng-panel", "baofeng-panel")
        self.ptt = PTTPort()
        self.audio = None
        self.audio_error = ""
        try:
            self.audio = AudioRouter()
        except Exception as e:  # noqa: BLE001
            self.audio_error = str(e)

        self.transmitting = False
        self.tx_started = 0.0
        self.channels_dirty = False
        self.clone_thread = None

        self.tx_timer = QTimer(self)
        self.tx_timer.setInterval(250)
        self.tx_timer.timeout.connect(self._tick_tx)

        central = QWidget()
        root = QVBoxLayout(central)
        root.addWidget(self._build_connection_bar())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_radio_tab(), "Radio")
        self.tabs.addTab(self._build_channels_tab(), "Channels")
        root.addWidget(self.tabs)
        self.setCentralWidget(central)
        self.statusBar()

        self.refresh_ports()  # also opens the PTT port
        self.refresh_audio_devices()
        if self.audio_error:
            self.status(f"Audio unavailable: {self.audio_error}")
        if not CHIRP_OK:
            self.status("CHIRP not found; channel editing disabled. "
                        "Install it from github.com/kk7ds/chirp.")

        self.resize(1000, 640)

    # ---------------------------------------------------------- UI builders
    def _build_connection_bar(self):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel("AIOC serial port:"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(380)
        self.port_combo.currentTextChanged.connect(self._open_ptt_port)
        h.addWidget(self.port_combo, 1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_ports)
        h.addWidget(b)
        h.addSpacing(16)
        h.addWidget(QLabel("Radio model:"))
        self.model_combo = QComboBox()
        if CHIRP_OK:
            ids = sorted(k for k, v in chirp_directory.DRV_TO_RADIO.items()
                         if v.VENDOR == "Baofeng")
            for k in ids:
                v = chirp_directory.DRV_TO_RADIO[k]
                self.model_combo.addItem(f"{v.VENDOR} {v.MODEL}", k)
            want = self.settings.value("model", DEFAULT_MODEL)
            i = self.model_combo.findData(want)
            self.model_combo.setCurrentIndex(max(i, 0))
        self.model_combo.currentIndexChanged.connect(
            lambda: self.settings.setValue("model", self.model_combo.currentData()))
        h.addWidget(self.model_combo)
        return box

    def _build_radio_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        audio = QGroupBox("Audio devices")
        form = QFormLayout(audio)
        self.rx_src_combo = QComboBox()
        self.rx_sink_combo = QComboBox()
        self.mic_combo = QComboBox()
        self.tx_sink_combo = QComboBox()
        form.addRow("Radio audio in (AIOC input):", self.rx_src_combo)
        form.addRow("Headphones / speakers:", self.rx_sink_combo)
        form.addRow("Microphone:", self.mic_combo)
        form.addRow("Radio audio out (AIOC output):", self.tx_sink_combo)
        rb = QPushButton("Refresh devices")
        rb.clicked.connect(self.refresh_audio_devices)
        form.addRow("", rb)
        for c in (self.rx_src_combo, self.rx_sink_combo,
                  self.mic_combo, self.tx_sink_combo):
            c.currentIndexChanged.connect(self._audio_selection_changed)
        v.addWidget(audio)

        listen = QHBoxLayout()
        self.listen_btn = QPushButton("Listen")
        self.listen_btn.setCheckable(True)
        self.listen_btn.setMinimumHeight(40)
        self.listen_btn.toggled.connect(self._toggle_listen)
        listen.addWidget(self.listen_btn)
        listen.addStretch(1)
        v.addLayout(listen)

        txbox = QGroupBox("Transmit")
        tv = QVBoxLayout(txbox)
        self.tx_label = QLabel("RECEIVE")
        f = QFont()
        f.setPointSize(20)
        f.setBold(True)
        self.tx_label.setFont(f)
        self.tx_label.setAlignment(Qt.AlignCenter)
        self.tx_label.setMinimumHeight(48)
        self._style_tx_label(False)
        tv.addWidget(self.tx_label)

        self.ptt_btn = QPushButton("PUSH TO TALK")
        self.ptt_btn.setMinimumHeight(80)
        pf = QFont()
        pf.setPointSize(16)
        pf.setBold(True)
        self.ptt_btn.setFont(pf)
        self.ptt_btn.pressed.connect(self._ptt_btn_pressed)
        self.ptt_btn.released.connect(self._ptt_btn_released)
        tv.addWidget(self.ptt_btn)

        opts = QHBoxLayout()
        self.toggle_mode = QCheckBox("Toggle mode (click to key, click again to unkey)")
        self.toggle_mode.setChecked(self.settings.value("toggle_mode", False, bool))
        self.toggle_mode.toggled.connect(lambda s: self.settings.setValue("toggle_mode", s))
        opts.addWidget(self.toggle_mode)
        opts.addStretch(1)
        opts.addWidget(QLabel("TX timeout (s):"))
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setValue(int(self.settings.value("tx_timeout", 120)))
        self.timeout_spin.valueChanged.connect(lambda s: self.settings.setValue("tx_timeout", s))
        opts.addWidget(self.timeout_spin)
        tv.addLayout(opts)
        v.addWidget(txbox)
        v.addStretch(1)
        return w

    def _build_channels_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.dl_btn = QPushButton("Download from radio")
        self.dl_btn.clicked.connect(self.download)
        self.ul_btn = QPushButton("Upload to radio")
        self.ul_btn.clicked.connect(self.upload)
        self.open_btn = QPushButton("Open image…")
        self.open_btn.clicked.connect(self.open_image)
        self.save_btn = QPushButton("Save image…")
        self.save_btn.clicked.connect(self.save_image)
        for b in (self.dl_btn, self.ul_btn, self.open_btn, self.save_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        self.add_btn = QPushButton("Add channel")
        self.add_btn.clicked.connect(self.add_channel)
        self.del_btn = QPushButton("Delete selected")
        self.del_btn.clicked.connect(self.delete_channels)
        self.show_empty_cb = QCheckBox("Show empty slots")
        bar.addWidget(self.add_btn)
        bar.addWidget(self.del_btn)
        bar.addWidget(self.show_empty_cb)
        v.addLayout(bar)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        v.addWidget(self.progress)

        self.chan_model = ChannelModel()
        self.chan_model.message.connect(self.status)
        self.chan_model.edited.connect(self._mark_dirty)
        self.show_empty_cb.toggled.connect(self.chan_model.set_show_empty)
        self.table = QTableView()
        self.table.setModel(self.chan_model)
        self.table.setItemDelegate(ComboDelegate(self.chan_model, self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.SelectedClicked)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COLUMNS.index("Name"), QHeaderView.Stretch)
        v.addWidget(self.table)

        hint = QLabel("Download first (this also gives you a backup to save). "
                      "Double-click a cell to edit. Changes stay local until you upload.")
        hint.setStyleSheet("color: gray;")
        v.addWidget(hint)
        self._update_channel_buttons()
        return w

    # ---------------------------------------------------------- helpers
    def status(self, msg):
        self.statusBar().showMessage(msg, 10000)

    def _style_tx_label(self, tx):
        if tx:
            self.tx_label.setStyleSheet(
                "background:#c62828; color:white; border-radius:6px;")
        else:
            self.tx_label.setStyleSheet(
                "background:#2e7d32; color:white; border-radius:6px;")

    def _update_channel_buttons(self):
        has = self.chan_model.radio is not None
        busy = self.clone_thread is not None
        self.dl_btn.setEnabled(CHIRP_OK and not busy)
        self.open_btn.setEnabled(CHIRP_OK and not busy)
        for b in (self.ul_btn, self.save_btn, self.add_btn, self.del_btn):
            b.setEnabled(CHIRP_OK and has and not busy)
        self.show_empty_cb.setEnabled(has)

    # ---------------------------------------------------------- serial port
    def refresh_ports(self):
        current = self.port_combo.currentText() or self.settings.value("port", "")
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        self.port_combo.addItems(list_serial_ports())
        i = self.port_combo.findText(current)
        if i >= 0:
            self.port_combo.setCurrentIndex(i)
        self.port_combo.blockSignals(False)
        self._open_ptt_port()

    def _open_ptt_port(self):
        if self.transmitting:
            self.stop_tx()
        port = self.port_combo.currentText()
        if not port or self.clone_thread is not None:
            self.ptt.close()
            return
        try:
            self.ptt.open(port)
            self.settings.setValue("port", port)
            self.status(f"PTT ready on {port}")
        except Exception as e:  # noqa: BLE001
            self.ptt.close()
            self.status(f"Cannot open {port}: {e} (are you in the 'dialout' group?)")

    # ---------------------------------------------------------- audio
    def refresh_audio_devices(self):
        combos = [self.rx_src_combo, self.rx_sink_combo, self.mic_combo, self.tx_sink_combo]
        if self.audio is None:
            for c in combos:
                c.setEnabled(False)
            self.listen_btn.setEnabled(False)
            return
        try:
            sources, sinks = self.audio.sources(), self.audio.sinks()
            dsink, dsrc = self.audio.default_sink(), self.audio.default_source()
        except Exception as e:  # noqa: BLE001
            self.status(f"Audio error: {e}")
            return

        def is_aioc(name):
            return "aioc" in name.lower()

        def fill(combo, items, key, candidates):
            """Select the saved device, else the first candidate present."""
            saved = self.settings.value(key, "")
            combo.blockSignals(True)
            combo.clear()
            for name, desc in items:
                combo.addItem(desc, name)
            names = [n for n, _ in items]
            pick = next((n for n in [saved, *candidates] if n and n in names), None)
            if pick:
                combo.setCurrentIndex(names.index(pick))
            combo.blockSignals(False)

        src_names = [n for n, _ in sources]
        sink_names = [n for n, _ in sinks]
        not_aioc_sinks = [n for n in sink_names if not is_aioc(n)]
        not_aioc_srcs = [n for n in src_names if not is_aioc(n)]
        fill(self.rx_src_combo, sources, "rx_source", [n for n in src_names if is_aioc(n)])
        fill(self.tx_sink_combo, sinks, "tx_sink", [n for n in sink_names if is_aioc(n)])
        fill(self.rx_sink_combo, sinks, "rx_sink",
             ([dsink] if dsink in not_aioc_sinks else []) + not_aioc_sinks)
        fill(self.mic_combo, sources, "mic",
             ([dsrc] if dsrc in not_aioc_srcs else []) + not_aioc_srcs)
        if dsink and is_aioc(dsink):
            self.status("Warning: your system default output is the AIOC. "
                        "System sounds would go to the radio's mic input.")

    def _audio_selection_changed(self):
        self.settings.setValue("rx_source", self.rx_src_combo.currentData())
        self.settings.setValue("rx_sink", self.rx_sink_combo.currentData())
        self.settings.setValue("mic", self.mic_combo.currentData())
        self.settings.setValue("tx_sink", self.tx_sink_combo.currentData())
        if self.listen_btn.isChecked():
            self._toggle_listen(True)

    def _toggle_listen(self, on):
        if self.audio is None:
            return
        try:
            if on:
                src, sink = self.rx_src_combo.currentData(), self.rx_sink_combo.currentData()
                if not src or not sink:
                    raise RuntimeError("choose radio input and headphones first")
                if "aioc" in (sink or "").lower():
                    raise RuntimeError("headphone output is set to the AIOC")
                self.audio.start_rx(src, sink)
                self.listen_btn.setText("Listening (click to stop)")
            else:
                self.audio.stop_rx()
                self.listen_btn.setText("Listen")
        except Exception as e:  # noqa: BLE001
            self.status(f"Listen failed: {e}")
            self.listen_btn.blockSignals(True)
            self.listen_btn.setChecked(False)
            self.listen_btn.blockSignals(False)
            self.listen_btn.setText("Listen")

    # ---------------------------------------------------------- transmit
    def start_tx(self):
        if self.transmitting:
            return
        if self.clone_thread is not None:
            self.status("Busy programming the radio; transmit disabled.")
            return
        if not self.ptt.is_open:
            self.status("No PTT serial port open.")
            return
        mic, radio_sink = self.mic_combo.currentData(), self.tx_sink_combo.currentData()
        if self.audio is not None and (not mic or not radio_sink):
            self.status("Choose a microphone and the AIOC output first.")
            return
        self.ptt.key(True)
        self.transmitting = True
        self.tx_started = time.monotonic()
        try:
            if self.audio is not None:
                self.audio.set_rx_muted(True)
                self.audio.start_tx(mic, radio_sink)
        except Exception as e:  # noqa: BLE001
            self.stop_tx()
            self.status(f"Transmit audio failed, unkeyed: {e}")
            return
        self._style_tx_label(True)
        self._tick_tx()
        self.tx_timer.start()

    def stop_tx(self, reason=""):
        if self.audio is not None:
            try:
                self.audio.stop_tx()
                self.audio.set_rx_muted(False)
            except Exception:  # noqa: BLE001
                pass
        self.ptt.key(False)
        was = self.transmitting
        self.transmitting = False
        self.tx_timer.stop()
        self.tx_label.setText("RECEIVE")
        self._style_tx_label(False)
        self.ptt_btn.setDown(False)
        if reason:
            self.status(reason)
        elif was:
            self.status("Unkeyed.")

    def _tick_tx(self):
        if not self.transmitting:
            return
        elapsed = time.monotonic() - self.tx_started
        limit = self.timeout_spin.value()
        if elapsed >= limit:
            self.stop_tx(f"TX timeout ({limit} s) - unkeyed automatically.")
            return
        m, s = divmod(int(elapsed), 60)
        self.tx_label.setText(f"TRANSMITTING  {m:02d}:{s:02d}")

    def _ptt_btn_pressed(self):
        if self.toggle_mode.isChecked():
            if self.transmitting:
                self.stop_tx()
            else:
                self.start_tx()
        else:
            self.start_tx()

    def _ptt_btn_released(self):
        if not self.toggle_mode.isChecked():
            self.stop_tx()

    # ---------------------------------------------------------- channels
    def _mark_dirty(self):
        self.channels_dirty = True
        self.tabs.setTabText(1, "Channels *")

    def _clear_dirty(self):
        self.channels_dirty = False
        self.tabs.setTabText(1, "Channels")

    def _on_clone_finished(self, radio, err):
        self._clone_done()
        handler, self._clone_handler = self._clone_handler, None
        if handler is not None:
            handler(radio, err)

    def _start_clone(self, worker, handler):
        if self.transmitting:
            self.stop_tx()
        self.ptt.close()  # release the port for CHIRP
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._clone_handler = handler
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._clone_progress)
        worker.finished.connect(self.clone_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self.clone_thread = thread
        self._clone_worker = worker
        self._update_channel_buttons()
        self.ptt_btn.setEnabled(False)
        thread.start()

    def _clone_progress(self, cur, mx, msg):
        self.progress.setMaximum(max(mx, 1))
        self.progress.setValue(cur)
        self.progress.setFormat(f"{msg}  %p%")

    def _clone_done(self):
        self.clone_thread = None
        self._clone_worker = None
        self.progress.setVisible(False)
        self.ptt_btn.setEnabled(True)
        self._update_channel_buttons()
        self._open_ptt_port()

    def download(self):
        port = self.port_combo.currentText()
        if not port:
            self.status("Choose the AIOC serial port first.")
            return
        if self.channels_dirty and QMessageBox.question(
                self, APP_NAME, "Discard unsaved channel edits and download?") != QMessageBox.Yes:
            return
        cls = chirp_directory.get_radio(self.model_combo.currentData())
        worker = CloneWorker(port, radio_cls=cls)

        def done(radio, err):
            if err:
                QMessageBox.warning(self, APP_NAME, f"Download failed:\n{err}\n\n"
                                    "Check the model, cable, and that the radio is on.")
                return
            self.chan_model.set_radio(radio)
            self._clear_dirty()
            self._update_channel_buttons()
            self.status("Downloaded. Tip: save an image now as a backup.")

        self._start_clone(worker, done)

    def upload(self):
        radio = self.chan_model.radio
        port = self.port_combo.currentText()
        if radio is None or not port:
            return
        if QMessageBox.question(
                self, APP_NAME,
                f"Upload all channels and settings to the radio on\n{port}?\n\n"
                "The radio will restart when done.") != QMessageBox.Yes:
            return
        worker = CloneWorker(port, radio=radio, upload=True)

        def done(_radio, err):
            if err:
                QMessageBox.warning(self, APP_NAME, f"Upload failed:\n{err}")
                return
            self._clear_dirty()
            self.status("Upload complete.")

        self._start_clone(worker, done)

    def open_image(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open CHIRP image", "", "CHIRP images (*.img);;All files (*)")
        if not path:
            return
        try:
            radio = chirp_directory.get_radio_by_image(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Could not open image:\n{e}")
            return
        self.chan_model.set_radio(radio)
        self._clear_dirty()
        self._update_channel_buttons()
        self.status(f"Opened {path} ({radio.VENDOR} {radio.MODEL}).")

    def save_image(self):
        radio = self.chan_model.radio
        if radio is None:
            return
        stamp = time.strftime("%Y%m%d-%H%M")
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save CHIRP image", f"{radio.MODEL}-{stamp}.img",
            "CHIRP images (*.img)")
        if not path:
            return
        try:
            radio.save_mmap(path)
            self.status(f"Saved {path}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Save failed:\n{e}")

    def add_channel(self):
        row = self.chan_model.add_channel()
        if row is None:
            self.status("No empty slots.")
            return
        idx = self.chan_model.index(row, 1)
        self.table.scrollTo(idx)
        self.table.setCurrentIndex(idx)
        self.table.edit(idx)

    def delete_channels(self):
        rows = [i.row() for i in self.table.selectionModel().selectedRows()]
        if not rows:
            return
        if QMessageBox.question(self, APP_NAME,
                                f"Erase {len(rows)} channel(s)? "
                                "(Not uploaded until you click Upload.)") != QMessageBox.Yes:
            return
        self.chan_model.erase_rows(rows)

    # ---------------------------------------------------------- shutdown
    def closeEvent(self, event):  # noqa: N802
        if self.clone_thread is not None:
            QMessageBox.information(self, APP_NAME, "Wait for programming to finish.")
            event.ignore()
            return
        if self.channels_dirty and QMessageBox.question(
                self, APP_NAME, "You have channel edits that were not uploaded. Quit anyway?"
        ) != QMessageBox.Yes:
            event.ignore()
            return
        self.shutdown()
        event.accept()

    def shutdown(self):
        try:
            self.stop_tx()
        finally:
            self.ptt.close()
            if self.audio is not None:
                self.audio.close()
