from app.ling.romanizer import cyr2lat, lat2cyr, looks_cyrillic_uz, normalize_uzbek


def test_cyr2lat_basic():
    assert cyr2lat("Ўзбек тили") == "O'zbek tili"
    assert cyr2lat("ғалат ҳаво") == "g'alat havo"


def test_lat2cyr_basic():
    assert lat2cyr("O'zbek tili") == "Ўзбек тили"
    assert lat2cyr("g'oz") == "ғоз"
    assert lat2cyr("o'rta") == "ўрта"


def test_roundtrip_latin():
    for word in ["Salom", "rahmat", "ktub", "choy"]:
        assert cyr2lat(lat2cyr(word)) == word


def test_digraph_priority():
    # 'shch' не должно превратиться в 'шх'
    assert lat2cyr("shch") == "щ"
    assert cyr2lat("ущ") == "ushch"  # щ → shch (полный диграф)
    assert cyr2lat("щу") == "shchu"


def test_normalize_apostrophes():
    assert normalize_uzbek("oʻgʻil") == "o'g'il"
    assert normalize_uzbek("gʼira") == "g'ira"


def test_detect_script():
    assert looks_cyrillic_uz("Салом дуруст")
    assert not looks_cyrillic_uz("Salom duRust")
