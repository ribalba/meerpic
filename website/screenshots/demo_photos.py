"""The demo library the website screenshots are taken of.

About eighty pictures, arranged as a year of somebody's photos: the last few
days at home near Potsdam, a day in Berlin, a summer at the Baltic Sea, a week
in Lisbon, winter, spring, and a hike in the Alps the year before. Most are an
iPhone's, copied out of iCloud Photos into one flat folder; the rest are a
Sony's and a Fujifilm's, imported into dated folders of their own. Two library
roots, one timeline, which is the point.

Every picture is a CC0 photo from StockSnap (see SOURCES), so the screenshots
can go on a public page. Nothing here is anybody's own library, and nothing in
this directory ever reads one.

Dates are relative to the day the library is built: the newest photo is from
this morning, so "newest first" looks the same whenever the shots are
regenerated. Seasonal sets (the beach, the snow) are pinned to their month
instead, at the most recent such date that is at least three weeks back.

Pure standard library: build_library.py (in the agent image), seed.py (in the
agent container) and shoot.py (on the host, with Playwright) all import it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# The zone "today" is counted in, and the one the demo stack runs in.
HOME_ZONE = ZoneInfo("Europe/Berlin")

# Seasonal sets are pinned to a month and day, but never closer to today than
# this, so they cannot land among the last few days.
MIN_GAP = timedelta(days=21)

# The two library roots, as LIBRARY_ROOTS names them.
ICLOUD = "icloud"
CAMERA = "camera"
ROOT_DIRS = {ICLOUD: "iCloud", CAMERA: "Camera"}

# The CC0 source of every picture: StockSnap id -> (photographer, title).
# Downloaded from https://cdn.stocksnap.io/img-thumbs/960w/<id>.jpg; the
# photo's page is in PAGES.
SOURCES: dict[str, tuple[str, str]] = {
    "UXO5P8KFS5": ("Hello Goodbye", "Coffee Cappuccino"),
    "WDJES619M1": ("David Bares", "Coffee Latte"),
    "D322D377AD": ("Drew Coffman", "Coffee Latte"),
    "YAG9NYBYZW": ("Michal Kulesza", "Golden Retriever Dog"),
    "2N0WSXKY4L": ("Shopify", "Dog Pug"),
    "JVSG0V22X6": ("Andrew Pons", "Dog Pet"),
    "63348D1909": ("Jay Mantri", "Dogs Animals"),
    "824XB7BXYU": ("Patrycja Tomaszczyk", "Dog Sleeping"),
    "VSCGP1X7OE": ("Tricia Gray", "Sunset Lake"),
    "8UOSAA3FRO": ("Sergei Gussev", "Sunset Lake"),
    "F7EPDH2L1A": ("Eneida Nieves", "Rustic Pizza"),
    "YM863UXAFR": ("Alex Blăjan", "Trees Plants"),
    "SBPB82MIOJ": ("Tricia Gray", "Autumn Path"),
    "61UZQF1A4B": ("Sebastian Unrau", "Trail Path"),
    "7VITJYYH6K": ("Lucas Allmann", "Swan River"),
    "UMEIOOHUSZ": ("Ian Livesey", "Swan Birds"),
    "F65E5BF6BE": ("Jonas Nilsson Lee", "Cows Animals"),
    "D810EF71C9": ("Kelly Sikkema", "Cows Animals"),
    "86HZXEM9SH": ("Tim Wright", "Cow Cattle"),
    "MRZZ7VSUNT": ("Angelina Litvin", "Cow Animals"),
    "QZZC02VYSS": ("Lucas Allmann", "Happy Cow"),
    "ZUXGSBSYBK": ("Ian Livesey", "Cow Animal"),
    "SQ99X727RI": ("Jahoo Clouseau", "Cow Animal"),
    "ASKA6Q9G1Y": ("Omar Prestwich", "Animals Horses"),
    "P8ZHITFSH3": ("Dave Meier", "Bike Bicycle"),
    "BCMY03PRZ9": ("Bogdan Dada", "Transportation Bicycles"),
    "W7T1J6B7DM": ("Altered Reality", "Birthday Cake"),
    "L28IBU4Y3J": ("Sergei Solovev", "Cake Food"),
    "KKYDNLT7LH": ("Sabri Tuzcu", "Cat Cute"),
    "UCS90HFBJL": ("Freddie Marriage", "Animals Cats"),
    "7BXCZNT5JF": ("Ian Livesey", "Cat Grooming"),
    "34QDSDEHXG": ("Toa Heftiba", "Street Food"),
    "8A981F99AC": ("Travel Coffee Book", "Berlin Germany"),
    "863C586BF6": ("Dave Meier", "Wurst Sausage"),
    "803CD628BB": ("Dave Meier", "Schiller Monument, Konzerthaus Steps"),
    "1AE1F86F1B": ("Dave Meier", "Sony Centre Berlin"),
    "A618B01675": ("Leeroy", "Train Station"),
    "N2OFJ9PRQ6": ("Bonnie Moreland", "Snow Train"),
    "46BADF516C": ("Leeroy", "Train Tracks"),
    "2PT5PZPXLH": ("Alice Donovan Rouse", "Beach Sand"),
    "VR4V2BQROC": ("Aaron Burden", "Beach Sand"),
    "MB9BSCD0GM": ("Sven Scheuermeier", "Beach Sand"),
    "T5TB6VX5PG": ("Adrianna Calvo", "Seashell Beach"),
    "DI64TAJTIS": ("Frank McKenna", "Nature Beach"),
    "FM9CFSXY0B": ("Lenart Lipovšek", "Beach Sand"),
    "I3QWA2OLLM": ("Ian Schneider", "Beach Shore"),
    "BAH9GVSUKZ": ("Bernard Spragg", "Pier Ocean"),
    "8M4C9RHYT1": ("Jay Mantri", "Pier Dock"),
    "7201530D4D": ("Nick Diamantidis", "Lighthouse Coast"),
    "K9GGGG9QB4": ("Riku Lu", "Lighthouse Ocean"),
    "P94WAUPGNQ": ("Leeroy", "Sailboats Ocean"),
    "D8974ECD93": ("Skitter Photo", "Sailboats Harbor"),
    "17E6A220E6": ("Alin Meceanu", "Sailing Sailboat"),
    "B9F8B43531": ("Skitter Photo", "Lisbon Portugal"),
    "2174058CFB": ("Skitter Photo", "Tramcar Streetcar"),
    "AFJ2YJAZUK": ("Sergei Gussev", "City Architecture"),
    "DJ9G2OU1TO": ("Sergei Gussev", "Tower Castle"),
    "N6LYB1HWK5": ("Sergei Gussev", "Street Architecture"),
    "VU6VE3P21M": ("Alfons Morales", "Architecture Building"),
    "BBGPLGHY5B": ("Foodie Girl", "Fresh Peppers"),
    "AA39511B59": ("Leeroy", "Fruits Vegetables"),
    "GMI1BVKXBN": ("Kelly Ishmael", "Spring Flowers"),
    "J4FWC3JXPV": ("Ian Livesey", "Spring Flowers"),
    "TQCY3FH54E": ("Altered Reality", "Spring Flowers"),
    "MPETRILKHN": ("Ian Livesey", "Baby Sheep"),
    "YDO05H0VY2": ("Tim Marshall", "Sheep Animal"),
    "4F944F6A71": ("Jonas Nilsson Lee", "Sheep Animals"),
    "UPPM7VG5M4": ("Mary", "Christmas Street"),
    "30A8EFE302": ("Ali Inay", "Winter Snow"),
    "UQYCF0AKLC": ("Elvin Siew Chun Wai", "Nature Snow"),
    "RQ2Z75PQIN": ("Samuel Zeller", "Snow Winter"),
    "N9EDXX8BPQ": ("Travel Photographer", "Fireworks Celebration"),
    "CPLJUAMC1T": ("Travel Photographer", "Fireworks Background"),
    "Y65WP68WKD": ("Claudel Rheault", "Nature Landscape"),
    "ZHC95L56RT": ("Chris Hayashi", "Hiking"),
    "THN6VCGM6X": ("Rob Bye", "Hiking Trekking"),
    "5IGV8IZMBT": ("Aukje Leermakers", "Mountain Lake"),
    "PW0UP8VIQZ": ("Tricia Gray", "Mountains Lake"),
    "Y2KCFMGHD1": ("Alisa Anton", "Bicycle Bike"),
    "KPM7XLOIGZ": ("Free Nature Stock", "Autumn Leaf"),
    "S2QPBLPTL5": ("Pawel Kadysz", "Dog Pet"),
}


# Each photo's page: StockSnap's own title for it, as a slug, then its id.
PAGES: dict[str, str] = {
    "UXO5P8KFS5": "coffee-cappuccino-UXO5P8KFS5",
    "WDJES619M1": "coffee-latte-WDJES619M1",
    "D322D377AD": "coffee-latte-D322D377AD",
    "YAG9NYBYZW": "goldenretreiver-dog-YAG9NYBYZW",
    "2N0WSXKY4L": "dog-pug-2N0WSXKY4L",
    "JVSG0V22X6": "dog-pet-JVSG0V22X6",
    "63348D1909": "dogs-animals-63348D1909",
    "824XB7BXYU": "dog-sleeping-824XB7BXYU",
    "VSCGP1X7OE": "sunset-lake-VSCGP1X7OE",
    "8UOSAA3FRO": "sunset-lake-8UOSAA3FRO",
    "F7EPDH2L1A": "rustic-pizza-F7EPDH2L1A",
    "YM863UXAFR": "trees-plants-YM863UXAFR",
    "SBPB82MIOJ": "autumn-path-SBPB82MIOJ",
    "61UZQF1A4B": "trail-path-61UZQF1A4B",
    "7VITJYYH6K": "swan-river-7VITJYYH6K",
    "UMEIOOHUSZ": "swan-birds-UMEIOOHUSZ",
    "F65E5BF6BE": "cows-animals-F65E5BF6BE",
    "D810EF71C9": "cows-animals-D810EF71C9",
    "86HZXEM9SH": "cow-cattle-86HZXEM9SH",
    "MRZZ7VSUNT": "cow-animals-MRZZ7VSUNT",
    "QZZC02VYSS": "happy-cow-QZZC02VYSS",
    "ZUXGSBSYBK": "cow-animal-ZUXGSBSYBK",
    "SQ99X727RI": "cow-animal-SQ99X727RI",
    "ASKA6Q9G1Y": "animals-horses-ASKA6Q9G1Y",
    "P8ZHITFSH3": "bike-bicycle-P8ZHITFSH3",
    "BCMY03PRZ9": "transportation-bicycles-BCMY03PRZ9",
    "W7T1J6B7DM": "birthday-cake-W7T1J6B7DM",
    "L28IBU4Y3J": "cake-food-L28IBU4Y3J",
    "KKYDNLT7LH": "cat-cute-KKYDNLT7LH",
    "UCS90HFBJL": "animals-cats-UCS90HFBJL",
    "7BXCZNT5JF": "cat-grooming-7BXCZNT5JF",
    "34QDSDEHXG": "street-food-34QDSDEHXG",
    "8A981F99AC": "berlin-germany-8A981F99AC",
    "863C586BF6": "wurst-sausage-863C586BF6",
    "803CD628BB": "schillermonument-konzerthaussteps-803CD628BB",
    "1AE1F86F1B": "sonycentre-berlin-1AE1F86F1B",
    "A618B01675": "train-station-A618B01675",
    "N2OFJ9PRQ6": "snow-train-N2OFJ9PRQ6",
    "46BADF516C": "train-tracks-46BADF516C",
    "2PT5PZPXLH": "beach-sand-2PT5PZPXLH",
    "VR4V2BQROC": "beach-sand-VR4V2BQROC",
    "MB9BSCD0GM": "beach-sand-MB9BSCD0GM",
    "T5TB6VX5PG": "seashell-beach-T5TB6VX5PG",
    "DI64TAJTIS": "nature-beach-DI64TAJTIS",
    "FM9CFSXY0B": "beach-sand-FM9CFSXY0B",
    "I3QWA2OLLM": "beach-shore-I3QWA2OLLM",
    "BAH9GVSUKZ": "pier-ocean-BAH9GVSUKZ",
    "8M4C9RHYT1": "pier-dock-8M4C9RHYT1",
    "7201530D4D": "lighthouse-coast-7201530D4D",
    "K9GGGG9QB4": "lighthouse-ocean-K9GGGG9QB4",
    "P94WAUPGNQ": "sailboats-ocean-P94WAUPGNQ",
    "D8974ECD93": "sailboats-harbor-D8974ECD93",
    "17E6A220E6": "sailing-sailboat-17E6A220E6",
    "B9F8B43531": "lisbon-portugal-B9F8B43531",
    "2174058CFB": "tramcar-streetcar-2174058CFB",
    "AFJ2YJAZUK": "city-architecture-AFJ2YJAZUK",
    "DJ9G2OU1TO": "tower-castle-DJ9G2OU1TO",
    "N6LYB1HWK5": "street-architecture-N6LYB1HWK5",
    "VU6VE3P21M": "architecture-building-VU6VE3P21M",
    "BBGPLGHY5B": "fresh-peppers-BBGPLGHY5B",
    "AA39511B59": "fruits-vegetables-AA39511B59",
    "GMI1BVKXBN": "spring-flowers-GMI1BVKXBN",
    "J4FWC3JXPV": "spring-flowers-J4FWC3JXPV",
    "TQCY3FH54E": "spring-flowers-TQCY3FH54E",
    "MPETRILKHN": "baby-sheep-MPETRILKHN",
    "YDO05H0VY2": "sheep-animal-YDO05H0VY2",
    "4F944F6A71": "sheep-animals-4F944F6A71",
    "UPPM7VG5M4": "christmas-street-UPPM7VG5M4",
    "30A8EFE302": "winter-snow-30A8EFE302",
    "UQYCF0AKLC": "nature-snow-UQYCF0AKLC",
    "RQ2Z75PQIN": "snow-winter-RQ2Z75PQIN",
    "N9EDXX8BPQ": "fireworks-celebration-N9EDXX8BPQ",
    "CPLJUAMC1T": "fireworks-background-CPLJUAMC1T",
    "Y65WP68WKD": "nature-landscape-Y65WP68WKD",
    "ZHC95L56RT": "guy-man-ZHC95L56RT",
    "THN6VCGM6X": "hiking-trekking-THN6VCGM6X",
    "5IGV8IZMBT": "mountain-lake-5IGV8IZMBT",
    "PW0UP8VIQZ": "mountains-lake-PW0UP8VIQZ",
    "Y2KCFMGHD1": "bicycle-bike-Y2KCFMGHD1",
    "KPM7XLOIGZ": "autumn-leaf-KPM7XLOIGZ",
    "S2QPBLPTL5": "dog-pet-S2QPBLPTL5",
}


def source_url(code: str) -> str:
    return f"https://cdn.stocksnap.io/img-thumbs/960w/{code}.jpg"


def source_page(code: str) -> str:
    return f"https://stocksnap.io/photo/{PAGES[code]}"


# --- places ------------------------------------------------------------------


@dataclass(frozen=True)
class Place:
    lat: float
    lon: float
    alt: float
    zone: str = "Europe/Berlin"


PLACES: dict[str, Place] = {
    # Home, and the country around it.
    "potsdam-home": Place(52.3961, 13.0521, 38),
    "potsdam-centre": Place(52.3995, 13.0590, 35),
    "heiliger-see": Place(52.4126, 13.0702, 31),
    "sanssouci": Place(52.4036, 13.0385, 45),
    "bornstedt": Place(52.4118, 13.0292, 40),
    "neuer-garten": Place(52.4190, 13.0690, 32),
    "wildpark": Place(52.3900, 12.9950, 48),
    "golm": Place(52.4088, 12.9662, 42),
    "werder": Place(52.3784, 12.9335, 33),
    "ketzin": Place(52.4800, 12.8450, 30),
    # Berlin.
    "berlin-spree": Place(52.5192, 13.3955, 34),
    "berlin-mitte": Place(52.5238, 13.4018, 36),
    "gendarmenmarkt": Place(52.5137, 13.3926, 36),
    "berlin-hbf": Place(52.5251, 13.3694, 35),
    "potsdamer-platz": Place(52.5099, 13.3733, 36),
    "prenzlauer-berg": Place(52.5392, 13.4241, 45),
    "brandenburger-tor": Place(52.5163, 13.3777, 34),
    "wannsee": Place(52.4212, 13.1790, 40),
    "dresden": Place(51.0500, 13.7373, 113),
    # The Baltic: Usedom, Ruegen, Warnemuende.
    "bansin": Place(53.9703, 14.1350, 4),
    "heringsdorf": Place(53.9553, 14.1672, 3),
    "ahlbeck": Place(53.9432, 14.1930, 3),
    "zinnowitz": Place(54.0782, 13.9120, 3),
    "karlshagen": Place(54.1070, 13.8330, 3),
    "kap-arkona": Place(54.6772, 13.4320, 45),
    "warnemuende": Place(54.1818, 12.0862, 3),
    # Lisbon, and Sintra.
    "lisbon-baixa": Place(38.7102, -9.1368, 20, "Europe/Lisbon"),
    "bica": Place(38.7091, -9.1468, 40, "Europe/Lisbon"),
    "alfama": Place(38.7118, -9.1300, 60, "Europe/Lisbon"),
    "graca": Place(38.7161, -9.1311, 80, "Europe/Lisbon"),
    "ribeira": Place(38.7069, -9.1459, 8, "Europe/Lisbon"),
    "bairro-alto": Place(38.7131, -9.1447, 70, "Europe/Lisbon"),
    "belem": Place(38.6916, -9.2160, 5, "Europe/Lisbon"),
    "sintra": Place(38.7980, -9.3880, 200, "Europe/Lisbon"),
    "chiado": Place(38.7107, -9.1420, 50, "Europe/Lisbon"),
    # The Alps.
    "oberstdorf": Place(47.4093, 10.2790, 1250),
    "garmisch": Place(47.4920, 11.0955, 1100),
    "koenigssee": Place(47.5530, 12.9720, 605),
    "oeschinensee": Place(46.4980, 7.7270, 1580, "Europe/Zurich"),
    "zermatt": Place(46.0207, 7.7491, 2600, "Europe/Zurich"),
}


# --- cameras -----------------------------------------------------------------


@dataclass(frozen=True)
class Camera:
    make: str
    model: str
    lens: str
    f_number: float
    focal: float
    focal35: int
    software: str
    lens_make: str = ""


IPHONE = Camera("Apple", "iPhone 16 Pro", "iPhone 16 Pro back triple camera 6.765mm f/1.78",
                1.78, 6.765, 24, "26.6", "Apple")
IPHONE_TELE = Camera("Apple", "iPhone 16 Pro", "iPhone 16 Pro back triple camera 15.66mm f/2.8",
                     2.8, 15.66, 120, "26.6", "Apple")
IPHONE_OLD = Camera("Apple", "iPhone 13 mini", "iPhone 13 mini back dual wide camera 5.1mm f/1.6",
                    1.6, 5.1, 26, "18.6.2", "Apple")
SONY = Camera("SONY", "ILCE-7M4", "FE 24-105mm F4 G OSS", 8.0, 35.0, 35, "ILCE-7M4 v4.01")
FUJI = Camera("FUJIFILM", "X-T5", "XF16-80mmF4 R OIS WR", 5.6, 23.0, 35,
              "Digital Camera X-T5 Ver4.10", "FUJIFILM")

# Exposure by light: (ISO, exposure time in seconds).
LIGHT = {
    "day": (64, 1 / 1200),
    "shade": (80, 1 / 400),
    "dusk": (250, 1 / 60),
    "indoor": (400, 1 / 60),
    "night": (1250, 1 / 30),
}


# --- when ----------------------------------------------------------------------


@dataclass(frozen=True)
class When:
    """A local wall-clock time, relative to the day the library is built."""

    days_ago: int | None = None   # ago(): that many days before today
    month_day: str = ""           # season(): "MM-DD", the most recent one at least MIN_GAP back
    years_back: int = 0
    hm: str = "12:00"

    def local_date(self, today: date) -> date:
        if self.days_ago is not None:
            return today - timedelta(days=self.days_ago)
        month, day = (int(p) for p in self.month_day.split("-"))
        year = today.year
        while True:
            candidate = date(year, month, day)
            if today - candidate >= MIN_GAP:
                break
            year -= 1
        return candidate.replace(year=candidate.year - self.years_back)

    def local(self, today: date) -> datetime:
        hour, minute = (int(p) for p in self.hm.split(":"))
        return datetime.combine(self.local_date(today), time(hour, minute))


def ago(days: int, hm: str) -> When:
    return When(days_ago=days, hm=hm)


def season(month_day: str, hm: str, years_back: int = 0) -> When:
    return When(month_day=month_day, years_back=years_back, hm=hm)


# --- the pictures ---------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    key: str                      # stable, unique; names the file's seconds and its Live Photo id
    src: str | None               # a SOURCES id; None for a screenshot (rendered by shoot.py)
    when: When
    place: str | None = None      # a PLACES key; None: no position in the file
    kind: str = "photo"           # photo | live | video | screenshot | whatsapp
    camera: Camera = IPHONE
    light: str = "day"
    portrait: bool = False
    favorite: bool = False
    albums: tuple[str, ...] = ()
    edited: bool = False          # also write IMG_n-edited.heic, which the grid shows instead
    seconds: int = 8              # videos
    folder: str = ""              # camera imports: the folder name after "YYYY-MM "

    @property
    def root(self) -> str:
        return CAMERA if self.camera in (SONY, FUJI) else ICLOUD


BALTIC = ("Baltic Sea",)
LISBON = ("Lisbon",)
ALPS = ("Hiking in the Alps",)
BIRTHDAY = ("Birthday",)
WHATSAPP = "WhatsApp"

ITEMS: tuple[Item, ...] = (
    # --- the last few days, at home ---------------------------------------------
    Item("coffee-heart", "UXO5P8KFS5", ago(0, "08:14"), "potsdam-centre", light="indoor"),
    Item("golden-retriever", "YAG9NYBYZW", ago(0, "12:31"), "sanssouci", kind="live", favorite=True),
    Item("screen-site", None, ago(0, "13:05"), kind="screenshot"),
    # Later than everything above on purpose: files are numbered in the order
    # they were taken, so a new item anywhere but at the newest end renames
    # the files after it and leaves the old names behind in DEMO_DIR.
    Item("bike-basket", "Y2KCFMGHD1", ago(0, "14:20"), "potsdam-centre"),
    Item("leaf", "KPM7XLOIGZ", ago(0, "15:05"), "sanssouci", light="shade", portrait=True),
    Item("dog-window", "S2QPBLPTL5", ago(0, "15:30"), "potsdam-home", light="indoor"),
    Item("cat-sofa", "KKYDNLT7LH", ago(1, "07:40"), "potsdam-home", light="indoor"),
    Item("sunset-trees", "VSCGP1X7OE", ago(1, "18:47"), "heiliger-see", light="dusk", favorite=True),
    Item("sunset-reflection", "8UOSAA3FRO", ago(1, "18:55"), "heiliger-see", light="dusk", edited=True),
    Item("pizza-oven", "F7EPDH2L1A", ago(1, "20:15"), "potsdam-centre", light="indoor"),
    Item("autumn-park", "YM863UXAFR", ago(2, "16:10"), "sanssouci", light="shade"),
    Item("swan-reeds", "7VITJYYH6K", ago(2, "16:24"), "sanssouci", camera=IPHONE_TELE),
    Item("cows-birches", "F65E5BF6BE", ago(3, "11:05"), "golm"),
    Item("cows-fog", "D810EF71C9", ago(3, "11:08"), "golm", kind="live"),
    Item("cow-video", "MRZZ7VSUNT", ago(3, "11:10"), "golm", kind="video", seconds=9),
    Item("cows-fence", "86HZXEM9SH", ago(3, "11:20"), "golm", favorite=True),
    Item("horses-paddock", "ASKA6Q9G1Y", ago(3, "11:52"), "werder"),
    Item("bike-lane", "P8ZHITFSH3", ago(3, "12:40"), "werder"),
    Item("wa-waffles", "34QDSDEHXG", ago(4, "19:22"), kind="whatsapp"),
    Item("birthday-cake", "W7T1J6B7DM", ago(5, "15:58"), "prenzlauer-berg", light="indoor",
         favorite=True, albums=BIRTHDAY),
    Item("candles", "L28IBU4Y3J", ago(5, "16:02"), "prenzlauer-berg", kind="live", light="indoor",
         albums=BIRTHDAY),
    Item("party-pug", "2N0WSXKY4L", ago(5, "16:30"), "prenzlauer-berg", light="indoor",
         favorite=True, albums=BIRTHDAY),
    Item("wa-cat", "7BXCZNT5JF", ago(6, "09:12"), kind="whatsapp"),
    Item("hauptbahnhof", "A618B01675", ago(9, "15:12"), "berlin-hbf", light="shade"),
    Item("konzerthaus", "803CD628BB", ago(9, "16:40"), "gendarmenmarkt"),
    Item("berlin-sunset", "8A981F99AC", ago(9, "19:02"), "berlin-spree", light="dusk", favorite=True),
    Item("wurst", "863C586BF6", ago(9, "20:10"), "berlin-mitte", light="night"),
    Item("festival-of-lights", "1AE1F86F1B", ago(9, "21:05"), "potsdamer-platz", light="night"),
    Item("wa-pug", "824XB7BXYU", ago(11, "22:48"), kind="whatsapp"),
    Item("screen-features", None, ago(12, "10:20"), kind="screenshot"),
    Item("kitten", "UCS90HFBJL", ago(16, "18:30"), "potsdam-home", light="indoor"),
    # --- the Baltic, in August ------------------------------------------------------
    Item("dune-path", "2PT5PZPXLH", season("08-04", "10:12"), "bansin", favorite=True, albums=BALTIC),
    Item("dune-path-2", "VR4V2BQROC", season("08-04", "10:20"), "bansin", albums=BALTIC),
    Item("beach-umbrella", "MB9BSCD0GM", season("08-04", "14:03"), "heringsdorf", albums=BALTIC),
    Item("pier-long", "BAH9GVSUKZ", season("08-05", "11:30"), "heringsdorf", albums=BALTIC),
    Item("pier-rail", "8M4C9RHYT1", season("08-05", "11:36"), "ahlbeck", albums=BALTIC),
    Item("shell", "T5TB6VX5PG", season("08-05", "17:45"), "ahlbeck", kind="live", albums=BALTIC),
    Item("wave-video", "I3QWA2OLLM", season("08-05", "18:02"), "ahlbeck", kind="video", seconds=12,
         albums=BALTIC),
    Item("puppy-beach", "JVSG0V22X6", season("08-06", "09:15"), "zinnowitz", favorite=True, albums=BALTIC),
    Item("two-dogs", "63348D1909", season("08-06", "09:40"), "zinnowitz", albums=BALTIC),
    Item("bikes-dunes", "BCMY03PRZ9", season("08-07", "13:10"), "karlshagen", albums=BALTIC),
    Item("seagulls-sunset", "DI64TAJTIS", season("08-07", "20:31"), "karlshagen", light="dusk",
         albums=BALTIC),
    Item("person-sea", "FM9CFSXY0B", season("08-08", "19:50"), "zinnowitz", light="dusk", albums=BALTIC),
    Item("chalk-lighthouse", "7201530D4D", season("08-09", "12:20"), "kap-arkona", albums=BALTIC),
    Item("mole-lighthouse", "K9GGGG9QB4", season("08-10", "16:05"), "warnemuende", albums=BALTIC),
    Item("regatta", "P94WAUPGNQ", season("08-10", "16:40"), "warnemuende", camera=IPHONE_TELE,
         albums=BALTIC),
    Item("sailing-video", "17E6A220E6", season("08-10", "17:10"), "warnemuende", kind="video",
         seconds=10, albums=BALTIC),
    Item("harbour-sunset", "D8974ECD93", season("08-10", "20:45"), "warnemuende", light="dusk",
         albums=BALTIC),
    # --- Lisbon, in May: the phone and the Fujifilm ------------------------------------
    Item("rua-augusta", "B9F8B43531", season("05-12", "11:20"), "lisbon-baixa", camera=FUJI,
         folder="Lisbon", albums=LISBON),
    Item("bica-graffiti", "2174058CFB", season("05-12", "15:40"), "bica", camera=FUJI,
         folder="Lisbon", albums=LISBON),
    Item("alfama-roofs", "AFJ2YJAZUK", season("05-13", "10:05"), "alfama", camera=FUJI,
         folder="Lisbon", favorite=True, albums=LISBON),
    Item("tram-28", "VU6VE3P21M", season("05-13", "11:30"), "graca", albums=LISBON),
    Item("tram-video", "VU6VE3P21M", season("05-13", "11:31"), "graca", kind="video", seconds=8,
         albums=LISBON),
    Item("peppers", "BBGPLGHY5B", season("05-13", "13:15"), "ribeira", light="indoor", albums=LISBON),
    Item("fruit-stand", "AA39511B59", season("05-13", "13:22"), "ribeira", light="shade", albums=LISBON),
    Item("alley", "N6LYB1HWK5", season("05-14", "19:40"), "bairro-alto", camera=FUJI, light="dusk",
         portrait=True, folder="Lisbon", albums=LISBON),
    Item("belem-tower", "DJ9G2OU1TO", season("05-15", "10:30"), "belem", camera=FUJI, portrait=True,
         folder="Lisbon", albums=LISBON),
    Item("sintra-tram", "46BADF516C", season("05-15", "15:05"), "sintra", albums=LISBON),
    Item("latte-lisbon", "D322D377AD", season("05-16", "09:05"), "chiado", light="indoor", albums=LISBON),
    # --- spring, at home ----------------------------------------------------------------
    Item("latte-home", "WDJES619M1", season("03-08", "09:30"), "potsdam-home", light="indoor"),
    Item("daffodils", "J4FWC3JXPV", season("04-06", "12:30"), "sanssouci"),
    Item("hyacinths", "GMI1BVKXBN", season("04-12", "11:00"), "bornstedt"),
    Item("ewe-lambs", "MPETRILKHN", season("04-19", "10:45"), "ketzin", favorite=True),
    Item("lambs", "YDO05H0VY2", season("04-19", "10:50"), "ketzin", kind="live"),
    Item("sheep-meadow", "4F944F6A71", season("04-19", "11:05"), "ketzin"),
    Item("blossom", "TQCY3FH54E", season("04-22", "14:10"), "werder"),
    Item("swans-cygnets", "UMEIOOHUSZ", season("05-24", "17:20"), "heiliger-see", camera=IPHONE_TELE),
    # --- winter ----------------------------------------------------------------------------
    Item("christmas-market", "UPPM7VG5M4", season("12-13", "18:20"), "dresden", light="night"),
    Item("fireworks", "N9EDXX8BPQ", season("01-01", "00:04"), "brandenburger-tor", light="night"),
    Item("fireworks-video", "CPLJUAMC1T", season("01-01", "00:06"), "brandenburger-tor",
         kind="video", seconds=8, light="night"),
    Item("snow-avenue", "30A8EFE302", season("01-18", "13:40"), "neuer-garten", favorite=True),
    Item("frosted-trees", "UQYCF0AKLC", season("01-18", "14:05"), "neuer-garten"),
    Item("snow-tracks", "N2OFJ9PRQ6", season("02-02", "10:15"), "wannsee"),
    # --- last autumn, the old phone ------------------------------------------------------
    Item("birch-path", "SBPB82MIOJ", season("10-18", "15:00"), "wildpark", camera=IPHONE_OLD),
    Item("forest-road", "61UZQF1A4B", season("10-25", "14:20"), "wildpark", camera=IPHONE_OLD,
         light="shade"),
    # --- the Alps, the September before: the Sony, and the old phone -----------------------
    Item("green-valley", "Y65WP68WKD", season("09-06", "10:40", 1), "oberstdorf", camera=SONY,
         folder="Alps", albums=ALPS),
    Item("hiker", "ZHC95L56RT", season("09-06", "12:15", 1), "oberstdorf", camera=SONY,
         folder="Alps", albums=ALPS),
    Item("alpine-cow", "QZZC02VYSS", season("09-06", "15:30", 1), "oberstdorf", camera=SONY,
         folder="Alps", favorite=True, albums=ALPS),
    Item("cow-face", "ZUXGSBSYBK", season("09-06", "15:34", 1), "oberstdorf", kind="live",
         camera=IPHONE_OLD, albums=ALPS),
    Item("cow-sunrise", "SQ99X727RI", season("09-08", "07:05", 1), "garmisch", camera=IPHONE_OLD,
         light="dusk", albums=ALPS),
    Item("two-walkers", "THN6VCGM6X", season("09-08", "11:20", 1), "garmisch", camera=SONY,
         folder="Alps", albums=ALPS),
    Item("mountain-reflection", "PW0UP8VIQZ", season("09-09", "08:10", 1), "koenigssee", camera=SONY,
         light="shade", folder="Alps", albums=ALPS),
    Item("turquoise-lake", "5IGV8IZMBT", season("09-11", "13:00", 1), "oeschinensee", camera=SONY,
         folder="Alps", favorite=True, albums=ALPS),
    Item("matterhorn", "RQ2Z75PQIN", season("09-12", "18:30", 1), "zermatt", camera=SONY,
         light="dusk", folder="Alps", albums=ALPS),
)

# The WhatsApp saves are in iCloud's WhatsApp album; nothing else says so.
WHATSAPP_KEYS = tuple(i.key for i in ITEMS if i.kind == "whatsapp")


# --- resolved: names, instants, positions ----------------------------------------------


@dataclass
class Resolved:
    item: Item
    local: datetime                         # wall clock where it was taken
    offset: timedelta                       # of that zone at that moment
    utc: datetime                           # aware
    lat: float | None
    lon: float | None
    alt: float | None
    rel_path: str                           # inside its root
    companion: str = ""                     # a Live Photo's MOV
    edit: str = ""                          # an edited still's -edited file
    content_id: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return [n for n in (self.rel_path, self.companion, self.edit) if n]


def _jitter(key: str, scale: float = 0.0016) -> tuple[float, float]:
    """A stable nudge, so photos taken at one place are not one point."""
    digest = hashlib.blake2b(key.encode(), digest_size=4).digest()
    return ((digest[0] - 127.5) / 127.5 * scale, (digest[1] - 127.5) / 127.5 * scale)


def today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(HOME_ZONE).date()


# The day the files are numbered for, whatever day they are built on. The
# seasonal sets move relative to each other through the year (last October is
# before the Baltic in September and after it in December), and numbering by
# the build day would rename files, leaving the old names behind in DEMO_DIR.
NUMBERING_DAY = date(2026, 9, 29)


def _moment(item: Item, on: date) -> tuple[datetime, datetime]:
    """(local wall clock, aware instant) of an item, for a build day."""
    place = PLACES[item.place] if item.place else None
    zone = ZoneInfo(place.zone) if place else HOME_ZONE
    local = item.when.local(on)
    # A stable second, so two shots in one minute are still ordered.
    local = local.replace(second=int(hashlib.blake2b(item.key.encode(), digest_size=1).digest()[0]) % 60)
    return local, local.replace(tzinfo=zone)


def resolve(on: date | None = None) -> list[Resolved]:
    """Every item with its time, position and file name.

    iPhone files are numbered in the order they were taken (on NUMBERING_DAY),
    as a phone does: IMG_7301 onwards, a Live Photo's still and motion sharing
    a number. The cameras count their own: DSCF (Fujifilm) and DSC0 (Sony).
    """
    on = on or today()
    rows: list[Resolved] = []
    for item in ITEMS:
        place = PLACES[item.place] if item.place else None
        local, aware = _moment(item, on)
        offset = aware.utcoffset() or timedelta(0)
        lat = lon = alt = None
        if place and item.kind not in ("whatsapp", "screenshot"):
            dlat, dlon = _jitter(item.key)
            lat, lon, alt = round(place.lat + dlat, 6), round(place.lon + dlon, 6), place.alt
        rows.append(Resolved(item, local, offset, aware.astimezone(timezone.utc), lat, lon, alt, ""))
    rows.sort(key=lambda r: _moment(r.item, NUMBERING_DAY)[1])

    counters = {"iphone": 7301, "fuji": 2104, "sony": 4480}
    for r in rows:
        item = r.item
        if item.kind == "whatsapp":
            # WhatsApp saves arrive with a random name and nothing else.
            r.rel_path = str(uuid.uuid5(uuid.NAMESPACE_URL, f"meerpic-demo:{item.key}")).upper() + ".JPG"
        elif item.root == CAMERA:
            folder = f"{r.local:%Y-%m} {item.folder}"
            if item.camera is FUJI:
                r.rel_path = f"{folder}/DSCF{counters['fuji']:04d}.JPG"
                counters["fuji"] += 1
            else:
                r.rel_path = f"{folder}/DSC0{counters['sony']:04d}.JPG"
                counters["sony"] += 1
        else:
            stem = f"IMG_{counters['iphone']:04d}"
            counters["iphone"] += 1
            ext = {"video": "MOV", "screenshot": "PNG"}.get(item.kind, "HEIC")
            r.rel_path = f"{stem}.{ext}"
            if item.kind == "live":
                r.companion = f"{stem}.MOV"
                r.content_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"meerpic-demo-live:{item.key}")).upper()
            if item.edited:
                r.edit = f"{stem}-edited.heic"
    return rows


def by_key(rows: list[Resolved]) -> dict[str, Resolved]:
    return {r.item.key: r for r in rows}
