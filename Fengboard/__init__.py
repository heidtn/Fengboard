"""Fengboard - a small desktop control panel for a Baofeng HT on an AIOC cable.

  * Listen: routes the AIOC's input (radio receive audio) to your headphones.
  * Talk:   push-to-talk button or hold a hotkey; routes your mic to the AIOC's
            output and keys PTT over the AIOC serial port (DTR=1, RTS=0).
  * Channels: download, edit, back up and upload channel memories through
            CHIRP's radio drivers (clone mode, same serial port).

Linux only (PulseAudio or PipeWire with pipewire-pulse).
"""

__version__ = "0.1.0"
