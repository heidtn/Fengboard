"""Audio routing (PulseAudio / PipeWire-pulse loopback modules) and PTT over
the AIOC serial port.
"""
from __future__ import annotations

import glob

import serial
import serial.tools.list_ports

from .constants import APP_NAME, LOOPBACK_LATENCY_MS

try:
    import pulsectl
except ImportError:  # handled at runtime with a message
    pulsectl = None


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
