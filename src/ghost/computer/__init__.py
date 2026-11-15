"""Computer use: the Agent S3 sidecar ported from Stonic (Phase 2).

Ghost-side modules:

* :mod:`ghost.computer.protocol` — providers, coordinate conventions, OCR
  discovery. Pure stdlib; the sidecar imports it too.
* :mod:`ghost.computer.hud` — the HUD state machine.
* :mod:`ghost.computer.manager` — the supervisor that owns the sidecar.

``sidecar/`` holds the scripts that run *inside* the computer-use runtime venv
(``gui-agents``, ``pyautogui``). It is deliberately not a package: Ghost never
imports it, and its dependencies are not installed in Ghost's environment.

Everything is off unless ``[stonic] computer_use = true``.
"""
