# Contributing to snapclip

Thanks for considering it. Pull requests are welcome, from people and from
people working with AI models alike.

## What to expect

- **I review PRs when I have time.** If your PR has been sitting without a
  review, that does not mean it has been rejected. If it is good, it will
  land eventually. Feel free to submit and move on.
- **Credit goes with landed work only.** If your PR is merged, you stay the
  author or a co-author in the git history. If it is not merged, for example
  because it adds a feature this tool does not need, there is nothing to
  credit and it is simply closed. If you would rather not be credited for
  merged work, or want a credit removed later, contact me on GitHub.
- **Rework is normal.** If a PR is close but not quite right, I may land a
  reworked version rather than send it back and forth; you are credited on
  that commit.

## What this project is

snapclip is a minimal screenshot tool for GNOME/Wayland that does one thing:
capture a region without a screen flash and put it on the clipboard. Keeping
it small is the feature. Please keep that in mind before adding anything.

- **No bloat.** A new option or tool has to earn its place. If something can
  be done with what is already there, do that instead. Deleting code is
  welcome.
- **Off by default.** New tools and behaviours ship disabled and are enabled
  in the settings dialog. The stock toolbar stays exactly Copy / Save / Cancel.
- **Never flash.** Nothing may fall back to the GNOME screenshot path
  silently. The flash-free ScreenCast capture is the whole point.
- **Fast to launch.** Every hotkey press is a fresh process. Do not add work
  to the startup path without measuring it.

## Practical rules

- **GTK stays on the main thread.** The capture runs on a worker thread; it
  may talk D-Bus and GStreamer, never GDK/GTK.
- **No silent fallbacks.** If something fails, raise `CaptureError` with the
  real message or say so on stderr. Do not guess and carry on.
- **Tests must be deterministic.** Fake the D-Bus replies (see `_FakeBus` in
  `test_snapclip.py`) rather than depending on the monitors, config or files
  on your machine. Live checks are fine as an extra, not as the only test.
- **Run the suite:** `python3 test_snapclip.py` must pass, and
  `python3 snapclip.py --self-test copy` must pass on a GNOME Wayland session.
- **Update the docs** if behaviour changes: README, and a line in
  `CHANGELOG.md` under *Unreleased*.
- **Describe what you tested**, on what setup. If a model wrote most of the
  code, that is fine; just make sure it was actually run, not only written.

## Reporting problems

Open an issue with your GNOME version (`gnome-shell --version`), the exact
steps, and what happened instead. "Small selection then Cancel reproduces it
every time" is the kind of report that gets fixed.
