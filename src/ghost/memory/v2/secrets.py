"""Memory Core's gatekeeper: secrets out of a conversation BEFORE it is written.

Ported from Stonic's ``memory/secrets.js``. Memory Core keeps every turn
verbatim and forever, in a plain SQLite file — so a password, OTP or card
number must never reach the disk; once written it travels on in every backup.

The opposite harm is just as bad: phone numbers, dates, prices, years, order
numbers and addresses are LEGITIMATE memories, and wiping them is a bug. So the
rule is "when in doubt, leave it": digits alone never get redacted; there must
be evidence — arithmetic (Luhn, mod-97), a known shape (CNIC dashes, a key
prefix) or a nearby word ("otp", "pin", "password").

The placeholder ``[redacted <kind>]`` is a contract: it stays in the database
and later versions read it, so its shape never changes (new kinds are fine).

Porting notes (JS -> Python ``re``): JS ``\\d`` / ``\\b`` / ``\\w`` are ASCII and
JS ``\\s`` is Unicode, so patterns compile with ``re.ASCII`` and use an explicit
JS-whitespace class; ``\\p{L}`` / ``\\p{M}`` / ``\\p{N}`` are built from
``unicodedata``; JS ``$`` (never before a final newline) is ``\\Z``; sticky
``y`` regexes are ``pattern.match(s, pos)``. A differential test runs this
module against Stonic's original on a generated corpus.
"""

from __future__ import annotations

import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, List, Optional, Tuple

KINDS = (
    "card",
    "cvv",
    "otp",
    "password",
    "api_key",
    "private_key",
    "cnic",
    "iban",
    "bank_account",
)

# Two rules on one span: the more certain one names it.
_RANK = {
    "private_key": 0, "api_key": 1, "iban": 2, "cnic": 3, "bank_account": 4,
    "card": 5, "cvv": 6, "otp": 7, "password": 8,
}  # fmt: skip


def placeholder(kind: str) -> str:
    return f"[redacted {kind}]"


_PLACEHOLDER = re.compile(r"\[redacted [a-z_]+\]")
# An existing placeholder is masked while scanning — otherwise the "otp" inside
# "[redacted otp]" would make the next number an OTP. It is also a wall: a mask
# between a keyword and a number means the number is not that keyword's.
MASK = "\ufffc"
# Removing one secret can expose another ("pin 1234 <card>"), so scanning
# repeats until nothing new turns up ("run twice, same result").
_MAX_PASSES = 6

_F = re.ASCII | re.IGNORECASE
_FA = re.ASCII


# -- Unicode classes -----------------------------------------------------------


@lru_cache(maxsize=None)
def _ucls(cats: str) -> str:
    """Character-class body for Unicode general categories (first letters, e.g. "LMN")."""
    ranges: List[Tuple[int, int]] = []
    start = -1
    prev = -2
    for cp in range(sys.maxunicode + 1):
        if unicodedata.category(chr(cp))[0] in cats:
            if cp != prev + 1:
                if start >= 0:
                    ranges.append((start, prev))
                start = cp
            prev = cp
    if start >= 0:
        ranges.append((start, prev))

    def esc(c: int) -> str:
        return f"\\U{c:08x}"

    return "".join(esc(a) if a == b else f"{esc(a)}-{esc(b)}" for a, b in ranges)


_LMN = _ucls("LMN")
_LM = _ucls("LM")
# JS \s (Unicode whitespace), for use inside classes.
_WS = "\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"
_S = f"[{_WS}]"
_NS = f"[^{_WS}]"

# JS \b only knows English letters; Urdu and Hindi words need their own edge.
_WB = f"(?<![{_LMN}_])"
_WE = f"(?![{_LM}_])"  # a digit may stick: "pin1234"
_WEN = f"(?![{_LMN}_])"

# Three Urdu letters arrive with other codes from an Arabic keyboard.
_K = "[کك]"
_Y = "[یي]"
_H = "[ہهھ]"

_COPULA = f"is|was|hai|hay|hy|he|{_H}ے|है"
_POSSESSIVE = f"your|my|the|mera|meri|apka|aapka|aap[ \\t]ka|tumhara|hamara|humara|م{_Y}را|آپ[ \\t]{_K}ا|मेरा|आपका"


def _words(text: str) -> frozenset:
    return frozenset(w for w in text.split() if w)


# -- preparation ---------------------------------------------------------------

_LOOKALIKE = re.compile(
    "[\u0660-\u0669\u06f0-\u06f9\u0966-\u096f\u00a0\u2007\u2009\u202f\u2010-\u2013\u2212]"
)


def _lookalike(m: re.Match) -> str:
    c = ord(m.group(0))
    if c in (0x00A0, 0x2007, 0x2009, 0x202F):
        return " "
    if c >= 0x2010:
        return "-"
    zero = 0x0966 if c >= 0x0966 else 0x06F0 if c >= 0x06F0 else 0x0660
    return chr(48 + c - zero)


def _prepare(text: str) -> str:
    """Foreign digits, NBSP and long dashes -> ASCII, one for one (offsets keep)."""
    s = text
    if "[redacted " in s:
        s = _PLACEHOLDER.sub(lambda m: MASK * len(m.group(0)), s)
    return _LOOKALIKE.sub(_lookalike, s)


_WORD_BEFORE = re.compile(f"([{_LMN}]+)[ \\t-]*\\Z", _FA)
_LINKERS = frozenset(["ka", "ki", "ke", "کا", "کی", "کے", "का", "की", "के"])


def _word_before(s: str, at: int, skip_linker: bool = False) -> str:
    win = s[max(0, at - 40) : at]
    m = _WORD_BEFORE.search(win)
    if not m:
        return ""
    word = m.group(1).lower()
    if not skip_linker or word not in _LINKERS:
        return word
    prior = _WORD_BEFORE.search(win[: m.start()])
    return prior.group(1).lower() if prior else word


# -- quantity, not a secret ----------------------------------------------------

_MONEY_BEFORE = re.compile(
    r"(?:rs\.?|pkr|inr|usd|rupees|rupay|روپے|रुपये|[$₨₹€£])[ \t]*\Z", _F
)
_NEXT_WORD = re.compile(f"^[ \\t]*(%|[{_LM}]+)", _FA)
_UNIT_WORDS = _words(
    """
  rupees rupee rupay rupaye rupiya rupaiye rs pkr inr usd dollars dollar percent
  saal sal year years month months mahine mahina din day days ghante ghanta hour hours hrs
  minute minutes min mins second seconds sec secs baje bajay am pm
  lines line users user log dafa baar times km kg gb mb px items item characters chars digits
  روپے سال بجے فیصد دن منٹ रुपये साल बजे दिन मिनट"""
)
_YEAR = re.compile(r"^(?:19|20)\d\d\Z", _FA)
_AFTER_YEAR = _words(
    """
  mein main me men se tak ka ki ke ko wala wali
  میں سے تک کا کی کے کو में से तक का की के को"""
)


def _looks_like_quantity(s: str, start: int, end: int) -> bool:
    if _MONEY_BEFORE.search(s[max(0, start - 12) : start]):
        return True
    m = _NEXT_WORD.search(s[end : end + 24])
    if not m:
        return False
    nxt = m.group(1).lower()
    return (
        nxt == "%"
        or nxt in _UNIT_WORDS
        or (nxt in _AFTER_YEAR and bool(_YEAR.search(s[start:end])))
    )


Add = Callable[[int, int, str], None]

# -- private keys (PEM) --------------------------------------------------------

_PEM_BEGIN = re.compile(r"-----BEGIN ([A-Z0-9 ]{0,40}?)PRIVATE KEY( BLOCK)?-----", _FA)
_PEM_LOOSE_BODY = re.compile(f"(?:{_S}*[A-Za-z0-9+/=]{{16,}})*", _FA)


def _find_private_keys(s: str, add: Add) -> None:
    if "-----BEGIN" not in s:
        return
    no_end: set = set()
    pos = 0
    while True:
        m = _PEM_BEGIN.search(s, pos)
        if not m:
            return
        end_line = f"-----END {m.group(1)}PRIVATE KEY{m.group(2) or ''}-----"
        end = -1
        if end_line not in no_end:
            at = s.find(end_line, m.end())
            if at == -1:
                no_end.add(end_line)
            else:
                end = at + len(end_line)
        if end == -1:
            body = _PEM_LOOSE_BODY.match(s, m.end())
            end = max(m.end(), body.end() if body else m.end())
        add(m.start(), end, "private_key")
        pos = end


# -- API keys ------------------------------------------------------------------

_KEY_PREFIXED = re.compile(
    "(?<![A-Za-z0-9_-])(?:"
    + "|".join(
        [
            r"sk-[A-Za-z0-9_-]{20,}",
            r"(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}",
            r"whsec_[A-Za-z0-9+/=_-]{16,}",
            r"AIza[A-Za-z0-9_-]{30,}",
            r"gh[pous]_[A-Za-z0-9]{20,}",
            r"github_pat_[A-Za-z0-9_]{20,}",
            r"xox[bpa]-[A-Za-z0-9-]{10,}",
            r"AKIA[A-Z0-9]{16}(?![A-Za-z0-9])",
            r"polar_[A-Za-z0-9_]{20,}",
            r"hf_[A-Za-z0-9]{20,}",
            r"gsk_[A-Za-z0-9]{20,}",
            r"sb_secret_[A-Za-z0-9_-]{16,}",
            r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*",
        ]
    )
    + ")",
    _FA,
)
# These prefixes look like words ("polar_bear_photos_..."): they need a digit too.
_KEY_WORDY_PREFIX = re.compile(r"^(?:sk-|polar_|hf_)", _FA)
_KEY_GENERIC = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}", _FA)
_KEY_MIN_ENTROPY = 3.5
# Hex has 16 symbols; its entropy cannot pass 4.0, so it gets its own bar.
_KEY_MIN_ENTROPY_HEX = 3.0
_HEX_ONLY = re.compile(r"^(?:0x)?[0-9a-fA-F]+\Z", _FA)
_UUID_INSIDE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _F
)
_WORDLIKE_PART = re.compile(r"^(?:[A-Za-z]+\d{0,4}|\d+|\d{1,4}[A-Za-z]{1,5})\Z", _FA)
_WORD_CHUNK = re.compile(r"[A-Z]?[a-z]{3,}", _FA)
_FILE_EXT_AFTER = re.compile(r"^\.[A-Za-z][A-Za-z0-9]{1,4}(?![A-Za-z0-9])", _FA)
_HASH_LABEL_BEFORE = re.compile(
    f"\\b(?:commit|sha-?\\d*|md5|hash|checksum)\\b[{_WS}:=#]{{0,3}}\\Z", _F
)


def _entropy(text: str) -> float:
    counts: dict = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _made_of_words(token: str) -> bool:
    return all(
        not part or _WORDLIKE_PART.match(part) for part in re.split(r"[-_]+", token)
    )


def _looks_random(token: str) -> bool:
    rest = _UUID_INSIDE.sub("", token) if "-" in token else token
    if (
        len(rest) < 32
        or not re.search(r"[A-Za-z]", rest)
        or not re.search(r"\d", rest, _FA)
    ):
        return False
    if _made_of_words(rest):
        return False
    # A long code name ("handleGen3SubscriptionRenewalWebhookEvent").
    if len(re.findall(r"\d+", rest, _FA)) <= 2:
        in_words = sum(len(c) for c in _WORD_CHUNK.findall(rest))
        if in_words >= len(re.sub(r"[^A-Za-z]", "", rest)) * 0.8:
            return False
    return _entropy(rest) >= (
        _KEY_MIN_ENTROPY_HEX if _HEX_ONLY.match(rest) else _KEY_MIN_ENTROPY
    )


def _find_api_keys(s: str, add: Add, has_digit: bool) -> None:
    for m in _KEY_PREFIXED.finditer(s):
        tok = m.group(0)
        if _KEY_WORDY_PREFIX.match(tok) and (
            not re.search(r"\d", tok, _FA) or _made_of_words(tok)
        ):
            continue
        add(m.start(), m.end(), "api_key")
    if not has_digit:
        return
    for m in _KEY_GENERIC.finditer(s):
        end = m.end()
        if not _looks_random(m.group(0)):
            continue
        if _FILE_EXT_AFTER.match(s[end : end + 8]):
            continue
        if _HASH_LABEL_BEFORE.search(s[max(0, m.start() - 16) : m.start()]):
            continue
        add(m.start(), end, "api_key")


# -- passwords -----------------------------------------------------------------
#
# The word after "password" is often not a secret ("password bhool gaya",
# "password is wrong"). Three levels:
#   ":" or "="        -> anything except ordinary words
#   "is" / "hai" first -> a token with digits/symbols always; a plain word only
#                         when the clause ends there ("password is sunshine.")
#   directly / "X hai" -> only a token with digits/symbols; a plain word only in
#                         the full form "mera password sunshine hai"

_PW_ASSIGNED = r"(?=[\"']?[ \t]*[:=])"
_PW_KEYWORD = re.compile(
    f"(?:{_WB}(?:pass?w(?:o|a)?rd|pass[ -]word|pass[ -]?code|passphrase|پاس[{_WS}]?ورڈ|پاسورڈ|पासवर्ड|"
    f"(?:passwd|pwd|pass|pw){_PW_ASSIGNED})"
    f"|(?:(?<=[A-Za-z0-9_])(?:password|passwd)|(?<=_)(?:pwd|pass)){_PW_ASSIGNED}){_WE}",
    _F,
)
_PW_HEAD = re.compile(
    f"[\"'”’]?[ \\t]*(?:(?:for|of)[ \\t]+(?:[^{_WS}.!?:=]{{1,24}}[ \\t]+){{1,4}}?)?"
    f"(?:(?P<cop>{_COPULA}){_WEN}(?:[ \\t]*(?P<csym>[:=]+))?|(?P<sym>[:=]+[->]?|[-–—]+>?(?=[ \\t])))[ \\t]*",
    _F,
)
_PW_TOKEN = re.compile(
    f"\"([^\"\\n]{{1,80}})\"|'([^'\\n]{{1,80}})'|“([^”\\n]{{1,80}})”|`([^`\\n]{{1,80}})`|({_NS}{{1,129}})",
    _FA,
)
_PW_CLOSE = re.compile(f"[ \\t]+(?:hai|hay|hy|he|tha|{_H}ے|تھا|है|था){_WEN}", _F)
_PW_CLAUSE_END = re.compile(
    f"^[ \\t]*(?:\\Z|[\\n.,;!?۔،]|(?:and|aur|or|ya|but|lekin|magar|so|then|phir|hai|اور|{_H}ے|और|है){_WEN})",
    _F,
)
_PW_HAS_SIGNAL = re.compile(r"[\d!@#$%^&*+=?~|\\_]", _FA)
_PW_NOT_A_VALUE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://|www\.|[a-z]:[\\/]|\.{0,2}/|~/|[*•●·]+\Z|\d+%\Z|\d+(?:[/:.,-]\d+)+\Z)",
    _F,
)
_PW_PARTICLE = re.compile(
    f"^(?:[ \\t]+(?:to|toh|tou|ye|yeh|yah|تو|{_Y}{_H}|तो|यह|ये)(?=[ \\t])){{1,2}}", _F
)
_EMAIL = re.compile(f"^[^{_WS}@]+@[^{_WS}@]+\\.[A-Za-z]{{2,}}\\Z", _FA)
_PW_MAX = 128
_PW_TAIL = re.compile(r"[)\]}>\"'”’.,;:۔،]+\Z", _FA)
_PW_TAIL_LOUD = re.compile(r"[)\]}>\"'”’.,;:۔،!?]+\Z", _FA)
_PW_CODE_EXPR = re.compile(
    r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*[([]|^[A-Za-z_$]+(?:\.[A-Za-z_$]+)+\Z",
    _FA,
)
_PW_PHONE = re.compile(r"^(?:0|92|0092)3\d{9}\Z", _FA)
_PW_SENT_TO = re.compile(f"^[ \\t]+(?:par|pe|per|ko|پر|{_K}و|पर|को){_WEN}", _F)
_PW_SELF = re.compile(r"^pass(?:word|code)?\Z", _FA)
_PW_MORE_DIGITS = re.compile(r"(?:[ -]\d+)+", _FA)
# "postgres://user:PASSWORD@host" — the "://name:x@" shape is the evidence.
_URL_CREDENTIALS = re.compile(f"://[^{_WS}:@/]{{0,64}}:([^{_WS}/]{{3,128}})@", _FA)
_PW_LEAD = re.compile(r"^[(\"'“‘\[{<]+", _FA)
_PW_NEXT_PARAM = re.compile(r"&[A-Za-z_][\w.-]*=", _FA)

_PW_ORDINARY = _words(
    """
  the a an my your his her their our its this that these those it he she we they you me
  not no yes too very so also still now just only always never ever what which where when why how who
  same different there here in on at for of to with by from as like than then and or but if because
  being been be am are was were is going gone get got set kept stored saved written required needed need
  protected encrypted hashed case sensitive something nothing anything everything unknown known
  secret private public personal confidential important compromised leaked stolen hacked expired
  changed updated reset shared lost forgotten forgot working fine done ready available null none undefined
  correct incorrect wrong invalid valid right true false empty blank missing hidden visible long short
  old new bad good weak strong simple easy hard secure safe unsafe optional mandatory default temporary
  random unique please pls thanks ok okay sure probably maybe actually really already mine yours
  hai hain tha thi ho hoga hogi hota hoti hua hui gaya gayi kya kia kyun kyu kab kahan kidhar kaise
  kaisa kaun kon kis jis jo wo woh ye yeh yah us un mera meri mere tera teri tere tumhara tumhari
  apka aapka apna apni apne uska uski iska iski inka unka hamara humara sabka koi kuch sab har ek
  nahi nahin nhi nai na mat haan han ji galat ghalat sahi theek thik purana purani naya nayi lamba
  chota bara mushkil asaan asan kamzor mazboot mehfooz yaad yad bhool bhul maloom malum pata zaroori
  lazmi wohi wahi yehi yahi aisa waisa itna bohat bohot bahut kafi thora bilkul sirf bas abhi ab phir
  bhi hi toh tou lekin magar aur ya par pe mein main ko se ka ki ke tak liye wala wali bhai yaar yar
  dost sir janab shayad zaroor change badal badlo badla badalna likho likha likh batao bata btao bta
  bataya dalo daalo dala daal lagao laga lagaya rakho rakha rakh bhejo bheja bhej send do de dena dedo
  diya dia karo kar karna karni kiya chahiye chahye mila dekho dekha yahan wahan alag khali gayab band
  save leak hack expire block blocked lock locked open closed enabled disabled on off pehle baad
  string str text varchar char boolean bool number int any object type nil
  show hide view copy paste enter click tap dikhao chupao dabao"""
)
_PW_OWNER = _words(
    """
  mera meri mere my our apna apni hamara humara tumhara apka aapka ka ki ke
  میرا میری اپنا ہمارا کا کی मेरा मेरी अपना हमारा का की"""
)


def _is_ordinary(token: str) -> bool:
    lower = token.lower()
    if lower in _PW_ORDINARY:
        return True
    parts = [p for p in re.split(f"[{_WS}-]+", lower) if p]
    return len(parts) > 1 and all(p in _PW_ORDINARY or _PW_SELF.match(p) for p in parts)


def _find_passwords(s: str, add: Add) -> None:
    for m in _PW_KEYWORD.finditer(s):
        at = m.end()
        particle = _PW_PARTICLE.match(s[at : at + 16])
        if particle:
            at += len(particle.group(0))
        head = _PW_HEAD.match(s, at)
        if head:
            at = head.end()
        else:
            gap = re.match(r"^[ \t]+", s[at : at + 8])
            if not gap:
                continue
            at += len(gap.group(0))
        t = _PW_TOKEN.match(s, at)
        if not t:
            continue
        raw_end = at + len(t.group(0))
        quoted = t.group(5) is None
        token = t.group(0)[1:-1] if quoted else t.group(5)
        start = at + 1 if quoted else at
        sym = (head.group("sym") or "") if head else ""

        if not quoted:
            lead = _PW_LEAD.match(token)
            if lead:
                token = token[len(lead.group(0)) :]
                start += len(lead.group(0))
            if head and sym and "=" in sym:
                nxt = _PW_NEXT_PARAM.search(token)
                if nxt and nxt.start() > 0:
                    token = token[: nxt.start()]
            core = _PW_TAIL_LOUD.sub("", token)
            token = _PW_TAIL.sub("", token) if _PW_HAS_SIGNAL.search(core) else core
        if (
            not token
            or len(token) > _PW_MAX
            or MASK in token
            or _PW_NOT_A_VALUE.match(token)
        ):
            continue

        end = start + len(token)
        tail = raw_end
        signal = bool(_PW_HAS_SIGNAL.search(token))
        digits_only = bool(re.match(r"^\d+\Z", token, _FA))
        if digits_only:
            more = _PW_MORE_DIGITS.match(s, end)
            if more:
                end = more.end()
                tail = end
            if (
                end - start < 4
                or end - start > _PW_MAX
                or _looks_like_quantity(s, start, end)
            ):
                continue
        split = end - start > len(token)
        ordinary = _is_ordinary(token)
        assign = (head.group("sym") or head.group("csym") or "") if head else ""

        if assign:
            if re.match(r"^={2,}", assign):
                continue
            if not quoted and "=" in assign and _PW_CODE_EXPR.match(token):
                continue
            ok = (
                len(token) >= 3
                and not ordinary
                and (signal or not re.match(r"^[^\x00-\x7F]+\Z", token))
            )
        elif head:
            if signal:
                ok = len(token) >= 3
            elif quoted:
                ok = not ordinary
            else:
                ok = (
                    len(token) >= 3
                    and not ordinary
                    and bool(re.match(r"^[A-Za-z'’-]+\Z", token))
                    and (
                        raw_end > end
                        or bool(_PW_CLAUSE_END.match(s[raw_end : raw_end + 12]))
                    )
                )
        else:
            closed = bool(_PW_CLOSE.match(s, tail))
            if signal:
                ok = len(token) >= (
                    3 if closed else 4 if digits_only else 6
                ) and not _EMAIL.match(token)
                if (
                    digits_only
                    and not closed
                    and (
                        split
                        or _PW_PHONE.match(token)
                        or _PW_SENT_TO.match(s[end : end + 8])
                    )
                ):
                    ok = False
            elif quoted:
                ok = (
                    len(token) >= 3
                    and not ordinary
                    and (closed or _word_before(s, m.start()) in _PW_OWNER)
                )
            else:
                ok = (
                    closed
                    and len(token) >= 4
                    and not ordinary
                    and bool(re.match(r"^[A-Za-z]+\Z", token))
                    and _word_before(s, m.start()) in _PW_OWNER
                )
        if ok:
            add(start, end, "password")

    if "://" not in s:
        return
    for m in _URL_CREDENTIALS.finditer(s):
        start = m.end() - 1 - len(m.group(1))
        if MASK not in m.group(1):
            add(start, start + len(m.group(1)), "password")


# -- cards ---------------------------------------------------------------------

_DIGIT_RUN = re.compile(r"\d(?:[ -]?\d)*", _FA)
# Real cards start with these (Visa 4, Mastercard 5 and 22–27, Amex 3, UnionPay 6).
_CARD_START = re.compile(r"^(?:2[2-7]|[3-6])", _FA)
_CARD_GROUPINGS = frozenset(["4-4-4-4", "4-4-4-4-3", "4-6-5", "4-6-4"])
# An IMEI is 15 digits and passes Luhn too — but it is the phone's identity.
_IMEI_BEFORE = re.compile(r"\bimei\b[^\d\n]{0,20}\Z", _F)


def _luhn(digits: str) -> bool:
    total = 0
    double = False
    for ch in reversed(digits):
        n = ord(ch) - 48
        if double:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        double = not double
    return total % 10 == 0


def _card_shape(groups: List[Tuple[int, int]], lo: int, hi: int, whole: bool) -> bool:
    count = hi - lo + 1
    if count == 1:
        return True
    if not whole and count > 5:
        return False
    lens = [groups[i][1] for i in range(lo, hi + 1)]
    if "-".join(map(str, lens)) in _CARD_GROUPINGS:
        return True
    size = lens[0]
    if not whole or size > 4:
        return False
    return all(
        (ln == size) if i < count - 1 else (ln <= size) for i, ln in enumerate(lens)
    )


def _find_cards(s: str, add: Add) -> None:
    for m in _DIGIT_RUN.finditer(s):
        run = m.group(0)
        if len(run) < 13:
            continue
        at = m.start()
        prev = s[at - 1] if at > 0 else ""
        nxt = s[at + len(run)] if at + len(run) < len(s) else ""
        if re.match(r"[A-Za-z_+]", prev) or re.match(r"[A-Za-z_]", nxt):
            continue
        if prev in (".", ",") and at >= 2 and "0" <= s[at - 2] <= "9":
            continue
        groups: List[Tuple[int, int]] = []
        group_at = 0
        for i in range(len(run) + 1):
            if i == len(run) or run[i] in (" ", "-"):
                groups.append((group_at, i - group_at))
                group_at = i + 1
        if len(run) - (len(groups) - 1) < 13:
            continue
        if _IMEI_BEFORE.search(s[max(0, at - 28) : at]):
            continue
        i = 0
        while i < len(groups):
            lead = ord(run[groups[i][0]]) - 48
            if lead < 2 or lead > 6:
                i += 1
                continue
            ends: List[int] = []
            digits = 0
            j = i
            while j < len(groups) and digits + groups[j][1] <= 19:
                digits += groups[j][1]
                if digits >= 13:
                    ends.append(j)
                j += 1
            for j in reversed(ends):
                if not _card_shape(groups, i, j, i == 0 and j == len(groups) - 1):
                    continue
                frm = groups[i][0]
                to = groups[j][0] + groups[j][1]
                number = re.sub(r"[ -]", "", run[frm:to])
                if not _CARD_START.match(number) or not _luhn(number):
                    continue
                add(at + frm, at + to, "card")
                i = j
                break
            i += 1


# -- CVV -----------------------------------------------------------------------

_CVV_KEYWORD = re.compile(
    f"{_WB}(?:cvv2?|cvc2?|cv2|security[ -]?code|card[ -]?verification[ -]?(?:code|value|number)|"
    f"س{_Y}[{_WS}]?و{_Y}[{_WS}]?و{_Y}|س{_Y}?{_K}{_Y}ورٹ{_Y}[{_WS}]?{_K}وڈ|सीवीवी|सिक्योरिटी[{_WS}]?कोड){_WE}",
    _F,
)
_CVV_AFTER = re.compile(
    r"[^\d\n.!?۔,;،؛\ufffc]{0,16}?(\d{3,4})(?![ -]?\d)(?![.,:/]\d)(?![A-Za-z_])", _FA
)


def _find_cvvs(s: str, add: Add) -> None:
    for m in _CVV_KEYWORD.finditer(s):
        hit = _CVV_AFTER.match(s, m.end())
        if not hit:
            continue
        end = hit.end()
        start = end - len(hit.group(1))
        if not _looks_like_quantity(s, start, end):
            add(start, end, "cvv")


# -- OTP / PIN -----------------------------------------------------------------
#
# Strong keywords ("otp", "passcode", "verification code", "atm pin") take a
# code a little further on in the same sentence. Weak ones (bare "code",
# "pin") need the number right there, with only "is / hai / number / :"
# between. Some "codes" are never secret (promo, area, zip, error…).

_OTP_PREFIX = (
    f"verification|confirmation|security|login|auth|authentication|2fa|sms|activation|otp|atm|upi|debit|credit|card|sim|transaction|"
    f"تصد{_Y}ق{_Y}|و{_Y}ر{_Y}ف{_Y}{_K}{_Y}شن|س{_Y}{_K}{_Y}ورٹ{_Y}|सत्यापन|वेरिफिकेशन|सिक्योरिटी|एटीएम|यूपीआई"
)
_OTP_KEYWORD = re.compile(
    f"{_WB}(?:"
    f"(?P<strong>o\\.?[ ]?t\\.?[ ]?p|one[ -]?time[ -]?(?:password|passcode|pin|code)|pass[ -]?code|[mt]-?pin|"
    f"او[{_WS}]?ٹ{_Y}[{_WS}]?پ{_Y}|ون[{_WS}]?ٹائم[{_WS}]?(?:پاس[{_WS}]?ورڈ|{_K}وڈ|پن)|پاس[{_WS}]?{_K}وڈ|"
    f"ओ[{_WS}]?टी[{_WS}]?पी|वन[{_WS}]?टाइम[{_WS}]?(?:पासवर्ड|कोड|पिन)|पासकोड)"
    f"|(?:(?P<pre>{_OTP_PREFIX})[ -]+)?(?:(?P<postal>pin[ -]?code|پن[{_WS}]?{_K}وڈ|पिन[{_WS}]?कोड)|(?P<weak>code|pin|{_K}وڈ|پن|कोड|पिन))"
    f"){_WE}",
    _F,
)
_CODE_NOT_SECRET = _words(
    """
  promo coupon discount voucher referral refer invite area zip postal post country dialing dial city
  std branch swift sort ifsc hs product item sku model part color colour hex employee student course
  subject tracking booking error status exit response http source qr bar morse dress cheat machine
  binary html css python javascript js java vs studio visual
  پرومو کوپن ڈسکاؤنٹ ایریا زپ پوسٹل پوسٹ کنٹری ایرر سورس آر بار برانچ
  प्रोमो कूपन डिस्काउंट एरिया ज़िप जिप पोस्टल कंट्री एरर सोर्स क्यूआर बार ब्रांच"""
)
_OTP_WINDOW = 48
_SENTENCE_END = re.compile(f"[!?۔\\n\\ufffc]|(?<!\\b(?:rs|no))\\.(?={_S}|\\Z)", _F)
_COPULA_BEFORE = re.compile(f"(?:\\b(?:is|hai|hay|hy)|[ہهھ]ے|है|[:=]){_S}*\\Z", _F)
_CODE_SHAPE = r"(\d{3}-\d{3}|\d(?: ?\d){3,7})(?![ -]?\d)(?![.,:/]\d)(?![A-Za-z_])"
_CODE_ANYWHERE = re.compile(r"(?<![\dA-Za-z_+])(?<!\d[ \-.,:/])" + _CODE_SHAPE, _FA)
_CODE_HERE = re.compile(_CODE_SHAPE, _FA)
_WEAK_FOR = f"(?:[ \\t]+(?:for|of)(?:[ \\t]+[^{_WS}.!?:=\\d]{{1,24}}){{1,3}}?(?=[ \\t]+(?:is|hai|hay){_WEN}))?"
_WEAK_GAP = re.compile(
    f"{_WEAK_FOR}(?:[ \\t]*[:=#-]+)?(?:[ \\t]+(?:{_COPULA}|hain|tha|thi|number|nmbr|num|no|ye|yeh|yah|to|toh|tou|"
    f"aya|aaya|ayi|aayi|mila|mili|received|{_H}{_Y}ں|{_Y}{_H}|نمبر|آ{_Y}ا|ملا|यह|नंबर|आया|मिला){_WEN}){{0,3}}"
    f"(?:[ \\t]*[:=#-]+)?[ \\t]*",
    _F,
)
_CODE_BEFORE_HEAD = r"(?<![\dA-Za-z_+])(?<!\d[ \-.,:/])(\d{4,8})[ \t]+"
_CODE_BEFORE_OWNER = f"(?:{_POSSESSIVE})[ \\t]+(?:[A-Za-z]{{2,15}}[ \\t]+)?"
_CODE_BEFORE_STRONG = re.compile(
    f"{_CODE_BEFORE_HEAD}(?:(?:{_COPULA})[ \\t]+)?(?:{_CODE_BEFORE_OWNER})?\\Z", _F
)
_CODE_BEFORE_WEAK = re.compile(
    f"{_CODE_BEFORE_HEAD}(?:(?:{_COPULA})[ \\t]+(?:{_CODE_BEFORE_OWNER})?|{_CODE_BEFORE_OWNER})\\Z",
    _F,
)
_YEAR_WORD_BEFORE = re.compile(
    f"{_WB}(?:in|since|from|until|till|by|during|year|saal|sal|san|سال|साल)[ \\t]*\\Z",
    _F,
)
_NUMBER_LABEL_BEFORE = re.compile(
    f"(?:{_WB}(?:order|number|no|num|nmbr|room|ticket|invoice|ref|reference|tracking|flat|house|street|plot|gali|"
    f"makan|model|version|ext|extension|total|roll|bill|page|line|port|آرڈر|نمبر|ऑर्डर|नंबर)|#)[ \\t.:#-]*\\Z",
    _F,
)


def _code_in_window(s: str, frm: int) -> Optional[Tuple[int, int]]:
    win = s[frm : frm + _OTP_WINDOW + 20]
    stop = _SENTENCE_END.search(win)
    if stop:
        win = win[: stop.start()]
    first = None
    for m in _CODE_ANYWHERE.finditer(win):
        if m.start() >= _OTP_WINDOW:
            break
        start = frm + m.start()
        end = start + len(m.group(0))
        if _looks_like_quantity(s, start, end):
            continue
        if _COPULA_BEFORE.search(win[: m.start()]):
            return (start, end)
        if first is None:
            first = (start, end)
    return first


def _find_otps(s: str, add: Add) -> None:
    for m in _OTP_KEYWORD.finditer(s):
        start, end = m.start(), m.end()
        if not m.group("strong") and _word_before(s, start, True) in _CODE_NOT_SECRET:
            continue
        strong = bool(m.group("strong") or m.group("pre"))
        hit = None
        if strong:
            hit = _code_in_window(s, end)
        else:
            gap = _WEAK_GAP.match(s, end)
            code = _CODE_HERE.match(s, max(end, gap.end() if gap else end))
            if code:
                code_at = code.end() - len(code.group(1))
                # "pin code 110001" / "54000" is a postal code, not a secret.
                digits = len(re.sub(r"\D", "", code.group(1), flags=_FA))
                postal = bool(m.group("postal")) and digits in (5, 6)
                if not postal and not _looks_like_quantity(s, code_at, code.end()):
                    hit = (code_at, code.end())
        if hit:
            add(hit[0], hit[1], "otp")

        if m.group("postal"):
            continue
        frm = max(0, start - 64)
        before = (_CODE_BEFORE_STRONG if strong else _CODE_BEFORE_WEAK).search(
            s[frm:start]
        )
        if not before or (before.start() == 0 and frm > 0):
            continue
        code_at = frm + before.start()
        lead = s[max(0, code_at - 24) : code_at]
        if _MONEY_BEFORE.search(lead) or _YEAR_WORD_BEFORE.search(lead):
            continue
        bare = len(before.group(0).strip()) == len(before.group(1))
        if bare and _NUMBER_LABEL_BEFORE.search(lead):
            continue
        if (
            not strong
            and _YEAR.match(before.group(1))
            and not _PW_CLAUSE_END.match(s[end : end + 12])
        ):
            continue
        add(code_at, code_at + len(before.group(1)), "otp")


# -- CNIC ----------------------------------------------------------------------

_CNIC_DASHED = re.compile(r"(?<!\d)\d{5}-\d{7}-\d(?!\d)", _FA)
_CNIC_PLAIN = re.compile(
    r"(?<![\d+])(?<!\d[ -])(?:\d{13}|\d{5} \d{7} \d)(?![ -]?\d)", _FA
)
_CNIC_KEYWORD = re.compile(
    f"{_WB}(?:cnic|nic|shanakhti|shnakhti|shinakhti|id[ -]?card|identity[ -]?card|national[ -]?id|شناخت{_Y}|"
    f"آئ{_Y}[{_WS}]?ڈ{_Y}[{_WS}]?{_K}ارڈ|पहचान[{_WS}]?पत्र|आईडी[{_WS}]?कार्ड){_WE}",
    _F,
)


def _find_cnics(s: str, add: Add) -> None:
    for m in _CNIC_DASHED.finditer(s):
        add(m.start(), m.end(), "cnic")
    for m in _CNIC_PLAIN.finditer(s):
        end = m.end()
        near = _CNIC_KEYWORD.search(
            s[max(0, m.start() - 48) : m.start()]
        ) or _CNIC_KEYWORD.search(s[end : end + 32])
        if near:
            add(m.start(), end, "cnic")


# -- IBAN ----------------------------------------------------------------------

_IBAN_PK = re.compile(
    r"(?<![A-Za-z0-9])PK[ -]?\d{2}[ -]?[A-Za-z]{4}(?:[ -]?\d){16}(?!\d)", _F
)
_IBAN_HEAD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}", _FA)
_IBAN_SOLID = re.compile(r"[A-Za-z0-9]{11,30}(?![A-Za-z0-9])", _FA)
_IBAN_GROUP = re.compile(r" ([A-Za-z0-9]{1,4})(?![A-Za-z0-9])", _FA)


def _mod97(iban: str) -> bool:
    moved = (iban[4:] + iban[:4]).upper()
    rem = 0
    for ch in moved:
        c = ord(ch)
        rem = (rem * 10 + c - 48) % 97 if c <= 57 else (rem * 100 + c - 55) % 97
    return rem == 1


def _find_ibans(s: str, add: Add) -> None:
    for m in _IBAN_PK.finditer(s):
        add(m.start(), m.end(), "iban")
    for m in _IBAN_HEAD.finditer(s):
        body_at = m.start() + 4
        solid = _IBAN_SOLID.match(s, body_at)
        if solid:
            if _mod97(m.group(0) + solid.group(0)):
                add(m.start(), solid.end(), "iban")
            continue
        ends: List[Tuple[int, str]] = []
        compact = m.group(0)
        at = body_at
        while len(compact) < 34:
            group = _IBAN_GROUP.match(s, at)
            if not group:
                break
            compact += group.group(1)
            at = group.end()
            if 15 <= len(compact) <= 34:
                ends.append((at, compact))
            if len(group.group(1)) < 4:
                break
        for end, text in reversed(ends):
            if re.search(r"\d", text[4:], _FA) and _mod97(text):
                add(m.start(), end, "iban")
                break


# -- bank accounts -------------------------------------------------------------

_ACCOUNT_KEYWORD = re.compile(
    f"{_WB}(?:(?:account|acct|acc)\\.?[ -]*(?:number|nmbr|num|no|#)|a/c(?:[ .]*(?:number|nmbr|num|no|#))?|"
    f"ا{_K}ا(?:ؤ|و)نٹ{_S}*نمبر|{_K}ھاتہ{_S}*نمبر|अकाउंट{_S}*नंबर|खाता{_S}*(?:नंबर|संख्या)){_WE}\\.?",
    _F,
)
_ACCOUNT_AFTER = re.compile(r"[^\d\n.!?۔,;،؛\ufffc]{0,24}?(\d(?:[ -]?\d)*)", _FA)
_DATE_SHAPE = re.compile(
    r"^(?:(?:19|20)\d\d-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-(?:19|20)\d\d)\Z", _FA
)


def _find_bank_accounts(s: str, add: Add) -> None:
    for m in _ACCOUNT_KEYWORD.finditer(s):
        hit = _ACCOUNT_AFTER.match(s, m.end())
        if not hit:
            continue
        run = hit.group(1)
        start = hit.end() - len(run)
        if start > 0 and re.match(r"[A-Za-z_+]", s[start - 1]):
            continue
        if _DATE_SHAPE.match(run):
            continue
        length = len(run)
        digits = length - len(re.findall(r"[ -]", run))
        if digits < 8 or digits > 20:
            length = len(re.match(r"^\d+", run, _FA).group(0))
            if length < 8 or length > 20:
                continue
        add(start, start + length, "bank_account")


# -- putting it together -------------------------------------------------------


@dataclass(slots=True)
class Span:
    start: int
    end: int
    kind: str


def detect(text: str) -> List[Span]:
    """Where the secrets are — in order, never overlapping."""
    s = _prepare(text)
    spans: List[Span] = []

    def add(start: int, end: int, kind: str) -> None:
        if end > start:
            spans.append(Span(start, end, kind))

    has_digit = bool(re.search(r"\d", s, _FA))
    _find_private_keys(s, add)
    _find_api_keys(s, add, has_digit)
    _find_passwords(s, add)
    if has_digit:
        _find_ibans(s, add)
        _find_cnics(s, add)
        _find_bank_accounts(s, add)
        _find_cards(s, add)
        _find_cvvs(s, add)
        _find_otps(s, add)
    if len(spans) < 2:
        return spans
    spans.sort(key=lambda sp: (sp.start, _RANK[sp.kind]))
    merged = [spans[0]]
    for sp in spans[1:]:
        last = merged[-1]
        if sp.start >= last.end:
            merged.append(sp)
            continue
        last.end = max(last.end, sp.end)
        if _RANK[sp.kind] < _RANK[last.kind]:
            last.kind = sp.kind
    return merged


@dataclass(slots=True)
class Redacted:
    text: str
    found: List[str]


def redact_secrets(value: object) -> Redacted:
    """Each secret becomes ``[redacted <kind>]``; ``found`` lists kinds in text order."""
    if not isinstance(value, str) or not value:
        return Redacted("", [])
    text = value
    marks: List[Tuple[int, str]] = []
    for _ in range(_MAX_PASSES):
        spans = detect(text)
        if not spans:
            break
        moved: List[Tuple[int, str]] = []
        out = ""
        cursor = 0
        shift = 0
        i = 0
        for sp in spans:
            while i < len(marks) and marks[i][0] < sp.end:
                if marks[i][0] < sp.start:
                    moved.append((marks[i][0] + shift, marks[i][1]))
                i += 1
            out += text[cursor : sp.start]
            moved.append((len(out), sp.kind))
            mark = placeholder(sp.kind)
            out += mark
            shift += len(mark) - (sp.end - sp.start)
            cursor = sp.end
        while i < len(marks):
            moved.append((marks[i][0] + shift, marks[i][1]))
            i += 1
        text = out + text[cursor:]
        marks = moved
    return Redacted(text, [k for _, k in marks])


def has_secret(text: object) -> bool:
    return isinstance(text, str) and bool(text) and bool(detect(text))


__all__ = [
    "KINDS",
    "MASK",
    "Redacted",
    "detect",
    "has_secret",
    "placeholder",
    "redact_secrets",
]
