# Baofeng Panel

A small PySide6 desktop panel for a Baofeng HT connected through an AIOC cable (Linux).

- **Listen** — routes the AIOC input (radio receive audio) to your headphones.
- **Push to talk** — hold the on-screen button (or click to lock it on in toggle mode).
  Your mic is routed to the AIOC output only while keyed; PTT is keyed over the AIOC
  serial port. Adjustable TX timeout as a backstop against leaving it keyed.
- **Channels** — download, edit, back up (save/open `.img`), and upload channel memories
  using CHIRP's drivers. Defaults to the BF-F8HP-PRO driver.

## Install

```bash
pip install -e .
sudo usermod -aG dialout $USER   # serial access; log out and back in
python3 -m Fengboard             # or: baofeng-panel
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
- PTT is only triggered by the on-screen button — there is no keyboard hotkey, to
  avoid accidentally keying the radio.
- PTT assumes the AIOC's default firmware mapping (DTR=1, RTS=0 keys the radio).
