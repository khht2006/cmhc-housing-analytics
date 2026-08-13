"""
Sorting out place names.

This is the trickiest part of the project, and it's worth understanding because
getting it wrong makes every total on the dashboard wrong.

THE PROBLEM
-----------
Statistics Canada puts totals in the same list as the things they're totals of.
So the "GEO" column contains all of these mixed together:

    Ontario                                 <- a real province
    Toronto, Ontario                        <- a real city
    Canada                                  <- the total of all provinces
    Atlantic provinces                      <- the total of NL + PE + NS + NB
    British Columbia excluding Vancouver    <- BC minus one city
    Ottawa-Gatineau, Ontario/Quebec         <- the total of the two rows below
    Ottawa-Gatineau, Ontario part, ...      <- the Ontario half
    Ottawa-Gatineau, Quebec part, ...       <- the Quebec half

If you just do SUM(value) GROUP BY month, you count everything about twice,
because "Canada" gets added on top of all the provinces that make it up.

THE FIX
-------
For each name we work out two things:

    geo_level     is this a country, a province, a city, or something else?
    is_aggregate  is this row a total of other rows in the same list?

Then anything with is_aggregate = True gets left out when we add things up.
"""

# The 10 provinces and their two-letter codes.
PROVINCES = {
    "Newfoundland and Labrador": "NL",
    "Prince Edward Island": "PE",
    "Nova Scotia": "NS",
    "New Brunswick": "NB",
    "Quebec": "QC",
    "Ontario": "ON",
    "Manitoba": "MB",
    "Saskatchewan": "SK",
    "Alberta": "AB",
    "British Columbia": "BC",
}

# Which arrears region each province belongs to. The Canadian Bankers
# Association groups the four Atlantic provinces together and doesn't report
# the territories separately, so this mapping is how we connect their
# delinquency numbers to our housing numbers.
ARREARS_REGION = {
    "NL": "ATLANTIC", "PE": "ATLANTIC", "NS": "ATLANTIC", "NB": "ATLANTIC",
    "QC": "QUEBEC",
    "ON": "ONTARIO",
    "MB": "MANITOBA",
    "SK": "SASKATCHEWAN",
    "AB": "ALBERTA",
    "BC": "BRITISH COLUMBIA",
}

# Names we know are totals rather than real places.
KNOWN_TOTALS = {
    "Canada",
    "Atlantic provinces",
    "Atlantic Region",
    "Prairie provinces",
    "Prairie Region",
    "Census metropolitan areas",
    "Census agglomerations 50,000 and over",
    "Census metropolitan areas and census agglomerations of 50,000 and over",
}


def find_province(name):
    """
    Work out which province a place name belongs to.

    Most city names look like "Toronto, Ontario", so the bit after the last
    comma is usually the province. But there are two special cases.
    """
    # Special case 1: "Ottawa-Gatineau, Ontario part, Ontario/Quebec".
    # This city straddles a border, so the province is the "X part" bit,
    # not the last chunk (which is "Ontario/Quebec" and means both).
    if " part," in name:
        for province in PROVINCES:
            if f"{province} part," in name:
                return province

    # Normal case: take whatever is after the last comma.
    last_chunk = name.rsplit(",", 1)[-1].strip()
    if last_chunk in PROVINCES:
        return last_chunk

    # Special case 2: names like "Montreal excluding Saint-Jerome, Quebec"
    # where the province appears somewhere else in the string.
    for province in PROVINCES:
        if province in name:
            return province

    return None


def classify(name):
    """
    Take a place name and return a dictionary describing it.

    The checks run in order, most specific first, because some names would
    match more than one rule.
    """
    name = name.strip()

    # 1. The whole country.
    if name == "Canada":
        return make_result("Country", None, None, is_aggregate=True, sort_order=0)

    # 2. Known totals like "Atlantic provinces".
    if name in KNOWN_TOTALS:
        return make_result("Region", None, None, is_aggregate=True, sort_order=10)

    # 3. An exact province name.
    if name in PROVINCES:
        code = PROVINCES[name]
        return make_result("Province", name, code,
                           is_aggregate=False, sort_order=20)

    # 4. "British Columbia excluding Vancouver" - this overlaps its province,
    #    so adding it to a provincial total would count things twice.
    if " excluding " in name:
        province = find_province(name)
        return make_result("Region", province, PROVINCES.get(province),
                           is_aggregate=True, sort_order=30)

    # 5. "Saint John, Fredericton, and Moncton, New Brunswick" - three cities
    #    lumped into one row, so it's a total too.
    if ", and " in name:
        province = find_province(name)
        return make_result("Region", province, PROVINCES.get(province),
                           is_aggregate=True, sort_order=35)

    # 6. A city, like "Toronto, Ontario".
    province = find_province(name)
    if "," in name and province is not None:
        last_chunk = name.rsplit(",", 1)[-1].strip()

        # "Ottawa-Gatineau, Ontario/Quebec" (a slash, and no "part") is the
        # combined row that sits next to its own two halves - so it's a total.
        is_combined_row = "/" in last_chunk and " part," not in name

        return make_result("CMA", province, PROVINCES.get(province),
                           is_aggregate=is_combined_row, sort_order=40)

    # 7. Something we don't recognise. We deliberately DON'T guess here - an
    #    unknown name should show up as "Unknown" so we notice it, rather than
    #    quietly being treated as a city and messing up a provincial total.
    return make_result("Unknown", None, None, is_aggregate=False, sort_order=999)


def make_result(geo_level, province_name, province_code, is_aggregate, sort_order):
    """Small helper so every branch above returns the same shape."""
    return {
        "geo_level": geo_level,
        "province_name": province_name,
        "province_code": province_code,
        "arrears_region": ARREARS_REGION.get(province_code),
        "is_aggregate": is_aggregate,
        "sort_order": sort_order,
    }
