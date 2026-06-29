"""
VenDrop Art Engine — Autonomous Wall Art Design Generator
Uses Claude AI to generate wall art concepts, prompts, and listings
for Amazon/Printify integration.

Niches: Exotic Cars + Trading Motivation Wall Art

Setup:
  pip install anthropic requests schedule python-dotenv pillow

Usage:
  python generate_designs.py              # Run one batch
  python generate_designs.py --loop       # Run continuously on schedule
  python generate_designs.py --export     # Export Printify-ready CSV
"""

import os
import json
import time
import argparse
import schedule
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import anthropic

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OUTPUT_DIR        = Path("output")
DESIGNS_FILE      = OUTPUT_DIR / "designs.json"
LISTINGS_FILE     = OUTPUT_DIR / "amazon_listings.csv"
LOG_FILE          = OUTPUT_DIR / "engine.log"

BATCH_SIZE        = 10          # designs per run
LOOP_INTERVAL_HR  = 6           # hours between auto-runs
MAX_DESIGNS       = 500         # stop when library reaches this size

NICHES = {
    "exotic_cars": {
        "label": "Exotic / Supercar Wall Art",
        "models": ["Lamborghini Huracán", "Ferrari SF90", "McLaren 720S",
                   "Porsche 911 GT3 RS", "Bugatti Chiron", "Rolls-Royce Wraith",
                   "Aston Martin DB12", "Pagani Huayra"],
        "moods":  ["cinematic speed", "garage luxury", "midnight drive",
                   "carbon fiber minimalism", "supercar silhouette"],
        "styles": ["canvas art", "framed poster", "metal print"],
    },
    "trading_motivation": {
        "label": "Trading & Financial Freedom Wall Art",
        "themes": ["stock market charts", "$100 bill art", "bull & bear",
                   "options flow", "risk/reward mindset", "financial freedom",
                   "Wall Street grind", "wealth building", "trader's desk"],
        "quotes": [
            "Risk comes from not knowing what you're doing.",
            "The market rewards patience.",
            "Trade the chart, not the news.",
            "Compound interest is the eighth wonder.",
            "Every dip is an opportunity.",
        ],
        "styles": ["canvas art", "framed poster", "black & white print"],
    },
}

PRICE_RANGES = {
    "canvas art":         {"min": 44.99, "max": 89.99},
    "framed poster":      {"min": 34.99, "max": 64.99},
    "metal print":        {"min": 59.99, "max": 99.99},
    "black & white print":{"min": 29.99, "max": 54.99},
}

# ── Logging ───────────────────────────────────────────────────────────────────

OUTPUT_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("art-engine")

# ── Claude client ─────────────────────────────────────────────────────────────

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Core generation ───────────────────────────────────────────────────────────

def generate_batch(niche_key: str, count: int = BATCH_SIZE) -> list[dict]:
    """
    Call Claude to generate `count` unique wall art concepts for the niche.
    Returns a list of design dicts.
    """
    niche = NICHES[niche_key]

    system_prompt = """You are a senior creative director at a premium wall art brand.
You specialize in two niches: exotic supercars and trading/financial motivation art.
Your designs sell for $35–$100 on Amazon as canvas prints and framed posters.
Buyers are young professional males (22–38) who trade, gym, and dream big.
Every design must feel premium, aspirational, and masculine.
Respond ONLY with valid JSON — no markdown, no explanation."""

    user_prompt = f"""Generate {count} UNIQUE wall art designs for the niche: {niche['label']}

Niche data: {json.dumps(niche, indent=2)}

For each design, return a JSON array of objects with these exact keys:
{{
  "id": "unique_snake_case_id",
  "niche": "{niche_key}",
  "title": "Amazon product title (60–80 chars, keyword-rich)",
  "subtitle": "Short punchy tagline (under 12 words)",
  "style": "canvas art | framed poster | metal print | black & white print",
  "size_options": ["12x16", "16x20", "24x32"],
  "color_palette": ["#hex1", "#hex2", "#hex3"],
  "image_prompt": "Detailed Midjourney/DALL-E prompt (80–120 words) describing the art",
  "amazon_keywords": ["keyword1", "keyword2", ...up to 8],
  "bullet_points": ["Feature 1", "Feature 2", "Feature 3", "Feature 4", "Feature 5"],
  "description": "Amazon product description (150–200 words)",
  "price": 49.99
}}

Return ONLY the JSON array. No markdown. No preamble."""

    log.info(f"Generating {count} designs for niche: {niche_key}")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )

    raw = response.content[0].text.strip()
    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    designs = json.loads(raw)
    ts = datetime.utcnow().isoformat()
    for d in designs:
        d["created_at"] = ts
        d["status"]     = "pending"   # pending | approved | listed
    log.info(f"  ✓ Generated {len(designs)} designs")
    return designs


def load_library() -> list[dict]:
    if DESIGNS_FILE.exists():
        return json.loads(DESIGNS_FILE.read_text())
    return []


def save_library(library: list[dict]):
    DESIGNS_FILE.write_text(json.dumps(library, indent=2))


def run_batch():
    library = load_library()
    if len(library) >= MAX_DESIGNS:
        log.info(f"Library full ({MAX_DESIGNS} designs). Skipping.")
        return

    new_designs = []
    for niche_key in NICHES:
        batch = generate_batch(niche_key, count=BATCH_SIZE // len(NICHES))
        new_designs.extend(batch)

    library.extend(new_designs)
    save_library(library)
    log.info(f"Library now has {len(library)} total designs.")
    export_csv(library)


# ── CSV export (Printify / Amazon ready) ─────────────────────────────────────

def export_csv(library: list[dict]):
    """Write a Printify-compatible CSV for bulk product upload."""
    import csv

    rows = []
    for d in library:
        for size in d.get("size_options", ["12x16", "16x20", "24x32"]):
            w, h = size.split("x")
            rows.append({
                "Title":          d["title"],
                "Description":    d["description"],
                "Price":          d["price"],
                "Style":          d["style"],
                "Width":          w,
                "Height":         h,
                "Keywords":       ", ".join(d.get("amazon_keywords", [])),
                "Bullet1":        d["bullet_points"][0] if len(d["bullet_points"]) > 0 else "",
                "Bullet2":        d["bullet_points"][1] if len(d["bullet_points"]) > 1 else "",
                "Bullet3":        d["bullet_points"][2] if len(d["bullet_points"]) > 2 else "",
                "Bullet4":        d["bullet_points"][3] if len(d["bullet_points"]) > 3 else "",
                "Bullet5":        d["bullet_points"][4] if len(d["bullet_points"]) > 4 else "",
                "Image_Prompt":   d["image_prompt"],
                "Color_Palette":  ", ".join(d.get("color_palette", [])),
                "Niche":          d["niche"],
                "Design_ID":      d["id"],
                "Status":         d["status"],
                "Created_At":     d["created_at"],
            })

    if not rows:
        return

    with open(LISTINGS_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    log.info(f"  ✓ Exported {len(rows)} rows → {LISTINGS_FILE}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VenDrop AI Art Engine")
    parser.add_argument("--loop",   action="store_true", help="Run on schedule")
    parser.add_argument("--export", action="store_true", help="Export CSV only")
    args = parser.parse_args()

    if args.export:
        export_csv(load_library())
        return

    if args.loop:
        log.info(f"Loop mode: running every {LOOP_INTERVAL_HR}h")
        run_batch()  # immediate first run
        schedule.every(LOOP_INTERVAL_HR).hours.do(run_batch)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_batch()


if __name__ == "__main__":
    main()
