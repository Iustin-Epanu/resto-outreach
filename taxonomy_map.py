#!/usr/bin/env python3
"""
Map Google Places (New) `types[]` to the Resto platform's exact cuisine + tag
slugs.

The slugs MUST match the lookup tables in the web repo's schema (cuisines / tags
seed). We only ever emit slugs that exist there — an unknown type is ignored,
never invented. When Places types are too generic (just `restaurant`), we fall
back to keyword inference on the venue name.
"""

from __future__ import annotations

from inference import infer_cuisine_from_name

# Valid cuisine slugs (must match the web `cuisines` table).
VALID_CUISINES = {
    "romanian", "italian", "pizza", "japanese", "asian", "indian", "greek",
    "mediterranean", "mideast", "mexican", "french", "seafood", "burger",
    "steak", "vegetarian", "coffee", "bakery", "bar", "fastfood", "international",
}

# Valid tag slugs (must match the web `tags` table), by category.
VALID_TAGS = {
    # setting
    "terrace", "rooftop", "garden", "cafe", "bistro", "fine-dining", "casual",
    "quick-bite", "street-food", "brunch", "breakfast",
    # vibe
    "live-music", "cocktails", "craft-beer", "wine", "romantic",
    "group-friendly", "work-friendly", "cozy", "lively", "late-night",
    # dietary
    "vegetarian-friendly", "vegan-options", "pet-friendly", "family-friendly",
}

# Google Places (New) primary/secondary type -> our cuisine slug.
TYPE_TO_CUISINE = {
    "pizza_restaurant": "pizza",
    "italian_restaurant": "italian",
    "sushi_restaurant": "japanese",
    "japanese_restaurant": "japanese",
    "ramen_restaurant": "japanese",
    "chinese_restaurant": "asian",
    "asian_restaurant": "asian",
    "thai_restaurant": "asian",
    "korean_restaurant": "asian",
    "vietnamese_restaurant": "asian",
    "indonesian_restaurant": "asian",
    "indian_restaurant": "indian",
    "greek_restaurant": "greek",
    "mediterranean_restaurant": "mediterranean",
    "spanish_restaurant": "mediterranean",
    "middle_eastern_restaurant": "mideast",
    "lebanese_restaurant": "mideast",
    "turkish_restaurant": "mideast",
    "afghani_restaurant": "mideast",
    "mexican_restaurant": "mexican",
    "brazilian_restaurant": "mexican",
    "latin_american_restaurant": "mexican",
    "french_restaurant": "french",
    "seafood_restaurant": "seafood",
    "steak_house": "steak",
    "barbecue_restaurant": "steak",
    "hamburger_restaurant": "burger",
    "fast_food_restaurant": "fastfood",
    "vegetarian_restaurant": "vegetarian",
    "vegan_restaurant": "vegetarian",
    "coffee_shop": "coffee",
    "cafe": "coffee",
    "breakfast_restaurant": "coffee",
    "brunch_restaurant": "coffee",
    "bakery": "bakery",
    "dessert_shop": "bakery",
    "ice_cream_shop": "bakery",
    "bar": "bar",
    "pub": "bar",
    "wine_bar": "bar",
    "bar_and_grill": "bar",
    "american_restaurant": "international",
    "restaurant": "international",
}

# Google Places type -> tag slug(s) that the type implies directly.
TYPE_TO_TAGS = {
    "cafe": ["cafe"],
    "coffee_shop": ["cafe"],
    "breakfast_restaurant": ["breakfast"],
    "brunch_restaurant": ["brunch"],
    "bakery": ["quick-bite"],
    "fast_food_restaurant": ["quick-bite", "street-food"],
    "hamburger_restaurant": ["casual", "quick-bite"],
    "bar": ["cocktails"],
    "pub": ["craft-beer"],
    "wine_bar": ["wine"],
    "bar_and_grill": ["group-friendly"],
    "steak_house": ["group-friendly"],
    "fine_dining_restaurant": ["fine-dining"],
    "vegetarian_restaurant": ["vegetarian-friendly", "vegan-options"],
    "vegan_restaurant": ["vegan-options"],
    "meal_takeaway": ["quick-bite"],
}

# Starter tags per cuisine when the Places types imply none.
STARTER_TAGS = {
    "romanian": ["casual"], "italian": ["casual"], "pizza": ["casual", "quick-bite"],
    "japanese": ["casual"], "asian": ["casual", "quick-bite"], "indian": ["vegetarian-friendly"],
    "greek": ["casual", "terrace"], "mediterranean": ["casual"], "mideast": ["quick-bite", "street-food"],
    "mexican": ["lively", "cocktails"], "french": ["romantic", "wine"], "seafood": ["fine-dining"],
    "burger": ["casual", "quick-bite"], "steak": ["group-friendly"],
    "vegetarian": ["vegetarian-friendly", "vegan-options"], "coffee": ["cafe", "brunch", "breakfast"],
    "bakery": ["cafe", "quick-bite"], "bar": ["cocktails", "live-music", "craft-beer"],
    "fastfood": ["quick-bite", "street-food"], "international": ["casual"],
}


def map_taxonomy(place_types, name: str):
    """
    Returns (cuisine_key, cuisine_slugs, tag_slugs).

    cuisine_key is the internal key used by inference.py for copy/imagery;
    cuisine_slugs / tag_slugs are the validated DB slug arrays.
    """
    types = [t for t in (place_types or [])]

    # 1. Cuisine — first specific Places type wins; else infer from the name.
    cuisine_key = None
    for t in types:
        slug = TYPE_TO_CUISINE.get(t)
        if slug and slug != "international":
            cuisine_key = slug
            break
    if cuisine_key is None:
        cuisine_key = infer_cuisine_from_name(name)

    cuisine_slugs = [cuisine_key] if cuisine_key in VALID_CUISINES else ["international"]

    # 2. Tags — union of type-implied tags + cuisine starter tags.
    tags: list[str] = []
    for t in types:
        for tag in TYPE_TO_TAGS.get(t, []):
            if tag not in tags:
                tags.append(tag)
    if not tags:
        tags = list(STARTER_TAGS.get(cuisine_key, ["casual"]))

    tag_slugs = [t for t in tags if t in VALID_TAGS] or ["casual"]
    return cuisine_key, cuisine_slugs, tag_slugs
