SOURCE_REGISTRY = {
    "president_press": {
        "display_name": "台湾总统府",
        "section": "official",
        "source_type": "government",
        "document_type": "新闻稿",
        "display_order": 10,
    },
    "mnd_press": {
        "display_name": "台湾国防部",
        "section": "official",
        "source_type": "government",
        "document_type": "新闻稿",
        "display_order": 20,
    },
    "cga_press": {
        "display_name": "台湾海巡署",
        "section": "official",
        "source_type": "government",
        "document_type": "新闻稿",
        "display_order": 30,
    },
    "mofa_press": {
        "display_name": "台湾外交部",
        "section": "official",
        "source_type": "government",
        "document_type": "新闻稿",
        "display_order": 40,
    },
    "dpp_news": {
        "display_name": "民主进步党中央",
        "section": "official",
        "source_type": "party",
        "document_type": "政党声明",
        "display_order": 50,
    },
    "ey_cabinet_news": {
        "display_name": "行政院",
        "section": "official",
        "source_type": "government",
        "document_type": "新聞稿",
        "display_order": 60,
    },
    "ey_ministry_news": {
        "display_name": "行政院",
        "section": "official",
        "source_type": "government",
        "document_type": "新聞稿",
        "display_order": 70,
    },
}

def get_source_info(source_id: str) -> dict:
    return SOURCE_REGISTRY.get(source_id, {})

def is_official_source(source_id: str) -> bool:
    return SOURCE_REGISTRY.get(source_id, {}).get("section") == "official"

def get_official_sources() -> list:
    result = []
    for sid, info in SOURCE_REGISTRY.items():
        if info["section"] == "official":
            result.append((info["display_order"], sid, info))
    result.sort(key=lambda x: x[0])
    return result
