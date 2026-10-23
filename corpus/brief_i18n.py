"""The brief's chrome in more than one language.

The clients are Croatian small businesses. A document they pay for that explains
itself in English ("the middle post, not the average") is a document they skim, so
every heading, card, note and footer goes through `t()`. English strings are the
keys, gettext-style: the English brief is byte-for-byte what it was, and a string
added without a translation shows up in English rather than as a blank, while the
test suite renders a Croatian brief and fails on anything recorded in MISSING.

Lens output is NOT translated here. Its language is decided where it is written
(the analysis prompts); this module only passes it through.
"""
from __future__ import annotations

import time

LANGS = ("en", "hr")

# Strings asked for in Croatian that have no entry. Filled at render time so a test
# can render a full brief in `hr` and assert nothing fell back to English.
MISSING: set[str] = set()

DAYS = {"en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "hr": ["Pon", "Uto", "Sri", "Čet", "Pet", "Sub", "Ned"]}

_EN_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec"]
# Genitive, which is what a Croatian date uses: "5. kolovoza 2026.", never
# "5. kolovoz 2026.".
_HR_MON = ["siječnja", "veljače", "ožujka", "travnja", "svibnja", "lipnja", "srpnja",
           "kolovoza", "rujna", "listopada", "studenoga", "prosinca"]


def lang_of(x) -> str:
    """Accept a language code or a brief `data` dict (which may lack the key)."""
    if isinstance(x, dict):
        x = x.get("lang")
    return x if x in LANGS else "en"


def t(lang, s: str, **kw) -> str:
    lang = lang_of(lang)
    if lang == "hr":
        hit = HR.get(s)
        if hit is None:
            MISSING.add(s)
        else:
            s = hit
    return s.format(**kw) if kw else s


def _lt(ts: int) -> time.struct_time:
    """The audience's clock ($CORPUS_TZ, else this machine's zone), as the weekday
    chart already used. UTC dated a sync at 00:40 in Central Europe "22 October (today)" on
    the 23rd, and a post made after midnight under the previous day (2026-10-23)."""
    import os
    name = os.environ.get("CORPUS_TZ")
    if name:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(ts, ZoneInfo(name)).timetuple()
        except Exception:
            pass
    return time.localtime(ts)


def date(ts: int, lang="en") -> str:
    """A date for prose and headers: 05 Aug 2026 / 5. kolovoza 2026."""
    g = _lt(ts)
    if lang_of(lang) == "hr":
        return f"{g.tm_mday}. {_HR_MON[g.tm_mon - 1]} {g.tm_year}."
    return f"{g.tm_mday:02d} {_EN_MON[g.tm_mon - 1]} {g.tm_year}"


def date_num(ts: int, lang="en") -> str:
    """A compact date for table cells: 2026-08-05 / 05.08.2026."""
    if lang_of(lang) == "hr":
        return time.strftime("%d.%m.%Y.", _lt(ts))
    return time.strftime("%Y-%m-%d", _lt(ts))


def stamp(ts: int, lang="en") -> str:
    """Generation time for the footer, on the same clock as every other date."""
    lt = _lt(ts)
    hm = time.strftime("%H:%M", lt)
    zone = time.strftime("%Z", lt) or "local"
    if lang_of(lang) == "hr":
        return f"{date(ts, 'hr')} u {hm} {zone}"
    return f"{date(ts, 'en')} {hm} {zone}"


def ago(days: int, lang="en") -> str:
    if lang_of(lang) == "hr":
        return "danas" if days <= 0 else "jučer" if days == 1 else f"prije {days} dana"
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


HR: dict[str, str] = {
    # masthead, age, window, footer
    "Content brief": "Analiza sadržaja",
    "Data captured {d} ({ago})": "Podaci prikupljeni {d} ({ago})",
    "Not synced yet: no capture date.": "Još nije sinkronizirano: nema datuma prikupljanja.",
    "Stale data.": "Zastarjeli podaci.",
    "These numbers are {n} days old. Posts published since then are missing and "
    "every count has moved. Re-sync before acting on this or sending it.":
        "Ovi brojevi stari su {n} dana. Objave od tada nedostaju, a svi su se brojevi "
        "u međuvremenu promijenili. Osvježite podatke prije nego što na temelju ovoga "
        "odlučujete ili ga šaljete dalje.",
    "No new post since {d}, {n} days before the last sync.":
        "Nema nove objave od {d}, {n} dana prije zadnje sinkronizacije.",
    "Covers {p}, the last sync, not up to today.":
        "Obuhvaća {p}, zadnju sinkronizaciju, a ne do danas.",
    "{n} older post(s) are outside this period and not counted.":
        "Starijih objava izvan ovog razdoblja, koje nisu uračunate: {n}.",
    "the 12 months to {d}": "12 mjeseci do {d}",
    "the {n} days to {d}": "{n} dana do {d}",
    "every stored post": "sve spremljene objave",
    "Generated {when} from the corpus store. Engagement figures are point-in-time "
    "captures, not live values.":
        "Izrađeno {when} iz baze corpus. Brojke interakcija stanje su u trenutku "
        "prikupljanja, a ne uživo.",
    "{n}/wk": "{n}/tj.",
    "{n}/mo": "{n}/mj.",

    # account summary
    "Summary and priorities": "Sažetak i prioriteti",
    "Three priorities": "Tri prioriteta",
    "Why": "Zašto",
    "Do this": "Što napraviti",
    "The next four weeks": "Sljedeća četiri tjedna",
    "Week": "Tjedan",
    "Post idea": "Ideja za objavu",
    "Format": "Format",

    # summary cards
    "Summary": "Sažetak",
    "No original posts in range. {n} repost(s) of other people's content were found "
    "and are listed separately.":
        "U ovom razdoblju nema izvornih objava. Pronađena su dijeljenja tuđeg sadržaja "
        "({n}), navedena zasebno.",
    "No content in range.": "U ovom razdoblju nema sadržaja.",
    "Original posts": "Izvorne objave",
    "written by you": "koje ste napisali vi",
    "written by this account": "koje je napisao ovaj profil",
    "Reactions": "Reakcije",
    "likes and other reactions": "lajkovi i ostale reakcije",
    "Comments": "Komentari",
    "replies left on the posts": "odgovori ostavljeni na objavama",
    "Shares": "Dijeljenja",
    "reposts by other people": "koliko su ih drugi podijelili",
    "Views": "Pregledi",
    "times the video was played": "koliko je puta video pokrenut",
    "Typical post": "Tipična objava",
    "the middle post, not the average": "srednja objava, ne prosjek",
    "Best post": "Najbolja objava",
    "highest single post": "najjača pojedinačna objava",
    "Posting rate": "Učestalost objavljivanja",
    "over {p}": "kroz {p}",
    "Followers": "Pratitelji",
    "at last check": "pri zadnjoj provjeri",
    "Engagement rate": "Stopa angažmana",
    "typical post vs follower count": "tipična objava u odnosu na broj pratitelja",
    "Engagement rate is not shown: at {n} followers a single post that reaches beyond "
    "the audience distorts the percentage past the point of meaning.":
        "Stopa angažmana nije prikazana: uz {n} pratitelja jedna objava koja dopre "
        "izvan publike toliko iskrivi postotak da on više ništa ne znači.",
    "Excludes {n} repost(s) of other people's posts, which are shown separately "
    "below. Reposts are amplification, not authorship, and their engagement belongs "
    "to the original author.":
        "Ne uključuje dijeljenja tuđih objava ({n}), koja su prikazana zasebno niže. "
        "Dijeljenje je pojačavanje tuđeg glasa, ne autorstvo, a interakcije pripadaju "
        "izvornom autoru.",

    # top posts and shared table words
    "Top posts": "Najbolje objave",
    "Date": "Datum",
    "Type": "Vrsta",
    "Total": "Ukupno",
    "Excerpt": "Isječak",
    "no caption": "bez opisa",
    "open": "otvori",
    "short video": "kratki video",
    "long video": "dugi video",
    "carousel": "karusel",
    "image": "slika",
    "text": "tekst",
    "article": "članak",
    "comment": "komentar",

    # formats
    "What formats work": "Koji formati rade",
    "Posts": "Objave",
    "Median engagement": "Medijan interakcija",
    "vs typical": "u odnosu na tipičnu",
    "Best": "Najbolja",
    "Median rather than mean: one viral post otherwise makes a format look reliable "
    "when it was a single outlier. &ldquo;vs typical&rdquo; compares each format "
    "against your own middle post, so 1.0&times; is par for you, not for the "
    "platform.":
        "Medijan, a ne prosjek: inače jedna viralna objava učini da format izgleda "
        "pouzdano, iako je bila samo iznimka. &bdquo;U odnosu na tipičnu&ldquo; "
        "uspoređuje svaki format s vašom srednjom objavom, pa je 1,0&times; vaš "
        "uobičajeni rezultat, a ne prosjek platforme.",
    "Median rather than mean: one viral post otherwise makes a format look reliable "
    "when it was a single outlier. &ldquo;vs typical&rdquo; compares each format "
    "against this account's own middle post, so 1.0&times; is par for them, not for "
    "the platform.":
        "Medijan, a ne prosjek: inače jedna viralna objava učini da format izgleda "
        "pouzdano, iako je bila samo iznimka. &bdquo;U odnosu na tipičnu&ldquo; "
        "uspoređuje svaki format sa srednjom objavom ovog profila, pa je 1,0&times; "
        "njihov uobičajeni rezultat, a ne prosjek platforme.",
    "Fewer than three posts. The median of two posts is not a pattern; treat these "
    "rows as unproven rather than as a recommendation.":
        "Manje od tri objave. Medijan dviju objava nije obrazac; ove retke shvatite "
        "kao nedokazane, a ne kao preporuku.",

    # gaps
    "What you never post about": "O čemu nikad ne objavljujete",
    "What they never post about": "O čemu nikad ne objavljuju",
    "Pricing named": "Spomenuta cijena",
    "A client result shown": "Prikazan rezultat klijenta",
    "How it works": "Kako to funkcionira",
    "Proof and numbers": "Dokazi i brojke",
    "A clear next step": "Jasan sljedeći korak",
    "Founder story": "Priča osnivača",
    "{n} of {m}": "{n} od {m}",
    "never": "nikad",
    "rarely": "rijetko",
    "sometimes": "ponekad",
    "often": "često",
    "Subject": "Tema",
    "Share": "Udio",
    "How often": "Koliko često",
    "Six subjects that tend to decide whether an audience becomes a customer, "
    "counted across every original post including video transcripts and on-screen "
    "text. A zero here is usually more actionable than any engagement number on the "
    "page.":
        "Šest tema koje obično odlučuju hoće li publika postati kupac, prebrojano "
        "kroz sve izvorne objave, uključujući transkripte videa i tekst na ekranu. "
        "Nula ovdje obično vrijedi više od bilo koje brojke interakcija na ovoj "
        "stranici.",
    "Counted by matching the phrases people actually use for each subject, in "
    "English and Croatian, not by interpretation, so every number is reproducible "
    "and can be checked against the post list below. The phrases are deliberately "
    "narrow: merely saying &ldquo;client&rdquo; is not counted as showing a client "
    "result. It will therefore undercount a subject discussed in unusual words.":
        "Prebrojano prema izrazima koje ljudi stvarno koriste za svaku temu, na "
        "engleskom i hrvatskom, a ne tumačenjem, pa se svaki broj može ponoviti i "
        "provjeriti na popisu objava niže. Izrazi su namjerno uski: sama riječ "
        "&bdquo;klijent&ldquo; ne računa se kao prikazan rezultat klijenta. Tema "
        "opisana neuobičajenim riječima zato može biti podbrojena.",

    # cadence
    "When you post": "Kada objavljujete",
    "When they post": "Kada objavljuju",
    "1 post": "1 objava",
    "Days in {tz} time. Median engagement by weekday, post count in brackets. Days "
    "with a single post are hatched and excluded from the scale, because one post is "
    "an anecdote rather than a pattern.":
        "Dani prema vremenskoj zoni {tz}. Medijan interakcija po danu u tjednu, broj "
        "objava u zagradi. Dani s jednom objavom iscrtkani su i ne ulaze u ljestvicu, "
        "jer je jedna objava anegdota, a ne obrazac.",

    # reposts
    "What you amplify": "Što dijelite",
    "What they amplify": "Što dijele",
    "Original author": "Izvorni autor",
    "Engagement": "Interakcije",
    "unknown": "nepoznato",
    "Reposts of other people's content. The engagement belongs to the original "
    "author, so none of it is counted in the figures above. Who you choose to amplify "
    "is still a positioning signal.":
        "Dijeljenja tuđeg sadržaja. Interakcije pripadaju izvornom autoru, pa nijedna "
        "nije uračunata u brojke iznad. Koga odlučite pojačati ipak govori nešto o "
        "vašem pozicioniranju.",
    "Reposts of other people's content. The engagement belongs to the original "
    "author, so none of it is counted in the figures above. Who someone chooses to "
    "amplify is still a positioning signal.":
        "Dijeljenja tuđeg sadržaja. Interakcije pripadaju izvornom autoru, pa nijedna "
        "nije uračunata u brojke iznad. Koga netko odluči pojačati ipak govori nešto "
        "o njegovu pozicioniranju.",

    # experience
    "Experience & education": "Iskustvo i obrazovanje",
    "Role": "Uloga",
    "When": "Kada",
    "What you did": "Što ste radili",
    "What they did": "Što su radili",
    "Education": "Obrazovanje",
    "School": "Škola",
    "Degree": "Zvanje",
    "more": "više",
    "awards": "nagrade",
    "skills": "vještine",
    "certifications": "certifikati",
    "languages": "jezici",
    "courses": "tečajevi",

    # lenses
    "Personal post, not assessed.": "Osobna objava, nije ocjenjivana.",
    "Content Audit": "Analiza objave",
    "What Works Here": "Što ovdje radi",
    "Hook Teardown": "Analiza udice",
    "CTA / Offer Teardown": "Analiza poziva na akciju",
    "Positioning Signal": "Signal pozicioniranja",

    # every post
    "Every post": "Svaka objava",
    "{n} original posts, newest first. Click any row for the full breakdown.":
        "Izvornih objava: {n}, od najnovije. Kliknite redak za cijelu analizu.",
    "({kind}, no caption)": "({kind}, bez opisa)",
    "R": "R", "C": "K", "S": "D", "V": "P",
    "reactions": "reakcija", "reaction": "reakcija",
    "comments": "komentara",
    "shares": "dijeljenja", "share": "dijeljenje",
    "views": "pregleda", "view": "pregled",
    "total interactions": "ukupno interakcija",
    "no engagement recorded": "nema zabilježenih interakcija",
    "open on {p}": "otvori na {p}u",
    "music bed": "glazbena podloga",
    "This post has no caption. On a carousel or image post the caption carries the "
    "argument, so there is nothing for a reader to respond to.":
        "Ova objava nema opis. Na karuselu ili slici poruku nosi opis, pa čitatelj "
        "nema na što odgovoriti.",
    "Machine transcript, may contain errors": "Strojni transkript, moguće su greške",

    # hooks
    "How you open": "Kako počinjete",
    "How they open": "Kako počinju",
    "First line": "Prvi redak",
    "Your best openings": "Vaši najbolji počeci",
    "Your weakest openings": "Vaši najslabiji počeci",
    "Their best openings": "Njihovi najbolji počeci",
    "Their weakest openings": "Njihovi najslabiji počeci",
    "The first line is what decides whether the rest gets read. These are the "
    "openings of your {n} best and {n} weakest posts, by total interactions. Look for "
    "what the top group does that the bottom group does not.":
        "Prvi redak odlučuje hoće li se ostatak pročitati. Ovo su počeci vaših {n} "
        "najboljih i {n} najslabijih objava, po ukupnim interakcijama. Tražite što "
        "gornja skupina radi, a donja ne.",
    "The first line is what decides whether the rest gets read. These are the "
    "openings of their {n} best and {n} weakest posts, by total interactions. Look "
    "for what the top group does that the bottom group does not.":
        "Prvi redak odlučuje hoće li se ostatak pročitati. Ovo su počeci njihovih {n} "
        "najboljih i {n} najslabijih objava, po ukupnim interakcijama. Tražite što "
        "gornja skupina radi, a donja ne.",

    # audience
    "What the audience says back": "Što publika odgovara",
    "Comments read": "Pročitani komentari",
    "audience comments captured": "prikupljeni komentari publike",
    "Questions asked": "Postavljena pitanja",
    "each one is a post your audience already wants":
        "svako je objava koju vaša publika već želi",
    "each one is a post they already want": "svako je objava koju publika već želi",
    "People": "Ljudi",
    "distinct commenters": "različitih komentatora",
    "Regulars": "Stalni",
    "commented on more than one post": "komentirali više od jedne objave",
    "someone": "netko",
    "on “{p}”": "na „{p}”",
    "on the post": "na objavi",
    "1 like": "1 lajk",
    "{n} likes": "lajkova: {n}",
    "Questions are the cheapest content ideas available: your audience has already "
    "said what it wants. Replies are not collected, so some of these may already "
    "have an answer under them.":
        "Pitanja su najjeftinije ideje za sadržaj: vaša publika je već rekla što "
        "želi. Odgovori se ne prikupljaju, pa neka od ovih pitanja možda već imaju "
        "odgovor ispod sebe.",
    "Questions are the cheapest content ideas available: the audience has already "
    "said what it wants. Replies are not collected, so some of these may already "
    "have an answer under them.":
        "Pitanja su najjeftinije ideje za sadržaj: publika je već rekla što želi. "
        "Odgovori se ne prikupljaju, pa neka od ovih pitanja možda već imaju odgovor "
        "ispod sebe.",
    "What got agreement": "S čim se ljudi slažu",
    "Comments other readers liked, so these are the opinions the wider audience "
    "endorsed, not just one person's.":
        "Komentari koje su drugi čitatelji lajkali, dakle mišljenja koja je podržala "
        "šira publika, a ne samo jedna osoba.",
    "Who keeps coming back": "Tko se vraća",
    "{n} of {m} commenters showed up on more than one post. These are your warmest "
    "audience and the cheapest people to talk to directly.":
        "{n} od {m} komentatora javilo se na više od jedne objave. To je vaša "
        "najtoplija publika i ljudi s kojima je najlakše razgovarati izravno.",
    "{n} of {m} commenters showed up on more than one post. These are the warmest "
    "audience on the account and the cheapest people to talk to directly.":
        "{n} od {m} komentatora javilo se na više od jedne objave. To je najtoplija "
        "publika ovog profila i ljudi s kojima je najlakše razgovarati izravno.",
    "Up to {cap} comments were read per post, newest first, and replies to comments "
    "are not collected. Your own accounts' comments and promotional plugs are left "
    "out. Counts here are therefore a floor, not the full thread.":
        "Po objavi je pročitano najviše {cap} komentara, od najnovijih, a odgovori na "
        "komentare se ne prikupljaju. Komentari vaših vlastitih profila i reklamni "
        "komentari izostavljeni su. Brojke su zato donja granica, a ne cijela "
        "rasprava.",

    # videos
    "What your videos say": "Što govore vaši videi",
    "What their videos say": "Što govore njihovi videi",
    "Spoken (machine transcript, may contain errors)":
        "Izgovoreno (strojni transkript, moguće su greške)",
    "On screen": "Na ekranu",
    "Caption": "Opis",
    "{n} interactions": "interakcija: {n}",
    "{n} video(s) with readable content. Spoken audio is transcribed by machine and "
    "can mishear names and slang; on-screen text is read off the frames. Most "
    "analytics tools cannot see either, which is why a video back-catalogue usually "
    "shows up as a row of thumbnails.":
        "Videa s čitljivim sadržajem: {n}. Govor je strojno transkribiran i može "
        "pogrešno čuti imena i sleng; tekst na ekranu očitan je sa sličica. Većina "
        "alata za analitiku ne vidi ni jedno ni drugo, pa se arhiva videa obično "
        "svede na niz sličica.",

    # limits
    "What this can and cannot show": "Što ova analiza može, a što ne može pokazati",
    "This brief is built from the account's own analytics export, so it includes "
    "owner-only metrics.":
        "Ova analiza izrađena je iz izvoza analitike samog profila, pa uključuje i "
        "brojke koje vidi samo vlasnik.",
    "This brief is built from publicly visible content, the same view any visitor "
    "has. These numbers exist only inside your own account:":
        "Ova analiza izrađena je iz javno vidljivog sadržaja, istog koji vidi svaki "
        "posjetitelj. Ove brojke postoje samo unutar vašeg profila:",
    "To add reach and saves to the next brief, send your {what}. It takes a minute "
    "to download from the app and nothing else can produce these numbers.":
        "Da sljedeća analiza uključi doseg i spremanja, pošaljite nam {what}. "
        "Preuzimanje iz aplikacije traje minutu, a te brojke ne može dati ništa "
        "drugo.",
    " and ": " i ",
    "analytics export": "izvoz analitike",
    "Instagram insights export": "izvoz Instagram statistike (Insights)",
    "LinkedIn analytics export": "izvoz LinkedIn analitike",
    "YouTube Studio export": "izvoz iz YouTube Studija",
    "This brief is built from publicly visible content. The following metrics are "
    "owner-only and not obtainable for an account you do not own, by any tool at "
    "any price:":
        "Ova analiza izrađena je iz javno vidljivog sadržaja. Sljedeće brojke vidi "
        "samo vlasnik i nisu dostupne za profil koji nije vaš, ni jednim alatom ni "
        "po kojoj cijeni:",
    "Engagement counts shown are what the platform displays publicly, captured at "
    "the times listed. They move after capture.":
        "Prikazane interakcije su ono što platforma javno prikazuje, u navedeno "
        "vrijeme prikupljanja. Nakon toga se mijenjaju.",
    "impressions": "prikazivanja",
    "reach": "doseg",
    "watch time": "vrijeme gledanja",
    "completion rate": "postotak odgledanih do kraja",
    "follows from post": "praćenja s objave",
    "profile visits": "posjeti profilu",
    "saves (Instagram)": "spremanja (Instagram)",

    # combined
    "Profiles": "Profili",
    "accounts in this report": "profila u ovoj analizi",
    "excludes reposts": "bez dijeljenja tuđih objava",
    "Total interactions": "Ukupno interakcija",
    "reactions + comments + shares": "reakcije + komentari + dijeljenja",
    "interactions, {n} post(s)": "interakcija, objava: {n}",
    "Across all profiles": "Svi profili zajedno",
    "Platform comparison": "Usporedba platformi",
    "Platform": "Platforma",
    "Handle": "Profil",
    "Interactions": "Interakcije",
    "Typical": "Tipična",
    "Engagement is not comparable across platforms in absolute terms: an Instagram "
    "like and a LinkedIn reaction cost the reader different amounts of effort. "
    "Compare each platform against itself over time, not against the other.":
        "Interakcije se ne mogu izravno uspoređivati među platformama: lajk na "
        "Instagramu i reakcija na LinkedInu čitatelja ne stoje jednako truda. "
        "Svaku platformu uspoređujte samu sa sobom kroz vrijeme, a ne s drugom.",
    "first-party analytics export": "izvoz vlastite analitike",
    "publicly visible data": "javno vidljivi podaci",
    "{n} profiles": "profila: {n}",
    "{n} posts": "objava: {n}",

    # benchmark
    "{name}: competitor benchmark": "{name}: usporedba s konkurencijom",
    "{name} against {n} account(s)": "{name} u usporedbi s profilima: {n}",
    "publicly visible data, own posts only": "javno vidljivi podaci, samo vlastite objave",
    "Not compared:": "Nije uspoređeno:",
    "no posts in the period": "nema objava u razdoblju",
    "the subject is not on {p}": "analizirani profil nije na {p}u",
    "the subject has no {p} posts in the period":
        "analizirani profil nema objava na {p}u u razdoblju",
    "you": "vi",
    "Account": "Profil",
    "Per week": "Tjedno",
    "Eng. rate": "Stopa ang.",
    "Video": "Video",
    "Best format": "Najbolji format",
    "Best day": "Najbolji dan",
    "Data from": "Podaci od",
    "{name} has only {n} post(s) here, too few to say whether it is above or below "
    "anyone.":
        "{name} ovdje ima samo objava: {n}, premalo da bi se reklo je li iznad ili "
        "ispod ikoga.",
    "posts per week": "objava tjedno",
    "interactions on a typical post": "interakcija na tipičnoj objavi",
    "engagement rate": "stopa angažmana",
    "share of posts that are video": "udio video objava",
    "above them": "iznad njih",
    "below them": "ispod njih",
    "in line with them": "u rangu s njima",
    "{a} vs {b} for the competitors' median, {word}.":
        "{a} prema {b} kod medijana konkurencije, {word}.",
    "Where {name} stands": "Kako stoji {name}",
    "{x}x their typical, {kind}, {when}": "{x}x njihove tipične, {kind}, {when}",
    "(no text)": "(bez teksta)",
    "{name}: best posts": "{name}: najbolje objave",
    "Nothing to compare: each platform needs the subject and at least one other "
    "account with posts.":
        "Nema se što usporediti: za svaku platformu potreban je analizirani profil i "
        "barem još jedan profil s objavama.",
    "Typical post is the median of reactions + comments + shares, which a single "
    "viral post cannot move. Engagement rate is shown only above 1,000 followers. "
    "Best day uses the {tz} clock and needs two posts on that day. Each account's "
    "period ends at its own last sync (Data from), so accounts synced on different "
    "days cover slightly different weeks.":
        "Tipična objava je medijan reakcija + komentara + dijeljenja, koji jedna "
        "viralna objava ne može pomaknuti. Stopa angažmana prikazuje se samo iznad "
        "1.000 pratitelja. Najbolji dan računa se po vremenskoj zoni {tz} i traži "
        "barem dvije objave tog dana. Razdoblje svakog profila završava njegovom "
        "zadnjom sinkronizacijom (Podaci od), pa profili sinkronizirani različitih "
        "dana pokrivaju malo različite tjedne.",
}


def num(text: str, lang="en") -> str:
    """A number already formatted the English way ("1,234.5", "5.45%"), written
    for `lang`. Croatian swaps the separators: "1.234,5", "5,45%". Format first,
    localize second, so every f-string in the brief keeps its precision rules."""
    if lang_of(lang) != "hr":
        return text
    return text.translate(str.maketrans({".": ",", ",": "."}))
