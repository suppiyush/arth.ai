"""The owner's language. Arth replies in the language of the owner's message: "en" (English) or "hi"
(Hindi or Hinglish; replies are Hinglish, Hindi in Roman script, the way owners type).

Each fixed reply is written in both; pick() chooses. The orchestrator detects the language of each typed
message and remembers it for button clicks. Specialists get it as fields["lang"] (Plan, Query) or as
Commit.lang / Undo.lang.
"""

import re
from typing import Literal

Lang = Literal["en", "hi"]
DEFAULT: Lang = "en"

# Common Hindi words as owners type them in Roman script ("me", "do", "par" are left out: they are English
# words too), and English function words. Names, items and numbers belong to neither, so the two counts
# decide: "Sunita ko 2 kg nails" is Hinglish, "Who owes me udhaar?" is English.
MARKERS = {
    "hai", "hain", "ho", "tha", "thi", "ka", "ki", "ke", "ko", "se", "mein", "pe", "aur", "ya",
    "kya", "kitna", "kitne", "kitni", "kis", "kisko", "kisne", "kaun", "kaunsa", "kab", "kahan", "kaise", "kaisa",
    "becha", "bechi", "beche", "bech", "diya", "diye", "de", "dena", "dene", "lena", "lene", "liya", "liye",
    "mila", "mile", "aaya", "aaye", "aayi", "karo", "kar", "karna", "dikhao", "batao", "bata", "bana", "banao",
    "mangwa", "mangwao", "bacha", "bache", "baaki", "baki", "udhaar", "udhar", "paisa", "paise", "aaj", "kal",
    "mahine", "hafte", "saal", "sabse", "zyada", "jyada", "kam", "naya", "nayi", "wala", "wale", "bhi", "nahi",
    "nahin", "haan", "abhi", "mujhe", "mera", "meri", "mere", "aap", "aapka", "hua", "hui", "hue", "gaya", "gayi",
    "hoga", "chahiye", "khata", "hisaab", "galla", "kharcha", "munafa", "bikri", "becho", "bhejo", "yaad",
}
ENGLISH = {
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "for", "from", "with", "and", "or", "in", "on",
    "at", "by", "how", "what", "who", "whom", "which", "when", "where", "why", "does", "did", "i", "my", "me", "you",
    "your", "we", "our", "he", "she", "her", "his", "they", "their", "it", "this", "that", "much", "many", "have",
    "has", "had", "can", "could", "will", "would", "should", "please", "show", "give", "tell", "make", "sold",
    "bought", "paid", "owe", "owes", "left", "best", "most", "least", "all", "any", "some", "there", "not",
}
_DEVANAGARI = re.compile("[\u0900-\u097f]")


def detect(text: str, previous: str | None = None) -> Lang:
    """The language of one message. A message with no telling words ("undo", "5% discount", "confirm") or an
    even split keeps the conversation's previous language."""
    if _DEVANAGARI.search(text):
        return "hi"
    words = re.findall(r"[a-z]+", text.lower())
    hindi, english = sum(w in MARKERS for w in words), sum(w in ENGLISH for w in words)
    if hindi > english or (hindi and hindi >= 2 and hindi == english):
        return "hi"
    if english > hindi:
        return "en"
    return previous if previous in ("en", "hi") else DEFAULT


def of(fields: dict | None) -> Lang:
    lang = (fields or {}).get("lang")
    return lang if lang in ("en", "hi") else DEFAULT


def pick(lang: str, hi: str, en: str) -> str:
    return en if lang == "en" else hi


def plural(n, word: str, many: str | None = None) -> str:
    """"1 supplier", "3 suppliers"; used in both languages (Hinglish keeps English nouns)."""
    return f"{n} {word if n == 1 else many or word + 's'}"


def name(lang: str) -> str:
    """For prompts: the language ASI1 should write in."""
    return pick(lang, "simple Hinglish: everyday Hindi written in Roman script, the way a shopkeeper types on "
                      "WhatsApp, with correct Hindi grammar (e.g. 'Aapko Balaji Build ko ₹5,800 dena hai')",
                "simple, plain English")
