#!/usr/bin/env python3
"""Diagnostic: show the raw bytes your terminal sends for each key, exactly as the project prompt would receive them.

    python3 scripts/keys_probe.py kitty    # asks for the kitty keyboard protocol, like the prompt does
    python3 scripts/keys_probe.py xterm    # asks for xterm modifyOtherKeys
    python3 scripts/keys_probe.py plain    # no protocol

Press Shift+Enter, Ctrl+C, arrows ... each key prints one line. Press q (or Ctrl+D) to quit. Nothing is stored or sent.
"""
import os
import select
import sys
import termios
import tty

MODES = {"kitty": ("\x1b[>1u", "\x1b[<u"), "xterm": ("\x1b[>4;2m", "\x1b[>4;0m"), "plain": ("", "")}


def main(argv=None):
    mode = (argv or sys.argv[1:] or ["kitty"])[0]
    if mode not in MODES or not sys.stdin.isatty():
        print(__doc__)
        return 2
    fd, out = sys.stdin.fileno(), sys.stdout
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if mode == "kitty":
            out.write("\x1b[?u")
            out.flush()
            reply = b""
            while select.select([fd], [], [], .3 if not reply else .05)[0]:
                reply += os.read(fd, 1)
                if reply.endswith(b"u"):
                    break
            out.write("terminal answered the keyboard-protocol query: %r\r\n" % reply)
        out.write(MODES[mode][0] + "mode=%s; press keys, q quits\r\n" % mode)
        out.flush()
        while True:
            data = os.read(fd, 64)
            while select.select([fd], [], [], .03)[0]:
                data += os.read(fd, 64)
            out.write("%r\r\n" % data)
            out.flush()
            if data in (b"q", b"\x04"):
                break
    finally:
        out.write(MODES[mode][1])
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
