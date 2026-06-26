#!/usr/bin/env python3
"""
Content inference for scraped venues — cuisine key, bilingual copy, and fallback
imagery. The cuisine keyword rules + bilingual templates are adapted from the
Resto seed script (scripts/seed-venues/generate_seed.py in the web repo), but
generalised so the city display name is a parameter.

The *structured* taxonomy mapping (Google Places types -> cuisine/tag slugs)
lives in taxonomy_map.py; this module owns the human-readable EN/RO labels and
the keyword fallback when Places types are too generic.
"""

from __future__ import annotations

IMG_BASE = "https://images.unsplash.com/photo-"
IMG_Q = "?auto=format&fit=crop&w=1200&q=80"

# Verified Unsplash photo ids per cuisine. [0] = hero, [1:4] = gallery.
# Used only when Google gives us no usable photo.
IMG = {
    "japanese": ["1579584425555-c3ce17fd4351", "1553621042-f6e147245754", "1617093727343-374698b1b08d", "1559925393-8be0ec4767c8"],
    "pizza":    ["1513104890138-7c749659a591", "1574071318508-1cdbab80d002", "1565299624946-b28f40a0ae38", "1604382354936-07c5d9983bd3"],
    "italian":  ["1551183053-bf91a1d81141", "1473093295043-cdd812d0e601", "1621996346565-e3dbc646d9a9", "1604382354936-07c5d9983bd3"],
    "burger":   ["1568901346375-23c9450c58cd", "1565958011703-44f9829ba187", "1559925393-8be0ec4767c8", "1544025162-d76694265947"],
    "steak":    ["1544025162-d76694265947", "1559339352-11d035aa65de", "1466978913421-dad2ebd01d17", "1424847651672-bf20a4b0982b"],
    "mideast":  ["1555396273-367ea4eb4db5", "1600335895229-6e75511892c8", "1565958011703-44f9829ba187", "1504674900247-0877df9cc836"],
    "greek":    ["1574894709920-11b28e7367e3", "1601050690597-df0568f70950", "1555396273-367ea4eb4db5", "1467003909585-2f8a72700288"],
    "indian":   ["1585937421612-70a008356fbe", "1505253758473-96b7015fcd40", "1631452180519-c014fe946bc7", "1504674900247-0877df9cc836"],
    "asian":    ["1617093727343-374698b1b08d", "1569718212165-3a8278d5f624", "1503764654157-72d979d9af2f", "1559925393-8be0ec4767c8"],
    "french":   ["1455619452474-d2be8b1e70cd", "1414235077428-338989a2e8c0", "1424847651672-bf20a4b0982b", "1504674900247-0877df9cc836"],
    "mexican":  ["1600335895229-6e75511892c8", "1552332386-f8dd00dc2f85", "1565958011703-44f9829ba187", "1504674900247-0877df9cc836"],
    "seafood":  ["1467003909585-2f8a72700288", "1559339352-11d035aa65de", "1565299624946-b28f40a0ae38", "1424847651672-bf20a4b0982b"],
    "mediterranean": ["1467003909585-2f8a72700288", "1601050690597-df0568f70950", "1574894709920-11b28e7367e3", "1504674900247-0877df9cc836"],
    "coffee":   ["1511920170033-f8396924c348", "1501339847302-ac426a4a7cbb", "1554118811-1e0d58224f24", "1497935586351-b67a49e012bf"],
    "bakery":   ["1509440159596-0249088772ff", "1486427944299-d1955d23e34d", "1555507036-ab1f4038808a", "1568254183919-78a4f43a2877"],
    "bar":      ["1514362545857-3bc16c4c7d1b", "1572116469696-31de0f17cc34", "1470337458703-46ad1756a187", "1538488881038-e252a119ace7"],
    "romanian": ["1466978913421-dad2ebd01d17", "1424847651672-bf20a4b0982b", "1504674900247-0877df9cc836", "1559925393-8be0ec4767c8"],
    "vegetarian": ["1512621776951-a57141f2eefd", "1540420773420-3366772f4999", "1543339308-43e59d6b73a6", "1490645935967-10de6ba17061"],
    "fastfood": ["1568901346375-23c9450c58cd", "1565958011703-44f9829ba187", "1600335895229-6e75511892c8", "1504674900247-0877df9cc836"],
    "international": ["1517248135467-4c7edcad34c4", "1559925393-8be0ec4767c8", "1424847651672-bf20a4b0982b", "1504674900247-0877df9cc836"],
}

# Cuisine inference from the venue name — first matching keyword wins.
# City-agnostic; works anywhere.
CUISINE_RULES = [
    ("japanese", ["sushi", "ramen", "japanese", "japonez", "tokyo", "sumeshi", "wasabi", "sakura", "omakase", "dashi", "ronin", "umami"]),
    ("asian",    ["korean", "shanghai", "hanoi", "asian", "asiatic", "chinese", "chinez", "wok", "thai", "vietnam", "pho ", "noodle"]),
    ("indian",   ["indian", "gandhi", "turmeric", "tikka", "curry", "tandoor", "masala"]),
    ("greek",    ["greek", "grec", "meraki", "souvlaki", "gyros", "spartan", "santorini", "mykonos"]),
    ("mexican",  ["taco", "torero", "picaro", "mexican", "burrito", "cantina mexic", "guacamole", "rodizio"]),
    ("french",   ["petit paris", "creperie", "crêperie", "le petit", "le bistro", "brasserie", "c'est la vie", "bonjour"]),
    ("mideast",  ["shaorma", "shaurma", "kebab", "döner", "doner", "shawarma", "sultan", "istanbul", "arabic", "liban", "syrian", "hummus", "falafel"]),
    ("pizza",    ["pizza", "pizzeria", "napoli", "trancio", "il forno", "margherita", "focacc"]),
    ("italian",  ["pasta", "trattoria", "cucina", "italian", "italiano", "italiana", "ristorante", "osteria", "gusto", "da pino", "da vinci", "buongiorno"]),
    ("burger",   ["burger", "smash", "meat up", "hamburger", "patty"]),
    ("steak",    ["steak", "steakhouse", "grill", "grătar", "gratar", "bbq", "barbecue", "texas", "smokehouse", "rib"]),
    ("seafood",  ["seafood", "fish", "pește", "peste", "oyster", "stridii", "raci", "fructe de mare", "marea"]),
    ("mediterranean", ["mediterran", "mediterane", "tapas", "paella", "spaniol", "spanish", "hispano"]),
    ("vegetarian", ["vegetarian", "vegan", "plant based", "plant-based", "raw ", "salad"]),
    ("coffee",   ["coffee", "café", "cafe", "espresso", "roaster", "barista", "caffè", "caffe", "brunch", "specialty"]),
    ("bakery",   ["bakery", "brutărie", "brutarie", "patiserie", "patisserie", "cofetărie", "cofetarie", "dessert", "deser", "cake", "tort", "gelat", "ice cream", "înghețat", "inghetat"]),
    ("bar",      ["pub", "gastropub", "lounge", "cocktail", "biergarten", "brewery", "brewing", "wine", "crama", "club", "irish", "bar "]),
    ("romanian", ["casa", "crama", "taverna", "zama", "vatra", "ardel", "românesc", "romanesc", "traditional", "tradițional", "ciorbă", "ciorba", "bunic", "haiduc", "boier"]),
    ("fastfood", ["kfc", "mcdonald", "fast food", "fast-food", "crispy", "autoservire", "cantina", "fries", "hot dog", "street food"]),
]

# Bilingual labels + copy per cuisine key. {name} and {city} are interpolated.
# (cuisine_en, cuisine_ro, tagline_en, tagline_ro, desc_en, desc_ro)
META = {
    "japanese": ("Japanese & Sushi", "Japoneză & Sushi", "Fresh sushi & Japanese flavors", "Sushi proaspăt & arome japoneze",
        "{name} brings Japanese flavors to {city} — fresh nigiri, maki and warm bowls prepared to order.",
        "{name} aduce aromele Japoniei în {city} — nigiri proaspăt, maki și boluri calde preparate la comandă."),
    "pizza": ("Pizza & Italian", "Pizza & Italiană", "Wood-fired pizza, done right", "Pizza la cuptor, ca la carte",
        "{name} is a pizza spot in {city} — thin, wood-fired crusts and honest toppings.",
        "{name} este o pizzerie din {city} — blat subțire copt la cuptor și ingrediente cinstite."),
    "italian": ("Italian", "Italiană", "Handmade pasta & Italian classics", "Paste de casă & clasice italiene",
        "{name} serves Italian comfort in {city} — handmade pasta, slow sauces and a short wine list.",
        "{name} servește bucate italiene în {city} — paste de casă, sosuri gătite cu răbdare și o listă scurtă de vinuri."),
    "burger": ("Burgers", "Burgeri", "Juicy burgers & crispy fries", "Burgeri suculenți & cartofi crocanți",
        "{name} is a burger joint in {city} — smashed patties, melty cheese and proper fries.",
        "{name} este un loc de burgeri din {city} — chifle pufoase, brânză topită și cartofi ca la carte."),
    "steak": ("Steakhouse & Grill", "Steakhouse & Grătar", "Open-fire grill & prime cuts", "Grătar pe foc & cărnuri alese",
        "{name} is a steakhouse in {city} — prime cuts over open fire and a meat-first menu.",
        "{name} este un steakhouse din {city} — cărnuri alese pe foc deschis și un meniu cu carnea în prim-plan."),
    "mideast": ("Middle Eastern", "Orientală", "Shawarma, kebab & mezze", "Shaorma, kebab & mezze",
        "{name} brings Middle Eastern street food to {city} — shawarma off the spit and fresh flatbread.",
        "{name} aduce street food oriental în {city} — shaorma de pe rotisor și lipie proaspăt coaptă."),
    "greek": ("Greek", "Grecească", "Greek tavern flavors", "Arome de tavernă grecească",
        "{name} is a Greek taverna in {city} — souvlaki, fresh salads, olive oil and lemon.",
        "{name} este o tavernă grecească din {city} — souvlaki, salate proaspete, ulei de măsline și lămâie."),
    "indian": ("Indian", "Indiană", "Curries, tandoor & spice", "Curry, tandoor & condimente",
        "{name} serves Indian cooking in {city} — slow curries, tandoor breads and warm spices.",
        "{name} servește bucătărie indiană în {city} — curry gătit îndelung, pâini la tandoor și condimente calde."),
    "asian": ("Asian", "Asiatică", "Far-East flavors & noodle bowls", "Arome asiatice & boluri cu tăiței",
        "{name} brings Far-East flavors to {city} — wok dishes, noodle bowls and fragrant broths.",
        "{name} aduce arome din Asia în {city} — preparate la wok, boluri cu tăiței și supe aromate."),
    "french": ("French", "Franțuzească", "French bistro classics", "Clasice de bistro franțuzesc",
        "{name} is a French bistro in {city} — bistro classics, good bread and a glass of wine.",
        "{name} este un bistro franțuzesc din {city} — clasice de bistro, pâine bună și un pahar de vin."),
    "mexican": ("Mexican & Latin", "Mexicană & Latino", "Tacos, salsa & good vibes", "Tacos, salsa & voie bună",
        "{name} brings Latin flavors to {city} — tacos, fresh salsa and a lively table.",
        "{name} aduce arome latino în {city} — tacos, salsa proaspătă și o masă plină de viață."),
    "seafood": ("Seafood", "Fructe de mare", "Fresh fish & seafood", "Pește proaspăt & fructe de mare",
        "{name} is a seafood spot in {city} — fresh fish, shellfish and clean, simple plates.",
        "{name} este un loc cu fructe de mare în {city} — pește proaspăt, scoici și preparate simple."),
    "mediterranean": ("Mediterranean", "Mediteraneană", "Sun-soaked Mediterranean plates", "Preparate mediteraneene însorite",
        "{name} serves Mediterranean food in {city} — bright, olive-oil-forward plates made for sharing.",
        "{name} servește bucătărie mediteraneană în {city} — preparate luminoase, cu ulei de măsline, numai bune de împărțit."),
    "coffee": ("Café & Brunch", "Cafenea & Brunch", "Specialty coffee & easy mornings", "Cafea de specialitate & diminețe lejere",
        "{name} is a relaxed coffee spot in {city} — specialty espresso, filter brews and fresh pastries.",
        "{name} este o cafenea relaxată din {city} — espresso de specialitate, cafea la filtru și produse proaspete de patiserie."),
    "bakery": ("Bakery & Desserts", "Brutărie & Deserturi", "Fresh bakes & sweet things", "Produse proaspete & dulciuri",
        "{name} is a bakery in {city} — fresh bread, pastries and desserts made daily.",
        "{name} este o brutărie din {city} — pâine proaspătă, produse de patiserie și deserturi făcute zilnic."),
    "bar": ("Bar & Pub", "Bar & Pub", "Drinks, music & good company", "Băuturi, muzică & companie bună",
        "{name} is a bar in {city} — craft drinks, a solid soundtrack and a room made for late nights.",
        "{name} este un bar din {city} — băuturi de autor, muzică bună și o atmosferă făcută pentru seri lungi."),
    "romanian": ("Romanian", "Românească", "Romanian comfort cooking", "Bucate românești ca acasă",
        "{name} cooks Romanian comfort food in {city} — hearty plates, old recipes and generous portions.",
        "{name} gătește bucate românești în {city} — preparate consistente, rețete vechi și porții generoase."),
    "vegetarian": ("Vegetarian & Plant-based", "Vegetariană & Plant-based", "Plant-forward, full of flavor", "Plant-based, plin de gust",
        "{name} is a plant-based spot in {city} — vegetarian and vegan plates that don't skimp on flavor.",
        "{name} este un loc plant-based din {city} — preparate vegetariene și vegane care nu fac compromisuri la gust."),
    "fastfood": ("Fast food", "Fast food", "Quick bites, big flavor", "Gustări rapide, gust pe măsură",
        "{name} is a quick-bite spot in {city} — fast, filling and easy on the wallet.",
        "{name} este un loc de gustări rapide din {city} — rapid, săritor și prietenos cu bugetul."),
    "international": ("International", "Internațională", "Easygoing food in the city", "Mâncare bună, în oraș",
        "{name} is a welcoming restaurant in {city} — a varied menu and a comfortable room for lunch or dinner.",
        "{name} este un restaurant primitor din {city} — un meniu variat și o sală comodă pentru prânz sau cină."),
}


def infer_cuisine_from_name(name: str) -> str:
    """First matching keyword wins; default 'international'."""
    n = (name or "").lower()
    for key, kws in CUISINE_RULES:
        for kw in kws:
            if kw in n:
                return key
    return "international"


def fallback_image_urls(cuisine_key: str):
    """(hero_url, [gallery_urls]) for a cuisine when Google gives no photo."""
    ids = IMG.get(cuisine_key, IMG["international"])
    urls = [IMG_BASE + i + IMG_Q for i in ids]
    return urls[0], urls[1:4]


def bilingual_copy(cuisine_key: str, name: str, city_name: str):
    """Returns the dict of EN/RO label + tagline + description fields for a venue."""
    cen, cro, ten, tro, den, dro = META.get(cuisine_key, META["international"])
    return {
        "cuisine": cen,
        "cuisine_ro": cro,
        "tagline": ten,
        "tagline_ro": tro,
        "description": den.format(name=name, city=city_name),
        "description_ro": dro.format(name=name, city=city_name),
    }
