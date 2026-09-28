import json
import math
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv()

PLATFORM_URLS = {
    "amazon": "https://www.amazon.in/s?k=",
    "flipkart": "https://www.flipkart.com/search?q=",
    "ikea": "https://www.ikea.com/in/en/search/?q=",
    "myntra": "https://www.myntra.com/",
    "ajio": "https://www.ajio.com/search/?text=",
    "swiggy": "https://www.swiggy.com/search?query=",
    "zomato": "https://www.zomato.com/search?q=",
    "google": "https://www.google.com/search?q=",
    "booking": "https://www.booking.com/searchresults.html?ss=",
    "makemytrip": "https://www.google.com/search?q=site%3Amakemytrip.com+",
    "oyorooms": "https://www.google.com/search?q=site%3Aoyorooms.com+",
    "nobroker": "https://www.google.com/search?q=site%3Anobroker.in+",
    "bookmyshow": "https://www.google.com/search?q=site%3Ain.bookmyshow.com+",
    "bluestone": "https://www.bluestone.com/jewellery/search.html?query=",
    "tanishq": "https://www.tanishq.co.in/search?q=",
    "caratlane": "https://www.caratlane.com/search/?q=",
    "melorra": "https://www.google.com/search?q=site%3Amelorra.com+",
    "meesho": "https://www.meesho.com/search?q=",
}

PARTY_PLATFORMS = {
    "venue": ["google", "booking", "makemytrip", "oyorooms", "nobroker"],
    "catering": ["swiggy", "zomato"],
    "food": ["swiggy", "zomato"],
    "decoration": ["amazon", "flipkart", "meesho", "myntra"],
    "entertainment": ["bookmyshow", "amazon", "flipkart"],
}
JEWELRY_PLATFORMS = ["amazon", "flipkart", "bluestone", "tanishq", "caratlane", "melorra", "meesho"]
HOME_PLATFORMS = ["amazon", "flipkart", "ikea", "myntra", "ajio"]


def ai_is_configured() -> bool:
    return bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))


def extract_json_from_response(response: str | Any) -> dict[str, Any]:
    """Extract a JSON object while tolerating fences, leading prose, and trailing commas."""
    text = getattr(response, "text", response)
    if not isinstance(text, str):
        raise ValueError("The model returned no text.")
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text, flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        candidate = text[match.start():]
        try:
            value, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                value = json.loads(repaired)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            return value
    raise ValueError("The model response did not contain a valid JSON object.")


def _search_links(search_terms: str, platforms: list[str]) -> dict[str, str]:
    query = quote_plus(search_terms.strip() or "budget friendly India")
    return {platform: f"{PLATFORM_URLS[platform]}{query}" for platform in platforms if platform in PLATFORM_URLS}


def _model():
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        return genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Gemini could not be initialized: {error}") from error


def _generate_json(prompt: str, fallback: dict[str, Any], image_path: str | Path | None = None) -> tuple[dict[str, Any], str]:
    model = _model()
    if model is None:
        return fallback, "budget-safe fallback"

    image = None
    if image_path is not None:
        try:
            from PIL import Image

            with Image.open(image_path) as opened:
                image = opened.copy()
        except Exception as error:
            raise HTTPException(status_code=400, detail=f"The uploaded image could not be read: {error}") from error

    strict_prompt = prompt + "\nReturn exactly one valid JSON object. Do not include Markdown, comments, prose, or trailing commas. Use double-quoted JSON keys and ensure every field matches the requested schema."
    for attempt in range(2):
        try:
            request = strict_prompt if attempt else prompt
            content = [request, image] if image is not None else request
            response = model.generate_content(content, generation_config={"temperature": 0.35, "response_mime_type": "application/json"})
            try:
                return extract_json_from_response(response), "Gemini"
            except ValueError:
                if attempt == 1:
                    break
        except HTTPException:
            raise
        except Exception as error:
            return fallback, "budget-safe fallback"
    return fallback, "budget-safe fallback"


def _text(value: Any, default: str = "") -> str:
    return str(value).strip()[:500] if value is not None else default


def _positive_number(value: Any, default: float = 0) -> float:
    try:
        parsed = float(value)
        return max(0.0, parsed) if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _fit_allocations(categories: list[dict[str, Any]], budget: float) -> list[dict[str, Any]]:
    raw = [max(0.0, _positive_number(category.get("allocation"))) for category in categories]
    total = sum(raw)
    if not total:
        raw = [1.0] * len(categories)
        total = float(len(categories) or 1)
    target_cents = min(int(round(budget * 100)), int(round(total * 100)))
    remaining_cents = target_cents
    allocations: list[dict[str, Any]] = []
    for index, (category, amount) in enumerate(zip(categories, raw)):
        cents = remaining_cents if index == len(raw) - 1 else min(remaining_cents, int(round(amount / total * target_cents)))
        remaining_cents -= cents
        items = []
        item_remaining = cents / 100
        source_items = category.get("items", [])
        if not isinstance(source_items, list):
            source_items = []
        for item in source_items[:20]:
            if not isinstance(item, dict) or item_remaining <= 0:
                continue
            try:
                quantity = max(1, min(50, int(_positive_number(item.get("quantity"), 1))))
            except (ValueError, OverflowError):
                quantity = 1
            unit = _positive_number(item.get("estimated_price"), item_remaining / quantity)
            if unit * quantity > item_remaining:
                unit = item_remaining / quantity
            if unit <= 0:
                continue
            items.append({
                "name": _text(item.get("name"), "Budget-friendly pick"),
                "description": _text(item.get("description"), "Compare current prices before purchasing."),
                "estimated_price": round(unit, 2),
                "quantity": quantity,
                "search_terms": _text(item.get("search_terms"), _text(item.get("name"), "budget friendly India")),
            })
            item_remaining = max(0.0, item_remaining - unit * quantity)
        allocations.append({"category": _text(category.get("category"), "Essentials"), "allocation": cents / 100, "items": items})
    return allocations


def _category_links(categories: list[dict[str, Any]], planner: str) -> None:
    for category in categories:
        for item in category["items"]:
            if planner == "party":
                key = category["category"].casefold()
                platforms = next((value for name, value in PARTY_PLATFORMS.items() if name in key), ["amazon", "flipkart", "google"])
            else:
                platforms = HOME_PLATFORMS
            item["shopping_links"] = _search_links(item["search_terms"], platforms)


def _fallback_home(data: dict[str, Any]) -> dict[str, Any]:
    budget = data["total_budget"]
    requested = [
        ("Lighting", "LED ceiling light", "warm white LED ceiling light India", data.get("number_of_lights", 0), 0.12),
        ("Ceiling fans", "Energy-efficient ceiling fan", "energy efficient ceiling fan India", data.get("ceiling_fans", 0), 0.18),
        ("Furniture", "Compact multi-purpose furniture", "compact multipurpose furniture India", data.get("furniture_pieces", 0), 0.31),
        ("Dining", "Four-seater dining table", "affordable four seater dining table India", data.get("dining_tables", 0), 0.20),
    ]
    for room in data.get("rooms", []):
        requested.append((room, f"{room} essentials", f"{room.lower()} furniture essentials India", 1, 0.08))
    chosen = [item for item in requested if item[3] > 0]
    if not chosen:
        chosen = [("Home essentials", "Home comfort essentials", "affordable home essentials India", 1, 1.0)]
    weight_total = sum(item[4] for item in chosen)
    categories = []
    for category, name, terms, quantity, weight in chosen:
        allocation = budget * weight / weight_total
        categories.append({"category": category, "allocation": allocation, "items": [{"name": name, "description": f"A practical choice for a {category.lower()} refresh within your stated budget.", "estimated_price": allocation * 0.8 / quantity, "quantity": quantity, "search_terms": terms}]})
    suggestions = ["Compare warranty, delivery and installation fees before ordering."]
    if data.get("additional_requirements"):
        suggestions.append(f"Keep these requirements in mind: {data['additional_requirements'][:180]}")
    return {"total_budget": budget, "budget_breakdown": categories, "calculation_table": [{"category": item["category"], "allocation": round(item["allocation"], 2)} for item in categories], "additional_suggestions": suggestions}


def get_home_recommendations(budget_input: Any) -> dict[str, Any]:
    data = budget_input.model_dump() if hasattr(budget_input, "model_dump") else dict(budget_input)
    budget = float(data["total_budget"])
    fallback = _fallback_home(data)
    prompt = f"You are an India-focused home interior budget adviser. Prices must be in INR (₹), using Indian brands and shopping platforms. Allocate the budget by room and requested items; prioritize realistic, clearly estimated prices. Ensure total allocated cost does not exceed the given budget of ₹{budget:.2f}. Requested items: {json.dumps(data, ensure_ascii=False)}. Respond ONLY with valid JSON using this exact shape: {{\"total_budget\": number, \"budget_breakdown\": [{{\"category\": string, \"allocation\": number, \"items\": [{{\"name\": string, \"description\": string, \"estimated_price\": number, \"quantity\": integer, \"search_terms\": string}}]}}], \"calculation_table\": [{{\"category\": string, \"allocation\": number}}], \"remaining_budget\": number, \"additional_suggestions\": [string]}}."
    result, source = _generate_json(prompt, fallback)
    categories = result.get("budget_breakdown", [])
    if not isinstance(categories, list) or not categories:
        categories, source = fallback["budget_breakdown"], "budget-safe fallback"
    clean = _fit_allocations([category for category in categories if isinstance(category, dict)][:20], budget)
    if not clean:
        clean, source = _fit_allocations(fallback["budget_breakdown"], budget), "budget-safe fallback"
    _category_links(clean, "home")
    allocated = round(sum(category["allocation"] for category in clean), 2)
    suggestions = result.get("additional_suggestions", fallback["additional_suggestions"])
    if not isinstance(suggestions, list):
        suggestions = fallback["additional_suggestions"]
    return {"total_budget": budget, "budget_breakdown": clean, "calculation_table": [{"category": item["category"], "allocation": item["allocation"]} for item in clean], "remaining_budget": max(0, round(budget - allocated, 2)), "additional_suggestions": [str(value)[:500] for value in suggestions[:12] if isinstance(value, str)], "source": source}


def _fallback_party(data: dict[str, Any]) -> dict[str, Any]:
    budget = data["total_budget"]
    choices = [
        ("Catering", "Party catering package", "party catering packages India", data.get("needs_catering", True), 0.40),
        ("Decoration", "Reusable party decoration set", "reusable party decoration set India", data.get("needs_decoration", True), 0.15),
        ("Entertainment", "Music and entertainment", "party music entertainment India", data.get("needs_entertainment", False), 0.12),
        ("Venue", f"{data.get('venue_type') or 'Local'} event venue", f"{data.get('venue_type') or 'affordable'} event venue India", True, 0.25),
        ("Contingency", "Event-day contingency", "event emergency essentials India", True, 0.08),
    ]
    chosen = [item for item in choices if item[3]]
    weight_total = sum(item[4] for item in chosen)
    categories = [{"category": title, "allocation": budget * weight / weight_total, "items": [{"name": name, "description": f"An indicative option for {data['guest_count']} guests; confirm availability and final pricing.", "estimated_price": budget * weight / weight_total * 0.85, "quantity": 1, "search_terms": terms}]} for title, name, terms, _, weight in chosen]
    return {"total_budget": budget, "budget_breakdown": categories, "additional_suggestions": ["Confirm taxes, service charges, setup and cancellation terms with every vendor.", "Keep a small on-the-day reserve for last-minute needs."], "venue_suggestions": [f"Compare three {data.get('venue_type') or 'local'} venues near your guests and confirm capacity, accessibility and included services."]}


def get_party_recommendations(budget_input: Any) -> dict[str, Any]:
    data = budget_input.model_dump() if hasattr(budget_input, "model_dump") else dict(budget_input)
    budget = float(data["total_budget"])
    fallback = _fallback_party(data)
    prompt = f"You are an India-focused party and event budget adviser. Prices must be in INR (₹), using realistic Indian vendors and platforms. Allocate proportionally across catering, decoration, entertainment, venue, and contingency, respecting only the requested services. Ensure total allocated cost does not exceed the given budget of ₹{budget:.2f}. Plan details: {json.dumps(data, ensure_ascii=False)}. Respond ONLY with valid JSON using this shape: {{\"total_budget\": number, \"budget_breakdown\": [{{\"category\": string, \"allocation\": number, \"items\": [{{\"name\": string, \"description\": string, \"estimated_price\": number, \"quantity\": integer, \"search_terms\": string}}]}}], \"calculation_table\": [{{\"category\": string, \"allocation\": number}}], \"remaining_budget\": number, \"venue_suggestions\": [string], \"additional_suggestions\": [string]}}."
    result, source = _generate_json(prompt, fallback)
    categories = result.get("budget_breakdown", [])
    if not isinstance(categories, list) or not categories:
        categories, source = fallback["budget_breakdown"], "budget-safe fallback"
    clean = _fit_allocations([category for category in categories if isinstance(category, dict)][:20], budget)
    if not clean:
        clean, source = _fit_allocations(fallback["budget_breakdown"], budget), "budget-safe fallback"
    _category_links(clean, "party")
    allocated = round(sum(category["allocation"] for category in clean), 2)
    suggestions = result.get("additional_suggestions", fallback["additional_suggestions"])
    venues = result.get("venue_suggestions", fallback["venue_suggestions"])
    if not isinstance(suggestions, list):
        suggestions = fallback["additional_suggestions"]
    if not isinstance(venues, list):
        venues = fallback["venue_suggestions"]
    return {"total_budget": budget, "budget_breakdown": clean, "calculation_table": [{"category": item["category"], "allocation": item["allocation"]} for item in clean], "remaining_budget": max(0, round(budget - allocated, 2)), "venue_suggestions": [str(value)[:500] for value in venues[:10] if isinstance(value, str)], "additional_suggestions": [str(value)[:500] for value in suggestions[:12] if isinstance(value, str)], "source": source}


def _fallback_jewelry(data: dict[str, Any], has_image: bool) -> dict[str, Any]:
    budget = data["total_budget"]
    preferences = data.get("preferences", "")
    types = [("Earrings", 0.25), ("Necklace", 0.40), ("Bracelet", 0.20), ("Ring", 0.15)]
    return {
        "outfit_analysis": {"colors": ["Image analysis requires a Gemini API key."], "style": "Not analyzed", "formality": "Not analyzed"} if has_image else None,
        "total_budget": budget,
        "jewelry_recommendations": [{"item_type": item, "description": f"A versatile {item.lower()} idea for {data['occasion']}; compare current metal and stone options.", "style": preferences[:160] or "Versatile everyday style", "estimated_price": budget * share, "search_terms": f"{data['occasion']} {preferences} {item} jewelry India".strip()} for item, share in types],
        "remaining_budget": 0,
        "styling_tips": ["Check metal purity, hallmark, return policy and making charges before buying.", "Choose one focal piece and keep accompanying jewelry balanced."],
    }


def get_jewelry_recommendations(budget_input: Any, image_path: str | Path | None = None) -> dict[str, Any]:
    data = budget_input.model_dump() if hasattr(budget_input, "model_dump") else dict(budget_input)
    budget = float(data["total_budget"])
    fallback = _fallback_jewelry(data, image_path is not None)
    image_instruction = " Analyze the attached outfit image for visible colors, style and formality; include a concise outfit_analysis object matched to your jewelry picks." if image_path else " Do not invent visual attributes; use only the supplied occasion and preferences. Set outfit_analysis to null."
    prompt = f"You are an India-focused jewelry stylist. Prices must be in INR (₹), with Indian jewelry brands and Indian shopping platforms. Ensure the total estimated jewelry cost does not exceed the given budget of ₹{budget:.2f}. Occasion and preferences: {json.dumps(data, ensure_ascii=False)}.{image_instruction} Respond ONLY with valid JSON using this exact shape: {{\"outfit_analysis\": {{\"colors\": [string], \"style\": string, \"formality\": string}} or null, \"total_budget\": number, \"jewelry_recommendations\": [{{\"item_type\": string, \"description\": string, \"style\": string, \"estimated_price\": number, \"search_terms\": string}}], \"remaining_budget\": number, \"styling_tips\": [string]}}."
    result, source = _generate_json(prompt, fallback, image_path)
    raw_items = result.get("jewelry_recommendations", [])
    if not isinstance(raw_items, list) or not raw_items:
        raw_items, source = fallback["jewelry_recommendations"], "budget-safe fallback"
    remaining = budget
    clean_items = []
    for item in [entry for entry in raw_items if isinstance(entry, dict)][:20]:
        if remaining <= 0:
            break
        price = min(remaining, _positive_number(item.get("estimated_price"), remaining / max(1, len(raw_items))))
        if price <= 0:
            continue
        terms = _text(item.get("search_terms"), f"{data['occasion']} jewelry India")
        clean_items.append({"item_type": _text(item.get("item_type"), "Jewelry"), "description": _text(item.get("description"), "Compare quality, prices and return policies."), "style": _text(item.get("style"), data.get("preferences", "Versatile style")), "estimated_price": round(price, 2), "search_terms": terms, "shopping_links": _search_links(terms, JEWELRY_PLATFORMS)})
        remaining = max(0, round(remaining - price, 2))
    tips = result.get("styling_tips", fallback["styling_tips"])
    if not isinstance(tips, list):
        tips = fallback["styling_tips"]
    return {"outfit_analysis": result.get("outfit_analysis", fallback["outfit_analysis"]), "total_budget": budget, "jewelry_recommendations": clean_items or fallback["jewelry_recommendations"], "remaining_budget": remaining if clean_items else 0, "styling_tips": [str(value)[:500] for value in tips[:12] if isinstance(value, str)], "source": source}
