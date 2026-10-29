# FengBoard

A small PySide6 desktop panel for a Baofeng HT connected through an AIOC cable (Linux).

I've recently gotten into Ham radio and I like to have my Baofeng on just to listen.
I wanted a way to listen and speak on my radio through my computer. That way I could
use my PC headphones and mic *and* I can listen to music or other things while the channel 
is quiet. This app lets me do just that. I also added a CHIRP interface just for ease of access.

- **Listen** — routes the AIOC input (radio receive audio) to your headphones.
- **Push to talk** — hold the on-screen button (or click to lock it on in toggle mode).
- **Channels** — download, edit, back up (save/open `.img`), and upload channel memories
  using CHIRP's drivers.

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


## AI Disclaimer
This project was pretty heavily AI assisted (probably 90% AI). I've gone through all 
the code and there are a few areas where things are a bit messy and not architected well, 
but it's entirely functional and I haven't found any major issues to be worried about (yet).

## Status, Future Work, and Contributing
I've only tested on my BF-F8HP Pro, but everything seems to be in working order so far.

I think at this stage it does everything I want it to so I don't think I'll be adding additional
features, just maintaining and fixing bugs. I've considered adding digital modes however to make this
a sort of all in one tool, maybe riding off of [Pat](https://getpat.io/) but tbd.

Feel free to fork or submit a PR if you think you have something interesting, though I'd like to 
keep the scope minimal so I may not accept it if it's a substantial feature.