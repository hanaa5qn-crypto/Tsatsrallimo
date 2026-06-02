"""Fixed point-to-point rate tables for Gobi Limo / Tsatsral Limo.

DATA is generated from the three pricing workbooks (SF city, SFO, OAK origins);
helper logic is hand-written. Prices are all-inclusive customer totals
(vehicle base + 20% gratuity, no extra fees). Matched by destination ZIP.
Regenerate data with the workbook generator if rates change.
"""
import re

# origin -> { destination_zip: {"Sedan": total, "SUV": total} }
RATE_TABLE = {
    'San Francisco': {
        '93950': {"Sedan": 420, "SUV": 500},
        '94010': {"Sedan": 150, "SUV": 180},
        '94019': {"Sedan": 200, "SUV": 230},
        '94022': {"Sedan": 192, "SUV": 210},
        '94027': {"Sedan": 180, "SUV": 220},
        '94028': {"Sedan": 192, "SUV": 220},
        '94030': {"Sedan": 150, "SUV": 180},
        '94043': {"Sedan": 192, "SUV": 210},
        '94044': {"Sedan": 150, "SUV": 180},
        '94062': {"Sedan": 180, "SUV": 200},
        '94070': {"Sedan": 170, "SUV": 200},
        '94087': {"Sedan": 192, "SUV": 210},
        '94104': {"Sedan": 105, "SUV": 120},
        '94305': {"Sedan": 180, "SUV": 220},
        '94404': {"Sedan": 170, "SUV": 200},
        '94501': {"Sedan": 164, "SUV": 200},
        '94508': {"Sedan": 300, "SUV": 360},
        '94515': {"Sedan": 300, "SUV": 360},
        '94526': {"Sedan": 220, "SUV": 280},
        '94530': {"Sedan": 168, "SUV": 200},
        '94533': {"Sedan": 350, "SUV": 400},
        '94538': {"Sedan": 240, "SUV": 280},
        '94544': {"Sedan": 164, "SUV": 220},
        '94549': {"Sedan": 168, "SUV": 200},
        '94550': {"Sedan": 250, "SUV": 300},
        '94559': {"Sedan": 240, "SUV": 288},
        '94563': {"Sedan": 168, "SUV": 200},
        '94574': {"Sedan": 300, "SUV": 360},
        '94577': {"Sedan": 164, "SUV": 220},
        '94587': {"Sedan": 240, "SUV": 280},
        '94589': {"Sedan": 500, "SUV": 600},
        '94597': {"Sedan": 168, "SUV": 200},
        '94599': {"Sedan": 300, "SUV": 360},
        '94608': {"Sedan": 144, "SUV": 200},
        '94612': {"Sedan": 164, "SUV": 200},
        '94621': {"Sedan": 164, "SUV": 200},
        '94703': {"Sedan": 168, "SUV": 200},
        '94806': {"Sedan": 250, "SUV": 350},
        '94901': {"Sedan": 150, "SUV": 180},
        '94904': {"Sedan": 150, "SUV": 180},
        '94920': {"Sedan": 150, "SUV": 180},
        '94924': {"Sedan": 190, "SUV": 220},
        '94925': {"Sedan": 150, "SUV": 180},
        '94930': {"Sedan": 168, "SUV": 200},
        '94938': {"Sedan": 190, "SUV": 220},
        '94939': {"Sedan": 150, "SUV": 180},
        '94941': {"Sedan": 150, "SUV": 180},
        '94945': {"Sedan": 180, "SUV": 220},
        '94946': {"Sedan": 190, "SUV": 220},
        '94951': {"Sedan": 180, "SUV": 220},
        '94952': {"Sedan": 180, "SUV": 220},
        '94960': {"Sedan": 160, "SUV": 180},
        '94963': {"Sedan": 190, "SUV": 220},
        '94964': {"Sedan": 150, "SUV": 180},
        '94965': {"Sedan": 150, "SUV": 180},
        '94970': {"Sedan": 180, "SUV": 220},
        '94973': {"Sedan": 190, "SUV": 220},
        '95008': {"Sedan": 210, "SUV": 240},
        '95020': {"Sedan": 320, "SUV": 400},
        '95030': {"Sedan": 210, "SUV": 240},
        '95050': {"Sedan": 180, "SUV": 210},
        '95060': {"Sedan": 300, "SUV": 360},
        '95070': {"Sedan": 240, "SUV": 240},
        '95076': {"Sedan": 350, "SUV": 420},
        '95112': {"Sedan": 210, "SUV": 240},
        '95204': {"Sedan": 480, "SUV": 600},
        '95351': {"Sedan": 500, "SUV": 700},
        '95377': {"Sedan": 420, "SUV": 500},
        '95407': {"Sedan": 240, "SUV": 288},
        '95442': {"Sedan": 240, "SUV": 288},
        '95476': {"Sedan": 240, "SUV": 288},
        '95492': {"Sedan": 300, "SUV": 360},
        '95620': {"Sedan": 350, "SUV": 400},
        '95688': {"Sedan": 350, "SUV": 400},
        '95814': {"Sedan": 384, "SUV": 460},
    },
    'SFO': {
        '93950': {"Sedan": 310, "SUV": 360},
        '94010': {"Sedan": 130, "SUV": 175},
        '94019': {"Sedan": 170, "SUV": 230},
        '94022': {"Sedan": 185, "SUV": 240},
        '94027': {"Sedan": 150, "SUV": 190},
        '94028': {"Sedan": 150, "SUV": 190},
        '94030': {"Sedan": 130, "SUV": 175},
        '94043': {"Sedan": 185, "SUV": 240},
        '94044': {"Sedan": 190, "SUV": 220},
        '94062': {"Sedan": 150, "SUV": 190},
        '94070': {"Sedan": 130, "SUV": 175},
        '94087': {"Sedan": 185, "SUV": 240},
        '94104': {"Sedan": 120, "SUV": 150},
        '94305': {"Sedan": 150, "SUV": 190},
        '94404': {"Sedan": 130, "SUV": 175},
        '94501': {"Sedan": 190, "SUV": 240},
        '94508': {"Sedan": 345, "SUV": 400},
        '94515': {"Sedan": 345, "SUV": 400},
        '94526': {"Sedan": 260, "SUV": 310},
        '94530': {"Sedan": 170, "SUV": 210},
        '94533': {"Sedan": 360, "SUV": 420},
        '94538': {"Sedan": 210, "SUV": 270},
        '94544': {"Sedan": 190, "SUV": 260},
        '94549': {"Sedan": 210, "SUV": 250},
        '94550': {"Sedan": 250, "SUV": 310},
        '94559': {"Sedan": 285, "SUV": 325},
        '94563': {"Sedan": 210, "SUV": 250},
        '94574': {"Sedan": 345, "SUV": 400},
        '94577': {"Sedan": 190, "SUV": 260},
        '94587': {"Sedan": 210, "SUV": 270},
        '94589': {"Sedan": 250, "SUV": 300},
        '94597': {"Sedan": 270, "SUV": 360},
        '94599': {"Sedan": 345, "SUV": 400},
        '94608': {"Sedan": 170, "SUV": 210},
        '94612': {"Sedan": 190, "SUV": 240},
        '94621': {"Sedan": 170, "SUV": 220},
        '94703': {"Sedan": 170, "SUV": 210},
        '94806': {"Sedan": 170, "SUV": 210},
        '94901': {"Sedan": 195, "SUV": 240},
        '94904': {"Sedan": 190, "SUV": 220},
        '94920': {"Sedan": 190, "SUV": 220},
        '94924': {"Sedan": 240, "SUV": 320},
        '94925': {"Sedan": 190, "SUV": 220},
        '94930': {"Sedan": 195, "SUV": 240},
        '94938': {"Sedan": 270, "SUV": 320},
        '94939': {"Sedan": 190, "SUV": 220},
        '94941': {"Sedan": 190, "SUV": 220},
        '94945': {"Sedan": 225, "SUV": 315},
        '94946': {"Sedan": 270, "SUV": 320},
        '94951': {"Sedan": 290, "SUV": 350},
        '94952': {"Sedan": 290, "SUV": 350},
        '94960': {"Sedan": 195, "SUV": 240},
        '94963': {"Sedan": 270, "SUV": 320},
        '94964': {"Sedan": 190, "SUV": 220},
        '94965': {"Sedan": 190, "SUV": 220},
        '94970': {"Sedan": 240, "SUV": 320},
        '94973': {"Sedan": 270, "SUV": 320},
        '95008': {"Sedan": 260, "SUV": 310},
        '95020': {"Sedan": 480, "SUV": 520},
        '95030': {"Sedan": 260, "SUV": 310},
        '95050': {"Sedan": 230, "SUV": 290},
        '95060': {"Sedan": 280, "SUV": 330},
        '95070': {"Sedan": 260, "SUV": 310},
        '95076': {"Sedan": 310, "SUV": 360},
        '95112': {"Sedan": 230, "SUV": 290},
        '95204': {"Sedan": 570, "SUV": 600},
        '95351': {"Sedan": 450, "SUV": 500},
        '95377': {"Sedan": 360, "SUV": 420},
        '95407': {"Sedan": 340, "SUV": 390},
        '95442': {"Sedan": 300, "SUV": 350},
        '95476': {"Sedan": 300, "SUV": 350},
        '95492': {"Sedan": 340, "SUV": 390},
        '95620': {"Sedan": 360, "SUV": 420},
        '95688': {"Sedan": 360, "SUV": 420},
        '95814': {"Sedan": 360, "SUV": 420},
    },
    'OAK': {
        '93950': {"Sedan": 420, "SUV": 500},
        '94010': {"Sedan": 180, "SUV": 220},
        '94019': {"Sedan": 220, "SUV": 260},
        '94022': {"Sedan": 200, "SUV": 240},
        '94027': {"Sedan": 180, "SUV": 220},
        '94028': {"Sedan": 200, "SUV": 240},
        '94030': {"Sedan": 180, "SUV": 220},
        '94043': {"Sedan": 180, "SUV": 220},
        '94044': {"Sedan": 300, "SUV": 350},
        '94062': {"Sedan": 180, "SUV": 220},
        '94070': {"Sedan": 180, "SUV": 220},
        '94087': {"Sedan": 180, "SUV": 220},
        '94104': {"Sedan": 164, "SUV": 200},
        '94305': {"Sedan": 180, "SUV": 220},
        '94404': {"Sedan": 180, "SUV": 220},
        '94501': {"Sedan": 150, "SUV": 180},
        '94508': {"Sedan": 270, "SUV": 310},
        '94515': {"Sedan": 270, "SUV": 310},
        '94526': {"Sedan": 200, "SUV": 250},
        '94530': {"Sedan": 150, "SUV": 180},
        '94533': {"Sedan": 250, "SUV": 280},
        '94538': {"Sedan": 190, "SUV": 230},
        '94544': {"Sedan": 150, "SUV": 180},
        '94549': {"Sedan": 170, "SUV": 210},
        '94550': {"Sedan": 200, "SUV": 260},
        '94559': {"Sedan": 240, "SUV": 290},
        '94563': {"Sedan": 170, "SUV": 210},
        '94574': {"Sedan": 270, "SUV": 310},
        '94577': {"Sedan": 150, "SUV": 180},
        '94587': {"Sedan": 190, "SUV": 230},
        '94589': {"Sedan": 500, "SUV": 800},
        '94597': {"Sedan": 170, "SUV": 210},
        '94599': {"Sedan": 270, "SUV": 310},
        '94608': {"Sedan": 150, "SUV": 180},
        '94612': {"Sedan": 150, "SUV": 180},
        '94703': {"Sedan": 150, "SUV": 180},
        '94806': {"Sedan": 250, "SUV": 500},
        '94901': {"Sedan": 185, "SUV": 220},
        '94904': {"Sedan": 185, "SUV": 220},
        '94920': {"Sedan": 185, "SUV": 220},
        '94924': {"Sedan": 250, "SUV": 280},
        '94925': {"Sedan": 185, "SUV": 220},
        '94930': {"Sedan": 185, "SUV": 220},
        '94938': {"Sedan": 230, "SUV": 260},
        '94939': {"Sedan": 185, "SUV": 220},
        '94941': {"Sedan": 185, "SUV": 220},
        '94945': {"Sedan": 200, "SUV": 240},
        '94946': {"Sedan": 230, "SUV": 260},
        '94951': {"Sedan": 200, "SUV": 240},
        '94952': {"Sedan": 200, "SUV": 240},
        '94960': {"Sedan": 185, "SUV": 220},
        '94963': {"Sedan": 230, "SUV": 260},
        '94964': {"Sedan": 185, "SUV": 220},
        '94965': {"Sedan": 185, "SUV": 220},
        '94970': {"Sedan": 250, "SUV": 280},
        '94973': {"Sedan": 230, "SUV": 260},
        '95008': {"Sedan": 240, "SUV": 280},
        '95020': {"Sedan": 320, "SUV": 400},
        '95030': {"Sedan": 240, "SUV": 280},
        '95050': {"Sedan": 180, "SUV": 220},
        '95060': {"Sedan": 300, "SUV": 350},
        '95070': {"Sedan": 240, "SUV": 280},
        '95076': {"Sedan": 350, "SUV": 410},
        '95112': {"Sedan": 240, "SUV": 280},
        '95204': {"Sedan": 400, "SUV": 550},
        '95351': {"Sedan": 400, "SUV": 550},
        '95377': {"Sedan": 300, "SUV": 350},
        '95407': {"Sedan": 300, "SUV": 350},
        '95442': {"Sedan": 240, "SUV": 290},
        '95476': {"Sedan": 240, "SUV": 290},
        '95492': {"Sedan": 350, "SUV": 400},
        '95620': {"Sedan": 384, "SUV": 450},
        '95688': {"Sedan": 250, "SUV": 280},
        '95814': {"Sedan": 384, "SUV": 450},
    },
}

# San Francisco county ZIP codes (the "San Francisco" origin = the whole city).
SF_ZIPS = frozenset({
    '94102', '94103', '94104', '94105', '94107', '94108', '94109', '94110',
    '94111', '94112', '94114', '94115', '94116', '94117', '94118', '94121',
    '94122', '94123', '94124', '94127', '94129', '94130', '94131', '94132',
    '94133', '94134', '94137', '94139', '94140', '94143', '94158', '94159',
    '94160', '94161', '94163', '94164', '94172', '94177', '94188',
})


def extract_zip(address):
    """Return the trailing California ZIP (9xxxx) found in an address, else None."""
    if not address:
        return None
    matches = re.findall(r"\b(9\d{4})\b", str(address))
    return matches[-1] if matches else None


def classify_origin(address):
    """Classify a free-form address as one of our fixed pricing origins.

    Returns "SFO", "OAK", "San Francisco", or None. Airport checks run first so a
    generic Oakland/SF city address is never mistaken for an airport pickup.
    """
    if not address:
        return None
    a = str(address).lower()
    z = extract_zip(address)
    # SFO airport
    if "sfo" in a or "san francisco international" in a or z == "94128":
        return "SFO"
    # OAK airport (require an airport signal; Oakland *city* is a destination)
    if (
        re.search(r"\boak\b", a)
        or ("oakland" in a and "airport" in a)
        or ("metropolitan oakland" in a)
        or ("1 airport dr" in a and "oakland" in a)
    ):
        return "OAK"
    # San Francisco city
    if z in SF_ZIPS:
        return "San Francisco"
    if "san francisco" in a and z is None:
        return "San Francisco"
    return None


def fixed_quote(pickup, dropoff, vehicle):
    """Return the all-inclusive total for a known route, or None to use the formula.

    Bidirectional: a price listed origin->destination also applies destination->origin.
    """
    if vehicle not in ("Sedan", "SUV"):
        return None
    # Forward: pickup is an origin, dropoff is the destination.
    origin = classify_origin(pickup)
    if origin:
        rec = RATE_TABLE.get(origin, {}).get(extract_zip(dropoff) or "")
        if rec and rec.get(vehicle) is not None:
            return rec[vehicle]
    # Reverse: dropoff is an origin, pickup is the destination.
    origin = classify_origin(dropoff)
    if origin:
        rec = RATE_TABLE.get(origin, {}).get(extract_zip(pickup) or "")
        if rec and rec.get(vehicle) is not None:
            return rec[vehicle]
    return None
