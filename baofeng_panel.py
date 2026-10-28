#!/usr/bin/env python3
"""
Baofeng Panel - a small desktop control panel for a Baofeng HT on an AIOC cable.

  * Listen: routes the AIOC's input (radio receive audio) to your headphones.
  * Talk:   push-to-talk button or hold a hotkey; routes your mic to the AIOC's
            output and keys PTT over the AIOC serial port (DTR=1, RTS=0).
  * Channels: download, edit, back up and upload channel memories through
            CHIRP's radio drivers (clone mode, same serial port).

Linux only (PulseAudio or PipeWire with pipewire-pulse).

Dependencies:
    pip install PySide6 pulsectl pyserial
    pip install "git+https://github.com/kk7ds/chirp.git"   # the radio CHIRP,
                                     # NOT the unrelated "chirp" package on PyPI
"""
from __future__ import annotations

import builtins
import glob
import sys
import time
import traceback

# CHIRP's drivers call a global gettext function "_" that its own GUI installs.
if not hasattr(builtins, "_"):
    builtins._ = lambda s: s  # noqa: E731

import serial
import serial.tools.list_ports
from PySide6.QtCore import (QAbstractTableModel, QEvent, QModelIndex,
                            QObject, QSettings, Qt, QThread, QTimer, Signal)
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView,
                               QKeySequenceEdit, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QStyledItemDelegate,
                               QTableView, QTabWidget, QVBoxLayout, QWidget)

try:
    import pulsectl
except ImportError:  # handled at runtime with a message
    pulsectl = None

try:
    from chirp import chirp_common
    from chirp import directory as chirp_directory
    chirp_directory.import_drivers()
    CHIRP_OK = True
except Exception:  # noqa: BLE001
    CHIRP_OK = False

APP_NAME = "Baofeng Panel"
DEFAULT_MODEL = "Baofeng_BF-F8HP-PRO"
LOOPBACK_LATENCY_MS = 60


# --------------------------------------------------------------------------
# Audio routing (PulseAudio / PipeWire-pulse loopback modules)
# --------------------------------------------------------------------------
class AudioRouter:
    """Creates and removes module-loopback instances.

    The transmit path (mic -> AIOC output) only exists while PTT is keyed, so
    there is never a live mic feed into the radio otherwise.
    """

    def __init__(self):
        if pulsectl is None:
            raise RuntimeError("pulsectl is not installed (pip install pulsectl)")
        self.pulse = pulsectl.Pulse(APP_NAME.replace(" ", "-"))
        self.rx_module = None
        self.tx_module = None

    def sources(self):
        return [(s.name, s.description) for s in self.pulse.source_list()
                if not s.name.endswith(".monitor")]

    def sinks(self):
        return [(s.name, s.description) for s in self.pulse.sink_list()]

    def default_sink(self):
        return self.pulse.server_info().default_sink_name

    def default_source(self):
        return self.pulse.server_info().default_source_name

    def _load_loopback(self, source, sink):
        args = (f"source={source} sink={sink} latency_msec={LOOPBACK_LATENCY_MS} "
                "source_dont_move=true sink_dont_move=true")
        return self.pulse.module_load("module-loopback", args)

    def _unload(self, idx):
        if idx is None:
            return
        try:
            self.pulse.module_unload(idx)
        except Exception:  # noqa: BLE001 - already gone is fine
            pass

    def start_rx(self, source, sink):
        self.stop_rx()
        self.rx_module = self._load_loopback(source, sink)

    def stop_rx(self):
        self._unload(self.rx_module)
        self.rx_module = None

    def set_rx_muted(self, muted):
        if self.rx_module is None:
            return
        for si in self.pulse.sink_input_list():
            if si.owner_module == self.rx_module:
                self.pulse.sink_input_mute(si.index, muted)

    def start_tx(self, mic, radio_sink):
        self.stop_tx()
        self.tx_module = self._load_loopback(mic, radio_sink)

    def stop_tx(self):
        self._unload(self.tx_module)
        self.tx_module = None

    def close(self):
        self.stop_tx()
        self.stop_rx()
        try:
            self.pulse.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# PTT over the AIOC serial port
# --------------------------------------------------------------------------
def find_aioc_ports():
    return sorted(glob.glob("/dev/serial/by-id/*AIOC*"))


def list_serial_ports():
    ports = find_aioc_ports()
    for p in serial.tools.list_ports.comports():
        if p.device not in ports:
            ports.append(p.device)
    return ports


class PTTPort:
    """AIOC default firmware keys PTT when DTR=1 and RTS=0.

    The idle state is DTR=0, RTS=1, which is set *before* the port opens so
    the radio is never keyed by the act of opening it.
    """

    def __init__(self):
        self.ser = None

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def open(self, port):
        self.close()
        s = serial.Serial()
        s.port = port
        s.baudrate = 9600
        s.dtr = False
        s.rts = True
        s.open()
        self.ser = s

    def key(self, on):
        if not self.is_open:
            return
        if on:
            self.ser.rts = False
            self.ser.dtr = True
        else:
            self.ser.dtr = False
            self.ser.rts = True

    def close(self):
        if self.ser is not None:
            try:
                self.key(False)
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
        self.ser = None


# --------------------------------------------------------------------------
# CHIRP clone worker
# --------------------------------------------------------------------------
class CloneWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object, str)  # (radio or None, error message)

    def __init__(self, port, radio_cls=None, radio=None, upload=False):
        super().__init__()
        self.port, self.radio_cls, self.radio, self.upload = port, radio_cls, radio, upload

    def _status(self, st):
        self.progress.emit(int(st.cur), int(st.max), str(st.msg))

    def run(self):
        ser = None
        try:
            cls = type(self.radio) if self.upload else self.radio_cls
            ser = serial.Serial()
            ser.port = self.port
            ser.baudrate = getattr(cls, "BAUD_RATE", 9600)
            ser.timeout = 0.25
            ser.dtr = False   # keep PTT released during cloning
            ser.rts = True
            ser.open()
            if self.upload:
                radio = self.radio
                radio.pipe = ser
                radio.status_fn = self._status
                radio.sync_out()
            else:
                radio = cls(ser)
                radio.status_fn = self._status
                radio.sync_in()
            radio.pipe = None
            self.finished.emit(radio, "")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.finished.emit(None, str(e) or type(e).__name__)
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:  # noqa: BLE001
                    pass


# --------------------------------------------------------------------------
# Channel table
# --------------------------------------------------------------------------
def fmt_mhz(hz):
    s = f"{hz / 1e6:.6f}".rstrip("0")
    whole, _, frac = s.partition(".")
    return f"{whole}.{frac.ljust(3, '0')}"


COLUMNS = ["Loc", "Name", "Frequency", "Duplex", "Offset", "Tone Mode",
           "Tone", "ToneSql", "DTCS", "Mode", "Power", "Skip"]


class ChannelModel(QAbstractTableModel):
    message = Signal(str)
    edited = Signal()

    def __init__(self):
        super().__init__()
        self.radio = None
        self.features = None
        self.numbers = []
        self.show_empty = False

    # -- data loading
    def set_radio(self, radio):
        self.beginResetModel()
        self.radio = radio
        self.features = radio.get_features() if radio else None
        self._rebuild_numbers()
        self.endResetModel()

    def _rebuild_numbers(self):
        self.numbers = []
        if not self.radio:
            return
        lo, hi = self.features.memory_bounds
        for n in range(lo, hi + 1):
            if self.show_empty or not self.radio.get_memory(n).empty:
                self.numbers.append(n)

    def set_show_empty(self, show):
        self.beginResetModel()
        self.show_empty = show
        self._rebuild_numbers()
        self.endResetModel()

    def refresh(self):
        self.beginResetModel()
        self._rebuild_numbers()
        self.endResetModel()

    # -- options for combo-box columns
    def options(self, col):
        f = self.features
        if f is None:
            return None
        name = COLUMNS[col]
        if name == "Duplex":
            return list(f.valid_duplexes)
        if name == "Tone Mode":
            return list(f.valid_tmodes)
        if name in ("Tone", "ToneSql"):
            return [f"{t:.1f}" for t in chirp_common.TONES]
        if name == "DTCS":
            return [f"{c:03d}" for c in chirp_common.DTCS_CODES]
        if name == "Mode":
            return list(f.valid_modes)
        if name == "Power":
            return [str(p).split(" (")[0] for p in f.valid_power_levels]
        if name == "Skip":
            return list(f.valid_skips)
        return None

    # -- Qt model API
    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.numbers)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0:
            return base
        return base | Qt.ItemIsEditable

    def mem(self, row):
        return self.radio.get_memory(self.numbers[row])

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        m = self.mem(index.row())
        name = COLUMNS[index.column()]
        if name == "Loc":
            return str(m.number)
        if m.empty:
            return ""
        if name == "Name":
            return m.name.rstrip()
        if name == "Frequency":
            return fmt_mhz(m.freq)
        if name == "Duplex":
            return m.duplex
        if name == "Offset":
            return fmt_mhz(m.offset) if m.duplex in ("+", "-", "split") else ""
        if name == "Tone Mode":
            return m.tmode
        if name == "Tone":
            return f"{m.rtone:.1f}"
        if name == "ToneSql":
            return f"{m.ctone:.1f}"
        if name == "DTCS":
            return f"{m.dtcs:03d}"
        if name == "Mode":
            return m.mode
        if name == "Power":
            return str(m.power).split(" (")[0] if m.power else ""
        if name == "Skip":
            return m.skip
        return None

    def setData(self, index, value, role=Qt.EditRole):  # noqa: N802
        if role != Qt.EditRole or not index.isValid():
            return False
        m = self.mem(index.row())
        name = COLUMNS[index.column()]
        value = str(value).strip()
        try:
            if m.empty:
                if name != "Frequency":
                    self.message.emit("Set a frequency first for an empty slot.")
                    return False
                m.empty = False
                m.mode = "FM"
                m.power = self.features.valid_power_levels[0]
            if name == "Name":
                m.name = value[: self.features.valid_name_length]
            elif name == "Frequency":
                m.freq = chirp_common.parse_freq(value)
            elif name == "Duplex":
                m.duplex = value
                if value in ("+", "-") and not m.offset:
                    m.offset = 600000 if m.freq < 300_000_000 else 5_000_000
            elif name == "Offset":
                m.offset = chirp_common.parse_freq(value) if value else 0
            elif name == "Tone Mode":
                m.tmode = value
            elif name == "Tone":
                m.rtone = float(value)
            elif name == "ToneSql":
                m.ctone = float(value)
            elif name == "DTCS":
                m.dtcs = int(value)
            elif name == "Mode":
                m.mode = value
            elif name == "Power":
                for p in self.features.valid_power_levels:
                    if str(p).split(" (")[0] == value:
                        m.power = p
            elif name == "Skip":
                m.skip = value
        except (ValueError, TypeError) as e:
            self.message.emit(f"Invalid {name}: {e}")
            return False

        problems = self.radio.validate_memory(m)
        errors = [str(p) for p in problems
                  if isinstance(p, chirp_common.ValidationError)]
        if errors:
            self.message.emit("Rejected: " + "; ".join(errors))
            return False
        self.radio.set_memory(m)
        warnings = [str(p) for p in problems if p not in errors]
        self.message.emit("Warning: " + "; ".join(warnings) if warnings
                          else f"Channel {m.number} updated (not yet uploaded).")
        row = index.row()
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
        self.edited.emit()
        return True

    # -- channel operations
    def first_empty(self):
        lo, hi = self.features.memory_bounds
        for n in range(lo, hi + 1):
            if self.radio.get_memory(n).empty:
                return n
        return None

    def add_channel(self, freq_hz=146_520_000):
        n = self.first_empty()
        if n is None:
            return None
        m = self.radio.get_memory(n)
        m.empty = False
        m.freq = freq_hz
        m.mode = "FM"
        m.power = self.features.valid_power_levels[0]
        m.name = "NEW"
        self.radio.set_memory(m)
        self.refresh()
        self.edited.emit()
        return self.numbers.index(n)

    def erase_rows(self, rows):
        for r in sorted(set(rows), reverse=True):
            self.radio.erase_memory(self.numbers[r])
        self.refresh()
        self.edited.emit()


class ComboDelegate(QStyledItemDelegate):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model

    def createEditor(self, parent, option, index):  # noqa: N802
        opts = self.model.options(index.column())
        if opts is None:
            return super().createEditor(parent, option, index)
        cb = QComboBox(parent)
        cb.addItems(opts)
        return cb

    def setEditorData(self, editor, index):  # noqa: N802
        if isinstance(editor, QComboBox):
            i = editor.findText(index.data(Qt.EditRole) or "")
            editor.setCurrentIndex(max(i, 0))
            editor.showPopup()
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):  # noqa: N802
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText(), Qt.EditRole)
        else:
            super().setModelData(editor, model, index)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
TEXT_INPUT_TYPES = (QLineEdit, QKeySequenceEdit, QSpinBox, QComboBox)


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

        self.refresh_ports()
        self.refresh_audio_devices()
        self._update_hotkey_label()
        self._open_ptt_port()
        if self.audio_error:
            self.status(f"Audio unavailable: {self.audio_error}")
        if not CHIRP_OK:
            self.status("CHIRP not found; channel editing disabled. "
                        "Install it from github.com/kk7ds/chirp.")

        QApplication.instance().installEventFilter(self)
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
        opts.addWidget(QLabel("Hotkey:"))
        self.hotkey_edit = QKeySequenceEdit(
            QKeySequence(self.settings.value("hotkey", "Space")))
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setMaximumWidth(120)
        self.hotkey_edit.editingFinished.connect(self._hotkey_changed)
        opts.addWidget(self.hotkey_edit)
        opts.addSpacing(12)
        opts.addWidget(QLabel("TX timeout (s):"))
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setValue(int(self.settings.value("tx_timeout", 120)))
        self.timeout_spin.valueChanged.connect(lambda s: self.settings.setValue("tx_timeout", s))
        opts.addWidget(self.timeout_spin)
        tv.addLayout(opts)
        self.hotkey_label = QLabel()
        self.hotkey_label.setStyleSheet("color: gray;")
        tv.addWidget(self.hotkey_label)
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

    def _hotkey(self):
        seq = self.hotkey_edit.keySequence()
        if seq.isEmpty():
            return None
        return seq[0].key()

    def _hotkey_changed(self):
        self.settings.setValue("hotkey", self.hotkey_edit.keySequence().toString())
        self._update_hotkey_label()
        self.ptt_btn.setFocus()

    def _update_hotkey_label(self):
        k = self.hotkey_edit.keySequence().toString() or "(none)"
        self.hotkey_label.setText(
            f"Hold {k} to transmit while this window is focused "
            "(ignored while typing in a text field or editing the channel table).")

    def eventFilter(self, obj, event):  # noqa: N802
        et = event.type()
        if et in (QEvent.KeyPress, QEvent.KeyRelease):
            key = self._hotkey()
            if key is not None and event.key() == key and self.isActiveWindow():
                focus = QApplication.focusWidget()
                typing = isinstance(focus, TEXT_INPUT_TYPES) or (
                    self.table.state() == QAbstractItemView.EditingState)
                if not typing:
                    if event.isAutoRepeat():
                        return True
                    if et == QEvent.KeyPress:
                        if self.toggle_mode.isChecked() and self.transmitting:
                            self.stop_tx()
                        else:
                            self.start_tx()
                    elif not self.toggle_mode.isChecked():
                        self.stop_tx()
                    return True
        elif et == QEvent.ApplicationDeactivate and self.transmitting \
                and not self.toggle_mode.isChecked():
            # releasing a held key in another window would never reach us
            self.stop_tx("Window lost focus - unkeyed.")
        return super().eventFilter(obj, event)

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


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    app.aboutToQuit.connect(win.shutdown)
    import signal
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()  # lets Python see Ctrl+C while Qt runs
    tick.start(300)
    tick.timeout.connect(lambda: None)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
