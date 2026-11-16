"""
Stonic Wake Word Detector — Background Process
Listens for the character's own name (and for "Stonic") and emits JSON events
to stdout. Spawned by Electron main.js as a child process.

Usage: wake_word_bg.py [--name "Ayesha"]

--name  is the name the user gave their character (onboarding's "Their name"
        step). It arrives on argv so this script needs no settings file of its
        own; main.js restarts the process when the name changes.
"""

import sys
import json

# ── Dependency Guard ─────────────────────────────────────────────────────────
# If speech_recognition or pyaudio is missing, emit a clean error and exit
# instead of crashing silently.
try:
    import speech_recognition as sr
except ImportError:
    print(json.dumps({
        "type": "ERROR",
        "msg": "DEPENDENCY_MISSING: 'speech_recognition' is not installed. Run: pip install SpeechRecognition"
    }))
    sys.stdout.flush()
    sys.exit(1)

# PyAudio is required internally by speech_recognition for Microphone
try:
    import pyaudio  # noqa: F401
except ImportError:
    print(json.dumps({
        "type": "ERROR",
        "msg": "DEPENDENCY_MISSING: 'pyaudio' is not installed. Run: pip install pyaudio"
    }))
    sys.stdout.flush()
    sys.exit(1)


# ── Fuzzy Wake Word Matching ─────────────────────────────────────────────────
# Google Speech API sometimes mis-hears "Stonic" as similar words.
# We check if any word in the transcription is phonetically close.
WAKE_WORD_VARIANTS = {
    "stonic", "sonic", "tonic", "stonick", "stonik", "stdonic",
    "iconic", "chronic", "bionic", "demonic",
    "satanic", "stonics", "stonnick", "stink",
}


# ── The name the user gave their character ───────────────────────────────────
#
# A character the user named themselves should answer to that name: saying
# "Ayesha?" out loud is the whole point. The name comes in on argv (--name), so
# there is no settings file to read here and no way for a bad value to do
# anything but fail to match.
#
# Only the FIRST word of the name is used (people call each other by the first
# name), and anything under three letters is dropped: a two-letter word turns up
# so often in ordinary speech that the detector would fire all day.
def _custom_names(argv) -> set:
    names = set()
    for index, arg in enumerate(argv):
        if arg != "--name" or index + 1 >= len(argv):
            continue
        parts = argv[index + 1].strip().split()
        first = parts[0].lower() if parts else ""
        if len(first) >= 3:
            names.add(first)
    return names


CUSTOM_NAMES = _custom_names(sys.argv[1:])


def _is_custom_name(w: str) -> bool:
    for name in CUSTOM_NAMES:
        if w == name:
            return True
        # Speech-to-text drops or adds a letter at the end of a name often
        # enough ("ayesha" -> "ayeshaa", "aisha") that a little slack is worth
        # it — but only on names of four letters or more, where a near miss is
        # unlikely to be some other word.
        if len(name) >= 4 and abs(len(w) - len(name)) <= 2 and (
            w.startswith(name[:4]) or name.startswith(w[:4])
        ):
            return True
    return False


# Primary check: the character's own name, then "Stonic" and the words it is
# usually mis-heard as (anything ending in "nic" / "nick").
def is_wake_word(word: str) -> bool:
    w = word.lower().strip()
    if _is_custom_name(w):
        return True
    if w in WAKE_WORD_VARIANTS:
        return True
    if w.endswith("nic") or w.endswith("nick"):
        return True
    return False


def find_trigger_word(words: list) -> str | None:
    """Find the trigger word from a list of words. Returns the word or None."""
    for word in words:
        if is_wake_word(word):
            return word
    return None


def listen_for_stonic():
    r = sr.Recognizer()
    r.energy_threshold = 300
    r.dynamic_energy_threshold = True

    try:
        with sr.Microphone() as source:
            print(json.dumps({"type": "INFO", "msg": "Microphone initialized. Tuning ambient noise..."}))
            sys.stdout.flush()

            r.adjust_for_ambient_noise(source, duration=0.5)

            listening = " or ".join(sorted(CUSTOM_NAMES) + ["Stonic"])
            print(json.dumps({"type": "INFO", "msg": f"Listening in background for {listening}."}))
            sys.stdout.flush()

            while True:
                try:
                    audio = r.listen(source, phrase_time_limit=10)

                    text = r.recognize_google(audio).lower()
                    words = text.split()
                    trigger_word = find_trigger_word(words)

                    if trigger_word:
                        # Extract the command (everything after the trigger word)
                        command = text.replace(trigger_word, "", 1).strip()
                        result = {
                            "type": "WAKE_WORD",
                            "text": text,
                            "command": command
                        }
                        print(json.dumps(result))
                        sys.stdout.flush()

                except sr.UnknownValueError:
                    # No speech detected — this is normal, just continue listening
                    pass
                except sr.RequestError:
                    print(json.dumps({"type": "ERROR", "msg": "Google Speech API Connection Issue"}))
                    sys.stdout.flush()
                except Exception as e:
                    print(json.dumps({"type": "ERROR", "msg": str(e)}))
                    sys.stdout.flush()

    except OSError as e:
        # Microphone not found or audio device error
        print(json.dumps({
            "type": "ERROR",
            "msg": f"MICROPHONE_ERROR: {str(e)}. Make sure a microphone is connected."
        }))
        sys.stdout.flush()
        sys.exit(1)


if __name__ == "__main__":
    try:
        listen_for_stonic()
    except KeyboardInterrupt:
        sys.exit(0)
