"""CHIRP integration: cloning a radio over the AIOC serial port, and the
channel-table model/delegate used to display and edit the result.
"""
from __future__ import annotations

import builtins
import traceback
import logging

# CHIRP's drivers call a global gettext function "_" that its own GUI installs.
if not hasattr(builtins, "_"):
    builtins._ = lambda s: s  # noqa: E731

import serial
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, Signal
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate

try:
    from chirp import chirp_common
    from chirp import directory as chirp_directory
    chirp_directory.import_drivers()
    CHIRP_OK = True
except Exception:  # noqa: BLE001
    CHIRP_OK = False

# Chirp uses a custom class with a log function, we're faking it here
class ChirpSerial(serial.Serial):
    def log(self, message):
        logging.getLogger("baofeng_panel.serial").debug(message)

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
            ser = ChirpSerial()
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
