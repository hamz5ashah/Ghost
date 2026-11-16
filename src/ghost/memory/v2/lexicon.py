"""Bilingual mini-lexicon for search (Stonic port, phase 6).

Ported from Stonic's ``memory/lexicon.js`` (groups extracted verbatim). Users
say one thing in Urdu one day and English the next: "tankhwa" / "salary",
"Itwar" / "Sunday". Each group lists everyday words that mean the same thing in
English, Roman Urdu, Urdu script or Hindi; a query word in a group also
searches the rest of its group.

It does not translate or understand meaning; only plain, unambiguous everyday
words appear (a word with two meanings sits only in its more common group).
Spelling does not matter here: every word passes through ``fold_text`` on load.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from ghost.memory.v2.normalize import fold_text, skeleton

GROUPS: List[str] = [
    "mother mom mum mama ammi maa walida amma امی ماں والدہ اماں माँ मां अम्मी",
    "father dad papa abbu baba walid abba ابو والد بابا ابا पिता पापा अब्बू",
    "sister behen behan bahen baji apa بہن باجی آپا बहन",
    "brother bhai bhaiya بھائی भाई",
    "wife biwi begum بیوی بیگم पत्नी बीवी",
    "husband shohar شوہر पति",
    "son beta بیٹا बेटा",
    "daughter beti بیٹی बेटी",
    "friend dost دوست दोस्त",
    "grandmother nani dadi نانی دادی नानी दादी",
    "grandfather nana dada نانا دادا नाना दादा",
    "uncle chacha mamu mamoon taya khalu چچا ماموں تایا خالو",
    "aunt khala phuppo chachi mumani خالہ پھوپھو چچی ممانی",
    "child children kid kids bacha bache bachay بچہ بچے बच्चा बच्चे",
    "family khandan gharwale خاندان परिवार",
    "wedding marriage shadi nikah شادی نکاح शादी",
    "birthday salgirah janamdin سالگرہ जन्मदिन",
    "guest guests mehman مہمان मेहमान",
    "neighbour neighbor parosi hamsaya پڑوسی ہمسایہ",
    "teacher ustad استاد",
    "doctor daktar ڈاکٹر डॉक्टर",
    "today aaj آج आज",
    "tomorrow yesterday kal کل कल",
    "morning subah صبح सुबह",
    "evening shaam شام शाम",
    "night raat رات रात",
    "afternoon noon dopahar dopehar دوپہر दोपहर",
    "week weekly hafta hafte ہفتہ ہفتے हफ्ता सप्ताह",
    "month monthly mahina mahine مہینہ مہینے महीना",
    "year yearly saal سال साल",
    "day din دن दिन",
    "date tareekh تاریخ तारीख",
    "sunday itwaar اتوار रविवार इतवार",
    "monday peer somwar پیر سوموار सोमवार",
    "tuesday mangal منگل मंगलवार",
    "wednesday budh بدھ बुधवार",
    "thursday jumerat جمعرات गुरुवार",
    "friday juma jumma جمعہ शुक्रवार",
    "saturday sanichar سنیچر शनिवार",
    "daily everyday roz rozana روز روزانہ रोज़",
    "next agla agle agli اگلا اگلے اگلی अगला",
    "last previous pichla pichle pichli akhri پچھلا پچھلے آخری पिछला",
    "deadline muddat ڈیڈ لائن",
    "money paisa paise raqam rupay rupees rupaye پیسہ پیسے رقم روپے पैसा पैसे रुपये",
    "salary wage wages tankhwa tankhwah تنخواہ तनख्वाह वेतन",
    "price cost rate qeemat keemat daam قیمت कीमत दाम",
    "payment adaigi پیمنٹ ادائیگی",
    "bill invoice بل انوائس",
    "loan debt qarz karz udhar قرض ادھار कर्ज़ उधार",
    "profit munafa nafa منافع",
    "loss nuqsan nuksan نقصان",
    "expensive mehnga mehngi مہنگا مہنگی महंगा",
    "cheap sasta sasti سستا سستی सस्ता",
    "work job kaam naukri کام نوکری काम नौकरी",
    "office daftar دفتر آفس दफ्तर",
    "shop store dukaan دکان اسٹور दुकान",
    "business karobar کاروبار बिज़नेस कारोबार",
    "customer client gahak گاہک کلائنٹ",
    "meeting mulaqat ملاقات میٹنگ",
    "company kampani کمپنی",
    "accountant munshi اکاؤنٹنٹ",
    "account khata اکاؤنٹ کھاتہ",
    "message paigham میسج پیغام",
    "call phone کال فون",
    "advance peshgi ایڈوانس پیشگی",
    "condition shart شرط शर्त",
    "decision decided decide faisla فیصلہ फैसला",
    "promise promised wada وعدہ वादा",
    "plan mansooba منصوبہ پلان",
    "holiday leave chutti چھٹی छुट्टी",
    "bonus inaam بونس انعام",
    "percent feesad فیصد",
    "thousand hazar ہزار हज़ार",
    "lakh lac لاکھ लाख",
    "half aadha آدھا आधा",
    "medicine tablet pill dawa dawai goli دوا دوائی گولی दवा दवाई",
    "hospital aspatal ہسپتال اسپتال अस्पताल",
    "sick ill beemar bimar بیمار बीमार",
    "health sehat tabiyat tabiat صحت طبیعت सेहत तबीयत",
    "pain dard درد दर्द",
    "fever bukhar بخار बुखार",
    "sleep neend نیند नींद",
    "food meal khana کھانا खाना",
    "water pani پانی पानी",
    "tea chai chaye چائے चाय",
    "milk doodh دودھ दूध",
    "sugar cheeni shakkar چینی شکر चीनी",
    "breakfast nashta ناشتہ नाश्ता",
    "exercise workout warzish kasrat ورزش",
    "walk sair سیر",
    "weight wazan وزن वज़न",
    "home house ghar گھر घर",
    "city shehar شہر शहर",
    "village gaon گاؤں गाँव",
    "country mulk ملک देश",
    "school iskool سکول اسکول स्कूल",
    "exam imtihan امتحان परीक्षा",
    "book kitab کتاب किताब",
    "car gari گاڑی गाड़ी",
    "trip travel journey tour safar سفر सफर यात्रा",
    "room kamra کمرہ कमरा",
    "market bazaar bazar بازار बाज़ार",
    "mosque masjid مسجد",
    "prayer namaz نماز",
    "gift present tohfa تحفہ तोहफा",
    "clothes dress kapre kapray kapde کپڑے कपड़े",
    "color colour rang رنگ रंग",
    "name naam نام नाम",
    "age umar عمر उम्र",
    "language zaban zuban زبان भाषा",
    "game khel کھیل खेल",
    "movie film فلم",
    "song music gana mausiqi گانا موسیقی",
    "photo picture tasveer تصویر तस्वीर",
    "favorite favourite like likes pasand pasandida پسند پسندیدہ पसंद",
    "dislike hate napasand nafrat ناپسند نفرت",
    "happy khush خوش खुश",
    "sad upset udaas pareshan اداس پریشان उदास परेशान",
    "angry gussa naraz غصہ ناراض गुस्सा नाराज़",
    "tired thaka thaki thakawat تھکا تھکاوٹ",
    "problem issue masla mushkil مسئلہ مشکل समस्या",
    "new naya nayi naye نیا نئی نئے नया",
    "old purana purani پرانا پرانی पुराना",
    "big bara bari بڑا بڑی बड़ा",
    "small little chota choti چھوٹا چھوٹی छोटा",
    "first pehla pehli پہلا پہلی पहला",
    "second doosra dusra doosri دوسرا دوسری दूसरा",
    "third teesra tisra تیسرا تیسری",
    "start begin shuru شروع शुरू",
    "end finish complete done khatam mukammal ختم مکمل",
    "buy purchase khareed kharid خرید",
    "strict sakht سخت",
    "rule usool qaida اصول قاعدہ",
]

_GROUP_OF: Dict[str, int] = {}
_MEMBERS: List[List[Tuple[str, str]]] = []

for _index, _line in enumerate(GROUPS):
    _seen: set = set()
    _list: List[Tuple[str, str]] = []
    for _raw in _line.split():
        _fold = fold_text(_raw)
        # A "word" that folds into two words (or nothing) cannot be a pair.
        if not _fold or " " in _fold or _fold in _seen:
            continue
        _seen.add(_fold)
        _list.append((_fold, skeleton(_raw)))
        # A word already in an earlier group stays there: one word, one meaning.
        _GROUP_OF.setdefault(_fold, _index)
    _MEMBERS.append(_list)

GROUP_COUNT = len(GROUPS)


def synonyms_of(fold: str) -> List[Tuple[str, str]]:
    """(fold, skel) of this word's synonyms, itself excluded; [] if not in the lexicon."""
    index = _GROUP_OF.get(fold)
    if index is None:
        return []
    return [m for m in _MEMBERS[index] if m[0] != fold]


__all__ = ["GROUPS", "GROUP_COUNT", "synonyms_of"]
