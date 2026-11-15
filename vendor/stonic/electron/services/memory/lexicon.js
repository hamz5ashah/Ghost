// Do zubanon ka chhota lughat — talash ke liye.
//
// ════════════════════════════════════════════════════════════════════════════
//  YE KYUN HAI
// ════════════════════════════════════════════════════════════════════════════
//
//  Hamare users ek hi baat ko kabhi Urdu mein kehte hain, kabhi English mein:
//  aaj "tankhwa", kal "salary"; aaj "Itwar", kal "Sunday". Lafzi talash in
//  dono ko alag lafz samajhti hai, aur golden set (scripts/memory-eval) ne
//  dikhaya ke isi ek wajah se bohat se sawal chook jate hain.
//
//  Ye file rozmarra ke lafzon ke JORE rakhti hai: ek group ke saare lafz ek hi
//  cheez hain, chahe English, Roman Urdu, Urdu script ya Hindi mein. Sawal ka
//  lafz kisi group mein ho to us group ke baqi lafz bhi dhoonde jate hain.
//
// ── YE KYA NAHI HAI ─────────────────────────────────────────────────────────
//
//  Ye tarjuma nahi karta aur matlab nahi samajhta — sirf aam, saaf lafzon ke
//  jore hain. "japani client" se "Tanaka" tak ka rasta is ke bas ka nahi: wo
//  nichor (digest) ke English jumle aur naamon ka kaam hai.
//
//  Sirf wo lafz yahan aate hain jin ka ek hi saaf matlab ho. Jis lafz ke do
//  matlab hon ("hafta" = week aur Saturday) wo sirf apne zyada aam matlab mein
//  hai — ghalat jor sahi yaad ko neeche dhakel deta hai.
//
// ── SPELLING ────────────────────────────────────────────────────────────────
//
//  Lafz jaise chahein likhein: load hote waqt har lafz `normalize.foldText` se
//  guzarta hai, to "tankhwa" aur "tankhwah" ki fikr yahan nahi karni.

const { foldText, skeleton } = require('./normalize');

const GROUPS = [
  // ── log ──
  'mother mom mum mama ammi maa walida amma امی ماں والدہ اماں माँ मां अम्मी',
  'father dad papa abbu baba walid abba ابو والد بابا ابا पिता पापा अब्बू',
  'sister behen behan bahen baji apa بہن باجی آپا बहन',
  'brother bhai bhaiya بھائی भाई',
  'wife biwi begum بیوی بیگم पत्नी बीवी',
  'husband shohar شوہر पति',
  'son beta بیٹا बेटा',
  'daughter beti بیٹی बेटी',
  'friend dost دوست दोस्त',
  'grandmother nani dadi نانی دادی नानी दादी',
  'grandfather nana dada نانا دادا नाना दादा',
  'uncle chacha mamu mamoon taya khalu چچا ماموں تایا خالو',
  'aunt khala phuppo chachi mumani خالہ پھوپھو چچی ممانی',
  'child children kid kids bacha bache bachay بچہ بچے बच्चा बच्चे',
  'family khandan gharwale خاندان परिवार',
  'wedding marriage shadi nikah شادی نکاح शादी',
  'birthday salgirah janamdin سالگرہ जन्मदिन',
  'guest guests mehman مہمان मेहमान',
  'neighbour neighbor parosi hamsaya پڑوسی ہمسایہ',
  'teacher ustad استاد',
  'doctor daktar ڈاکٹر डॉक्टर',
  // ── waqt ──
  'today aaj آج आज',
  'tomorrow yesterday kal کل कल',
  'morning subah صبح सुबह',
  'evening shaam شام शाम',
  'night raat رات रात',
  'afternoon noon dopahar dopehar دوپہر दोपहर',
  'week weekly hafta hafte ہفتہ ہفتے हफ्ता सप्ताह',
  'month monthly mahina mahine مہینہ مہینے महीना',
  'year yearly saal سال साल',
  'day din دن दिन',
  'date tareekh تاریخ तारीख',
  'sunday itwaar اتوار रविवार इतवार',
  'monday peer somwar پیر سوموار सोमवार',
  'tuesday mangal منگل मंगलवार',
  'wednesday budh بدھ बुधवार',
  'thursday jumerat جمعرات गुरुवार',
  'friday juma jumma جمعہ शुक्रवार',
  'saturday sanichar سنیچر शनिवार',
  'daily everyday roz rozana روز روزانہ रोज़',
  'next agla agle agli اگلا اگلے اگلی अगला',
  'last previous pichla pichle pichli akhri پچھلا پچھلے آخری पिछला',
  'deadline muddat ڈیڈ لائن',
  // ── paisa aur kaam ──
  'money paisa paise raqam rupay rupees rupaye پیسہ پیسے رقم روپے पैसा पैसे रुपये',
  'salary wage wages tankhwa tankhwah تنخواہ तनख्वाह वेतन',
  'price cost rate qeemat keemat daam قیمت कीमत दाम',
  'payment adaigi پیمنٹ ادائیگی',
  'bill invoice بل انوائس',
  'loan debt qarz karz udhar قرض ادھار कर्ज़ उधार',
  'profit munafa nafa منافع',
  'loss nuqsan nuksan نقصان',
  'expensive mehnga mehngi مہنگا مہنگی महंगा',
  'cheap sasta sasti سستا سستی सस्ता',
  'work job kaam naukri کام نوکری काम नौकरी',
  'office daftar دفتر آفس दफ्तर',
  'shop store dukaan دکان اسٹور दुकान',
  'business karobar کاروبار बिज़नेस कारोबार',
  'customer client gahak گاہک کلائنٹ',
  'meeting mulaqat ملاقات میٹنگ',
  'company kampani کمپنی',
  'accountant munshi اکاؤنٹنٹ',
  'account khata اکاؤنٹ کھاتہ',
  'message paigham میسج پیغام',
  'call phone کال فون',
  'advance peshgi ایڈوانس پیشگی',
  'condition shart شرط शर्त',
  'decision decided decide faisla فیصلہ फैसला',
  'promise promised wada وعدہ वादा',
  'plan mansooba منصوبہ پلان',
  'holiday leave chutti چھٹی छुट्टी',
  'bonus inaam بونس انعام',
  'percent feesad فیصد',
  'thousand hazar ہزار हज़ार',
  'lakh lac لاکھ लाख',
  'half aadha آدھا आधा',
  // ── sehat aur rozmarra ──
  'medicine tablet pill dawa dawai goli دوا دوائی گولی दवा दवाई',
  'hospital aspatal ہسپتال اسپتال अस्पताल',
  'sick ill beemar bimar بیمار बीमार',
  'health sehat tabiyat tabiat صحت طبیعت सेहत तबीयत',
  'pain dard درد दर्द',
  'fever bukhar بخار बुखार',
  'sleep neend نیند नींद',
  'food meal khana کھانا खाना',
  'water pani پانی पानी',
  'tea chai chaye چائے चाय',
  'milk doodh دودھ दूध',
  'sugar cheeni shakkar چینی شکر चीनी',
  'breakfast nashta ناشتہ नाश्ता',
  'exercise workout warzish kasrat ورزش',
  'walk sair سیر',
  'weight wazan وزن वज़न',
  // ── jagahein aur cheezein ──
  'home house ghar گھر घर',
  'city shehar شہر शहर',
  'village gaon گاؤں गाँव',
  'country mulk ملک देश',
  'school iskool سکول اسکول स्कूल',
  'exam imtihan امتحان परीक्षा',
  'book kitab کتاب किताब',
  'car gari گاڑی गाड़ी',
  'trip travel journey tour safar سفر सफर यात्रा',
  'room kamra کمرہ कमरा',
  'market bazaar bazar بازار बाज़ार',
  'mosque masjid مسجد',
  'prayer namaz نماز',
  'gift present tohfa تحفہ तोहफा',
  'clothes dress kapre kapray kapde کپڑے कपड़े',
  'color colour rang رنگ रंग',
  'name naam نام नाम',
  'age umar عمر उम्र',
  'language zaban zuban زبان भाषा',
  'game khel کھیل खेल',
  'movie film فلم',
  'song music gana mausiqi گانا موسیقی',
  'photo picture tasveer تصویر तस्वीर',
  // ── pasand aur haalat ──
  'favorite favourite like likes pasand pasandida پسند پسندیدہ पसंद',
  'dislike hate napasand nafrat ناپسند نفرت',
  'happy khush خوش खुश',
  'sad upset udaas pareshan اداس پریشان उदास परेशान',
  'angry gussa naraz غصہ ناراض गुस्सा नाराज़',
  'tired thaka thaki thakawat تھکا تھکاوٹ',
  'problem issue masla mushkil مسئلہ مشکل समस्या',
  'new naya nayi naye نیا نئی نئے नया',
  'old purana purani پرانا پرانی पुराना',
  'big bara bari بڑا بڑی बड़ा',
  'small little chota choti چھوٹا چھوٹی छोटा',
  'first pehla pehli پہلا پہلی पहला',
  'second doosra dusra doosri دوسرا دوسری दूसरा',
  'third teesra tisra تیسرا تیسری',
  'start begin shuru شروع शुरू',
  'end finish complete done khatam mukammal ختم مکمل',
  'buy purchase khareed kharid خرید',
  'strict sakht سخت',
  'rule usool qaida اصول قاعدہ',
];

/** fold -> group ka number; aur har group ke fold/skel. */
const groupOf = new Map();
const members = [];

GROUPS.forEach((line, index) => {
  const seen = new Set();
  const list = [];
  for (const raw of line.split(/\s+/)) {
    const fold = foldText(raw);
    // Ek "lafz" fold ke baad do ho jaye (ya khali) to wo jora nahi ban sakta.
    if (!fold || fold.includes(' ') || seen.has(fold)) continue;
    seen.add(fold);
    list.push({ fold, skel: skeleton(raw) });
    // Jo lafz pehle kisi group mein aa chuka wo wahin rehta hai: ek lafz, ek matlab.
    if (!groupOf.has(fold)) groupOf.set(fold, index);
  }
  members.push(list);
});

/**
 * Is lafz ke hum-maani lafz (khud ko chhor kar). Lughat mein na ho to khali.
 * @param {string} fold  normalize.foldText se guzra hua lafz
 * @returns {Array<{ fold: string, skel: string }>}
 */
function synonymsOf(fold) {
  const index = groupOf.get(fold);
  if (index == null) return [];
  return members[index].filter((m) => m.fold !== fold);
}

module.exports = { synonymsOf, GROUP_COUNT: GROUPS.length };
