"""Memory Core's "ear" (Stonic port, phase 6): written and searched words meet
in one shape, so a change of spelling or script never loses a memory.

Ported from Stonic's ``memory/normalize.js``. Users mix Urdu (Arabic script),
Roman Urdu, Hindi (Devanagari) and English in one sentence, and Roman Urdu has
no fixed spelling ("acha / achha / accha", "nahi / nahin / nhi"). Full-text
search only matches identical words, so stored text AND queries both pass
through here.

Rule: consistency beats linguistics. One input always folds to one shape, even
if that shape is "wrong Urdu" — if "meeting" becomes "miting" on both sides,
search still matches.

🔴 Changing these rules means re-indexing old memories: fold/skel are stored in
the database, so a changed rule must come with a reindex migration.

Two layers:

* ``fold`` — same script, cleaned, spelling variants unified
  ("Mujhay chai pasand hay." -> "mujhe chai pasand hai")
* ``skel`` — each word's consonant skeleton in ASCII, one for all three scripts
  ("kitab" / "کتاب" / "किताब" -> "ktb")

fold is precise; skel is the net ("kal", "kala", "call" all become "kl"), so
ranking weighs fold higher.

Python's ``re`` has no ``\\p{L}``; letter / number / mark tests use
``unicodedata`` categories, with character scans where Stonic used Unicode
lookbehinds. The output is the same as Stonic's for the same input.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Set

# -- 1. cleanup (all three scripts) -------------------------------------------
#
# Characters are written as code-point ranges, not literally: zero-width and
# bidi characters are invisible in source, and an editor silently eating one
# would change the normaliser without anyone noticing.


def _cls(ranges: List[tuple]) -> str:
    out = []
    for r in ranges:
        lo = r[0]
        hi = r[1] if len(r) > 1 else lo
        out.append(f"\\u{lo:04x}" if lo == hi else f"\\u{lo:04x}-\\u{hi:04x}")
    return "".join(out)


# These never change a word's identity, they only break matches — removed.
_INVISIBLE = re.compile(
    "["
    + _cls(
        [
            (0x00AD,),  # soft hyphen
            (0x02BB, 0x02BC),  # letter-like apostrophes
            (0x0300, 0x036F),  # Latin combining marks
            (0x0610, 0x061A),
            (0x061C,),  # Arabic marks above
            (0x0640,),  # tatweel
            (0x064B, 0x065F),  # aeraab: zabar, zer, pesh, tanween, shadda, sukoon
            (0x0670,),  # standing alif
            (0x06D6, 0x06DC),
            (0x06DF, 0x06E4),
            (0x06E7, 0x06E8),
            (0x06EA, 0x06ED),  # Quranic marks
            (0x180E,),
            (0x200B, 0x200F),
            (0x202A, 0x202E),
            (0x2060, 0x2064),
            (0xFEFF,),  # zero-width and bidi
            (0x20D0, 0x20F0),
            (0xFE00, 0xFE0F),  # emoji modifiers
        ]
    )
    + "]"
)

# Arabic-keyboard Urdu uses different codes for yeh / kaf / heh that look the
# same — all brought to the Urdu form.
_LETTER_CANON: Dict[str, str] = {
    chr(a): chr(b)
    for a, b in [
        (0x064A, 0x06CC),
        (0x0649, 0x06CC),
        (0x06D0, 0x06CC),  # Arabic yeh forms -> ی
        (0x0643, 0x06A9),
        (0x06AA, 0x06A9),  # Arabic kaf -> ک
        (0x0647, 0x06C1),
        (0x0629, 0x06C1),
        (0x06C0, 0x06C1),  # Arabic heh, teh marbuta -> ہ
        (0x06C2, 0x06C1),
        (0x06C3, 0x06C1),
        (0x06D5, 0x06C1),
        (0x0623, 0x0627),
        (0x0625, 0x0627),
        (0x0671, 0x0627),  # hamza alif -> ا
        (0x06D3, 0x06D2),  # hamza bari ye -> ے
        (0x0901, 0x0902),  # chandrabindu / anusvara are used interchangeably
    ]
}
_LETTER_VARIANT = re.compile("[" + "".join(re.escape(k) for k in _LETTER_CANON) + "]")

_FOREIGN_DIGIT = re.compile(
    "[" + _cls([(0x0660, 0x0669), (0x06F0, 0x06F9), (0x0966, 0x096F)]) + "]"
)


def _ascii_digit(m: re.Match) -> str:
    c = ord(m.group(0))
    return str(c & 0xF if c < 0x0900 else c - 0x0966)


_LATIN_EXTENDED = re.compile("[" + _cls([(0x00C0, 0x024F), (0x1E00, 0x1EFF)]) + "]")
_COMBINING = re.compile("[" + _cls([(0x0300, 0x036F)]) + "]")
_LATIN_SPECIAL = {
    "ß": "ss",
    "æ": "ae",
    "œ": "oe",
    "ø": "o",
    "đ": "d",
    "ł": "l",
    "ı": "i",
    "þ": "th",
    "ð": "d",
}


def _plain_latin(m: re.Match) -> str:
    ch = m.group(0)
    return _LATIN_SPECIAL.get(ch) or _COMBINING.sub(
        "", unicodedata.normalize("NFD", ch)
    )


# "10,000" and "10000" are one amount; Indian grouping too ("1,50,000").
# Pairs only join when the last group has three digits.
_DIGIT_SEP = ",\\u060c\\u066c"
_THOUSANDS = re.compile(
    f"([0-9])[{_DIGIT_SEP}](?=(?:[0-9]{{2}}[{_DIGIT_SEP}]){{0,4}}[0-9]{{3}}(?![0-9]))"
)

_APOS = set("'`‘’´")
_ANY_APOS = _APOS | {"ʻ", "ʼ"}

_ASCII_NOT_WORD = re.compile(r"[^a-z0-9]+")
_COMMON_PUNCT = re.compile(
    "["
    + _cls(
        [
            (0x0000, 0x002F),
            (0x003A, 0x0040),
            (0x005B, 0x0060),
            (0x007B, 0x00BF),
            (0x0600, 0x060F),
            (0x061B, 0x061F),
            (0x066A, 0x066D),
            (0x06D4,),
            (0x06DD, 0x06DE),
            (0x06E9,),
            (0x06FD, 0x06FE),
            (0x0964, 0x0965),
            (0x0970,),
            (0x2000, 0x206F),
        ]
    )
    + "]+"
)
_UNKNOWN_CHAR = re.compile(
    "[^ a-z0-9"
    + _cls(
        [
            (0x0620, 0x064A),
            (0x066E, 0x066F),
            (0x0671, 0x06D3),
            (0x06D5,),
            (0x06EE, 0x06EF),
            (0x06FA, 0x06FC),
            (0x06FF,),
            (0x0750, 0x077F),
            (0x0900, 0x0963),
            (0x0971, 0x097F),
        ]
    )
    + "]"
)


def _is_letter(ch: str) -> bool:
    return unicodedata.category(ch).startswith("L")


def _is_word_char(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LNM"


def _strip_possessive(s: str) -> str:
    """ "Ali's birthday" -> "Ali birthday": letter + apostrophe + s, not followed by a word char."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if (
            ch in _ANY_APOS
            and i > 0
            and _is_letter(s[i - 1])
            and i + 1 < n
            and s[i + 1] == "s"
            and (i + 2 >= n or not _is_word_char(s[i + 2]))
        ):
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_inner_apostrophes(s: str) -> str:
    """don't -> dont, ma'loom -> maloom (apostrophe between two letters)."""
    if not any(c in _APOS for c in s):
        return s
    out = []
    for i, ch in enumerate(s):
        if (
            ch in _APOS
            and 0 < i < len(s) - 1
            and _is_letter(s[i - 1])
            and _is_letter(s[i + 1])
        ):
            continue
        out.append(ch)
    return "".join(out)


def _script_of_char(c: int) -> int:
    if (0x61 <= c <= 0x7A) or (0x30 <= c <= 0x39):
        return 1  # latin or digit
    if (0x0600 <= c <= 0x06FF) or (0x0750 <= c <= 0x077F):
        return 2
    if 0x0900 <= c <= 0x097F:
        return 3
    return 0


def _split_script_edges(s: str) -> str:
    """ "WhatsAppپر" / "5بجے": a space wherever the script changes."""
    out = []
    prev = 0
    for ch in s:
        cur = _script_of_char(ord(ch))
        if prev and cur and prev != cur:
            out.append(" ")
        out.append(ch)
        prev = cur
    return "".join(out)


def canon_words(text: object) -> List[str]:
    """Text -> list of clean words (lowercase, normalised within script, before folding)."""
    if not isinstance(text, str) or not text:
        return []
    s = unicodedata.normalize("NFKC", text).lower()
    if any(c in _ANY_APOS for c in s):
        s = _strip_possessive(s)
    if s.isascii():
        s = _THOUSANDS.sub(r"\1", s)
        s = _strip_inner_apostrophes(s)
        return [w for w in _ASCII_NOT_WORD.split(s) if w]
    s = _LATIN_EXTENDED.sub(_plain_latin, s)
    s = _INVISIBLE.sub("", s)
    s = _LETTER_VARIANT.sub(lambda m: _LETTER_CANON[m.group(0)], s)
    s = _FOREIGN_DIGIT.sub(_ascii_digit, s)
    s = _THOUSANDS.sub(r"\1", s)
    s = _strip_inner_apostrophes(s)
    s = _COMMON_PUNCT.sub(" ", s)
    if _UNKNOWN_CHAR.search(s):
        s = "".join(ch if _is_word_char(ch) else " " for ch in s)
    return [w for w in _split_script_edges(s).split(" ") if w]


NUMBER, LATIN, URDU, DEVANAGARI, OTHER = 0, 1, 2, 3, 4


def kind_of(word: str) -> int:
    for ch in word:
        c = ord(ch)
        if 48 <= c <= 57:
            continue
        if 97 <= c <= 122:
            return LATIN
        if (0x0600 <= c <= 0x06FF) or (0x0750 <= c <= 0x077F):
            return URDU
        if 0x0900 <= c <= 0x097F:
            return DEVANAGARI
        return OTHER
    return NUMBER


# -- 2. Roman Urdu folding -----------------------------------------------------
#
#   0. (done in cleanup) diacritics, possessive 's, inner apostrophes
#   1. exact word in the lexicon?            -> canonical, done ("q" -> kyun)
#   2. ph -> f; c before k / q / x drops     (ck -> k, acquire -> akuire)
#   3. q -> k, w -> v, x -> ks
#   4. aa -> a, ee -> i, oo -> u
#   5. runs of one letter -> one letter      (achha / accha -> acha), "ch" counts as one
#   6. final "ay" / "ey" -> "e", only on 5+ letter words
#   7. folded shape in the lexicon?          -> canonical (teek -> thik)
#
# Promise: folding a folded shape gives the same shape.


def phonetic(word: str) -> str:
    s = []
    i = 0
    n = len(word)
    while i < n:
        ch = word[i]
        if ch == "p" and i + 1 < n and word[i + 1] == "h":
            s.append("f")
            i += 2
            continue
        if ch == "c":
            j = i + 1
            while j < n and word[j] == "c":
                j += 1
            nxt = word[j] if j < n else ""
            i = j
            if nxt not in ("k", "q", "x"):
                s.append("c")
            continue
        if ch == "q":
            s.append("k")
        elif ch == "w":
            s.append("v")
        elif ch == "x":
            s.append("ks")
        else:
            s.append(ch)
        i += 1
    src = "".join(s)
    out: List[str] = []
    last = ""
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if "a" <= ch <= "z":
            end = i + 1
            while end < n and src[end] == ch:
                end += 1
            joined = "".join(out)
            if (
                ch == "c"
                and end < n
                and src[end] == "h"
                and last == "h"
                and joined.endswith("ch")
            ):
                i = end + 1
                continue
            if end - i > 1:
                if ch == "e":
                    ch = "i"
                elif ch == "o":
                    ch = "u"
                i = end - 1
            if ch == last:
                i += 1
                continue
        out.append(ch)
        last = ch
        i += 1
    res = "".join(out)
    if len(res) >= 5 and (res.endswith("ay") or res.endswith("ey")):
        base = res[:-2]
        res = base if base.endswith("e") else base + "e"
    return res


# Common function words' spellings -> one shape. Only sentence "glue"; never
# names of things or people. Deliberately absent: pr (PR), kb / tm (units),
# kiya -> kya (kiya means "did").
ROMAN_VARIANTS: Dict[str, str] = {
    "hai": "hay hy he hae",
    "hain": "hein hen hyn",
    "hun": "hoon hu houn",
    "tha": "thi the thay thy",
    "nahi": "nahin nai nhi nahe nhe nahein nhin",
    "kya": "kia",
    "kyun": "kyu kiun q kyon kiyun kion",
    "kyunki": "kyunke kyuki kyuke kiunke kiunki kyonki kyonke",
    "kaise": "kese kaisy kesy",
    "kaisa": "kesa",
    "kaisi": "kesi",
    "mein": "main me mai mei",
    "maine": "mene meine mne",
    "mujhe": "muje mujhy mjhe mjy",
    "mere": "mery",
    "apne": "apny",
    "uske": "usky",
    "iske": "isky",
    "unke": "unky",
    "tujhe": "tuje tujhy",
    "ke": "kay k ky",
    "se": "say sy",
    "aur": "or aor",
    "to": "tou toh",
    "wo": "woh voh",
    "ye": "yeh yh",
    "bhi": "bhy",
    "hum": "ham",
    "theek": "thik teek thek thk",
    "bohat": "bahut bhot bht boht bohot bahot buhat bohut",
    "acha": "achcha achchha",
    "raha": "rha",
    "rahi": "rhi",
    "rahe": "rhe rahy rhy",
    "kar": "kr",
    "karna": "krna",
    "karne": "krne krny",
    "karni": "krni",
    "karo": "kro",
    "karta": "krta",
    "karti": "krti",
    "karte": "krte krty",
    "karke": "krke",
    "karen": "kren karein karain",
    "liye": "liay lye liey",
    "liya": "lia lya",
    "diya": "dia dya",
    "diye": "diay",
    "kiye": "kiay kie",
    "gaya": "gya geya",
    "gayi": "gai gyi",
    "gaye": "gae gye gaey",
    "hua": "hwa huwa",
    "hue": "huay huey",
    "chahiye": "chahye chaiye chahie chaheye chahiay",
    "sakta": "skta",
    "sakti": "skti",
    "sakte": "skte",
    "phir": "fir phr pher",
    "lekin": "lkn laikin lakin",
    "agar": "agr",
    "sath": "sth",
    "pehle": "pahle phle pehly",
    "zyada": "ziada ziyada zyda zayada",
    "thora": "thoda",
    "thori": "thodi",
    "kuch": "kch kuj",
    "abhi": "abi",
    "sirf": "srf",
    "bilkul": "bikul blkl bilkol",
    "zaroor": "zrur zror",
    "shayad": "shyd shaid shayd",
    "jaldi": "jldi",
    "ghalat": "galat",
    "matlab": "mtlb matlb",
    "pata": "pta",
    "yahan": "yaha yhan",
    "wahan": "waha whan",
    "kaun": "kon koun",
    "kitna": "ktna",
    "kitni": "ktni",
    "kitne": "ktne",
    "shukriya": "shukria shukrya",
    "han": "hn",
    "please": "plz pls",
    "thanks": "thnx thx tnx thnks",
    "ok": "okay okey oky",
    "message": "msg",
}


def _build_variants() -> Dict[str, str]:
    table: Dict[str, str] = {}
    groups = [(phonetic(c), lst.split(" ")) for c, lst in ROMAN_VARIANTS.items()]
    for canon, lst in groups:
        for v in lst:
            if v != canon and v not in table:
                table[v] = canon
    for canon, lst in groups:
        for v in lst:
            key = phonetic(v)
            if key != canon and key not in table:
                table[key] = canon
    for canon, _ in groups:
        table.pop(canon, None)
    return table


_VARIANT_TO_CANON = _build_variants()


def fold_latin(word: str) -> str:
    direct = _VARIANT_TO_CANON.get(word)
    if direct is not None:
        return direct
    w = phonetic(word)
    return _VARIANT_TO_CANON.get(w, w)


_NUKTA = chr(0x093C)


def fold_word(word: str, kind: int) -> str:
    if kind == LATIN:
        return fold_latin(word)
    if kind == DEVANAGARI:
        return word.replace(_NUKTA, "") if _NUKTA in word else word
    return word


# -- 3. consonant skeleton -----------------------------------------------------

_LATIN_DROPPED = set("aeiouyvw")
_LATIN_ASPIRATED = set("bcdgjkrst")


def _skel_latin(w: str) -> str:
    out = ""
    i = 0
    n = len(w)
    while i < n:
        ch = w[i]
        if ch == "s" and w[i + 1 : i + 3] == "ch":
            add = "sk"
            i += 2
        elif ch == "t" and w[i + 1 : i + 4] == "ion":
            add = "sn"
            i += 3
        elif ch == "c":
            nxt = w[i + 1] if i + 1 < n else ""
            if nxt == "h":
                add = "c"
                i += 1
            else:
                add = "s" if nxt in ("e", "i", "y") else "k"
        elif ch in _LATIN_DROPPED:
            i += 1
            continue
        else:
            add = ch
        out += add
        if i + 1 < n and w[i + 1] == "h" and add[-1] in _LATIN_ASPIRATED:
            i += 1
        i += 1
    return out[:-1] if out.endswith("h") else out


_URDU_SKEL = {
    "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "c", "ح": "h", "خ": "k",
    "د": "d", "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "z", "س": "s", "ش": "s",
    "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "غ": "g", "ف": "f", "ق": "k", "ک": "k", "گ": "g",
    "ل": "l", "م": "m", "ن": "n", "ں": "n", "ہ": "h",
}  # fmt: skip
_DO_CHASHMI_HE = "ھ"
_PE = "پ"


def _skel_urdu(word: str) -> str:
    out = ""
    for i, ch in enumerate(word):
        if ch == _DO_CHASHMI_HE:
            if i > 0 and word[i - 1] == _PE:
                out = out[:-1] + "f"
            continue
        mapped = _URDU_SKEL.get(ch)
        if mapped:
            out += mapped
    return out[:-1] if out.endswith("h") else out


_DEVA_SKEL = {
    "क": "k", "ख": "k", "ग": "g", "घ": "g", "ङ": "n", "च": "c", "छ": "c", "ज": "j", "झ": "j", "ञ": "n",
    "ट": "t", "ठ": "t", "ड": "d", "ढ": "d", "ण": "n", "त": "t", "थ": "t", "द": "d", "ध": "d", "न": "n",
    "ऩ": "n", "प": "p", "फ": "f", "ब": "b", "भ": "b", "म": "m", "र": "r", "ऱ": "r", "ल": "l", "ळ": "l",
    "ऴ": "l", "श": "s", "ष": "s", "स": "s", "ह": "h", "ऋ": "r", "ृ": "r",
}  # fmt: skip
_DEVA_NUKTA_SKEL = {"ज": "z", "झ": "z", "ड": "r", "ढ": "r"}
_VIRAMA = chr(0x094D)
_ANUSVARA = chr(0x0902)
_DEVA_LABIAL = set("पफबभम")


def _skel_devanagari(word: str) -> str:
    out = ""
    joined = False
    i = 0
    n = len(word)
    while i < n:
        ch = word[i]
        if ch == _VIRAMA:
            joined = True
            i += 1
            continue
        if ch == _ANUSVARA:
            mapped = "m" if i + 1 < n and word[i + 1] in _DEVA_LABIAL else "n"
        elif ch == "ज" and word[i + 1 : i + 3] == _VIRAMA + "ञ":
            mapped = "g"
            i += 2
        elif i + 1 < n and word[i + 1] == _NUKTA:
            mapped = _DEVA_NUKTA_SKEL.get(ch) or _DEVA_SKEL.get(ch)
            i += 1
        else:
            mapped = _DEVA_SKEL.get(ch)
        if mapped and not (joined and out.endswith(mapped)):
            out += mapped
        joined = False
        i += 1
    return out[:-1] if out.endswith("h") else out


def skel_word(word: str, kind: int, folded: str) -> str:
    """A word's skeleton; shorter than 2 = '' (one letter matches everything)."""
    if kind == NUMBER:
        return word
    if kind == LATIN:
        s = _skel_latin(folded)
    elif kind == URDU:
        s = _skel_urdu(word)
    elif kind == DEVANAGARI:
        s = _skel_devanagari(word)
    else:
        s = ""
    return s if len(s) >= 2 else ""


# -- 4. stopwords --------------------------------------------------------------
#
# Only sentence glue. Day words (kal, aaj) are NOT stopwords. Words that fold
# into a name or thing are excluded: but (Butt), aap (app), mat (Matt), …

_STOPWORD_SOURCE = [
    "a the and or if of to in on at by for from with as is are was were be being",
    "have has had does did not no so than that this these those there here it its",
    "i me my we us our you your he him his she her they them their",
    "what which who whom when where why how can could would should",
    "about into over under again very just also too only",
    "im ive dont doesnt didnt cant wont isnt arent wasnt please ok yes hi hello hey",
    "hai hain hun ho hoga hogi honge hota hoti hote hona hua hui hue tha",
    "ka ki ke ko se ne par pe mein tak ya aur lekin magar agar to bhi hi na nahi",
    "kya kyun kyunki kaise kaisa kaisi kab kahan kaun kis kisi koi kuch kitna kitni kitne",
    "jo jab jaise jis ye wo is us in un yahan wahan aisa aise aisi itna itni itne",
    "mujhe mujh mujhko tujhe tum tu hum mera meri mere tera teri tere hamara hamari hamare",
    "maine tumne usne isne humne unhone inhone kisne jisne ga gi ge",
    "tumhara tumhari tumhare apka apki apke uska uski uske iska iski iske unka unki unke inka inki inke",
    "apna apni apne usko isko unko inko usay isay unhe unhen inhe inhen",
    "kar karna karne karni karo karta karti karte karke karen karun karunga karungi karenge kiya kiye",
    "raha rahi rahe gaya gayi gaye liya liye sakta sakti sakte chahiye wala wale",
    "phir bohat zyada sirf bas ab acha yaar ji han",
    "کا کی کے کو سے نے پر پہ میں تک یا اور لیکن مگر اگر تو بھی ہی نہ نہیں کہ",
    "ہے ہیں ہوں ہو ہوگا ہوگی ہوتا ہوتی ہوتے ہونا ہوا ہوئی ہوئے تھا تھی تھے تھیں گا گی گے",
    "کیا کیوں کیونکہ کیسے کیسا کیسی کب کہاں کون کس کسی کوئی کچھ کتنا کتنی کتنے",
    "جو جب جیسے جس یہ وہ اس ان یہاں وہاں ایسا ایسے ایسی اتنا اتنی اتنے",
    "مجھے مجھ تجھے تم تو آپ ہم میرا میری میرے تیرا تیری تیرے ہمارا ہماری ہمارے",
    "تمہارا تمہاری تمہارے اپنا اپنی اپنے اسے انہیں انہوں",
    "کر کرنا کرنے کرنی کرو کرتا کرتی کرتے کرکے کریں کروں کیے",
    "رہا رہی رہے گیا گئی گئے لیا لیے لئے سکتا سکتی سکتے چاہیے چاہئے والا والی والے",
    "پھر بہت زیادہ صرف اب اچھا یار جی ہاں",
    "का की के को से ने पर पे में तक या और लेकिन मगर अगर तो भी ही न नहीं कि",
    "है हैं हूँ हो होगा होगी होता होती होते होना हुआ हुई हुए था थी थे थीं गा गी गे मैं नही",
    "क्या क्यों क्योंकि कैसे कैसा कैसी कब कहाँ कौन किस किसी कोई कुछ कितना कितनी कितने",
    "जो जब जैसे जिस यह ये वह वो इस उस इन उन यहाँ वहाँ ऐसा ऐसे ऐसी इतना इतनी इतने",
    "मुझे मुझ तुझे तुम तू आप हम मेरा मेरी मेरे तेरा तेरी तेरे हमारा हमारी हमारे",
    "तुम्हारा तुम्हारी तुम्हारे अपना अपनी अपने उसे उन्हें इन्हें",
    "कर करना करने करनी करो करता करती करते करके करें करूँ किया किए किये",
    "रहा रही रहे गया गई गयी गए गये लिया लिए लिये सकता सकती सकते चाहिए वाला वाली वाले",
    "फिर बहुत ज़्यादा सिर्फ़ अब अच्छा यार जी हाँ",
]

STOPWORDS: Set[str] = set()
for _line in _STOPWORD_SOURCE:
    for _w in canon_words(_line):
        STOPWORDS.add(fold_word(_w, kind_of(_w)))


# -- 5. public functions -------------------------------------------------------

_MIXED_SHARE = 0.2


def _is_arabic_letter(c: int) -> bool:
    return (
        (0x0620 <= c <= 0x064A)
        or (0x066E <= c <= 0x06D5 and c not in (0x0670, 0x06D4))
        or (0x06EE <= c <= 0x06EF)
        or (0x06FA <= c <= 0x06FF)
        or (0x0750 <= c <= 0x077F)
        or (0xFB50 <= c <= 0xFDFF)
        or (0xFE70 <= c <= 0xFEFC)
    )


def _is_devanagari_letter(c: int) -> bool:
    return (
        (0x0904 <= c <= 0x0939)
        or c in (0x093D, 0x0950)
        or (0x0958 <= c <= 0x0961)
        or (0x0972 <= c <= 0x097F)
    )


def _is_latin_letter(c: int) -> bool:
    return (
        (97 <= c <= 122)
        or (65 <= c <= 90)
        or (0x00C0 <= c <= 0x024F and c not in (0x00D7, 0x00F7))
        or (0x1E00 <= c <= 0x1EFF)
    )


def detect_script(text: object) -> str:
    """Script by letter count; two scripts at 20 %+ each = "mixed"."""
    if not isinstance(text, str) or not text:
        return "other"
    counts = {"urdu": 0, "devanagari": 0, "latin": 0, "other": 0}
    for ch in text:
        c = ord(ch)
        if c < 0x80:
            if _is_latin_letter(c):
                counts["latin"] += 1
        elif _is_arabic_letter(c):
            counts["urdu"] += 1
        elif _is_devanagari_letter(c):
            counts["devanagari"] += 1
        elif _is_latin_letter(c):
            counts["latin"] += 1
        elif (
            not (0x0600 <= c <= 0x06FF)
            and not (0x0900 <= c <= 0x097F)
            and _is_letter(ch)
        ):
            counts["other"] += 1
    total = sum(counts.values())
    if total == 0:
        return "other"
    order = ["urdu", "devanagari", "latin", "other"]
    if sum(1 for k in order if counts[k] / total >= _MIXED_SHARE) >= 2:
        return "mixed"
    return max(order, key=lambda k: (counts[k], -order.index(k)))


def fold_text(text: object) -> str:
    return " ".join(fold_word(w, kind_of(w)) for w in canon_words(text))


def skeleton(text: object) -> str:
    out = []
    for w in canon_words(text):
        k = kind_of(w)
        s = skel_word(w, k, fold_latin(w) if k == LATIN else w)
        if s:
            out.append(s)
    return " ".join(out)


@dataclass(frozen=True, slots=True)
class Normalized:
    fold: str
    skel: str
    script: str


def normalize_for_search(text: object) -> Normalized:
    """Store and search both go through this — all three in one pass."""
    folds: List[str] = []
    skels: List[str] = []
    for w in canon_words(text):
        k = kind_of(w)
        f = fold_word(w, k)
        folds.append(f)
        s = skel_word(w, k, f)
        if s:
            skels.append(s)
    return Normalized(" ".join(folds), " ".join(skels), detect_script(text))


@dataclass(frozen=True, slots=True)
class QueryToken:
    raw: str
    fold: str
    skel: str


def query_tokens(text: object) -> List[QueryToken]:
    """A query's meaningful words; ``skel`` may be '' (short word: search by fold only)."""
    out: List[QueryToken] = []
    seen: Set[str] = set()
    for w in canon_words(text):
        k = kind_of(w)
        f = fold_word(w, k)
        if k != NUMBER and (len(w) < 2 or len(f) < 2):
            continue
        if f in STOPWORDS or f in seen:
            continue
        seen.add(f)
        out.append(QueryToken(w, f, skel_word(w, k, f)))
    return out


def content_hash(text: object) -> str:
    """Duplicate detection: spelling, spacing and punctuation don't change the hash."""
    return hashlib.sha256(fold_text(text).replace(" ", "").encode("utf-8")).hexdigest()


__all__ = [
    "STOPWORDS",
    "Normalized",
    "QueryToken",
    "canon_words",
    "content_hash",
    "detect_script",
    "fold_text",
    "normalize_for_search",
    "query_tokens",
    "skeleton",
]
