# Baofeng Panel

A small PySide6 desktop panel for a Baofeng HT connected through an AIOC cable (Linux).

- **Listen** — routes the AIOC input (radio receive audio) to your headphones.
- **Push to talk** — hold the on-screen button or a hotkey (default Space). Your mic is
  routed to the AIOC output only while keyed; PTT is keyed over the AIOC serial port.
  Toggle mode, adjustable TX timeout, and automatic unkey if the window loses focus.
- **Channels** — download, edit, back up (save/open `.img`), and upload channel memories
  using CHIRP's drivers. Defaults to the BF-F8HP-PRO driver.

## Install

```bash
pip install PySide6 pulsectl pyserial
pip install "git+https://github.com/kk7ds/chirp.git"   # radio CHIRP; the PyPI "chirp" package is unrelated
sudo usermod -aG dialout $USER                         # serial access; log out and back in
python3 baofeng_panel.py
```

Requires PulseAudio, or PipeWire with `pipewire-pulse` (standard on current desktop distros).

## First run

1. Pick the AIOC serial port (the `/dev/serial/by-id/...AIOC...` entry) and confirm the model.
2. Check the four audio dropdowns. AIOC devices are pre-selected for radio in/out;
   your system defaults for headphones and mic. Choices are remembered.
3. **Channels → Download from radio**, then **Save image…** as a backup before editing.
4. Test transmit on low power or into a dummy load first. If you sound distorted,
   lower the AIOC output volume in `pavucontrol` (start around 30–40%).

## Notes and limits

- Channel changes are written by a full clone upload; the radio restarts afterward.
  The UV-5R family has no live CAT control, so channel/squelch changes can't be instant.
- PTT and programming share the serial port; transmit is disabled while programming.
- The hotkey only works while the window is focused and you aren't typing in a field.
- PTT assumes the AIOC's default firmware mapping (DTR=1, RTS=0 keys the radio).
