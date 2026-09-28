# -*- coding: utf-8 -*-
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import app.ling.romanizer as r

s = "I\u015e"
for i, ch in enumerate(s):
    low = ch.lower()
    print(i, hex(ord(ch)), "isupper:", ch.isupper(), "low:", hex(ord(low)),
          "in_rev:", low in r._NEW_LATIN_REVERSE, "map:", r._NEW_LATIN_REVERSE.get(low))
print("len:", len(s))
print("single :", repr(r.from_new_latin("\u015e")))
print("prefixI:", repr(r.from_new_latin(s)))
print("reverse table:", {hex(ord(k)): v for k, v in r._NEW_LATIN_REVERSE.items()})
