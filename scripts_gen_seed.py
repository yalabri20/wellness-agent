"""One-shot generator for seed JSON files (Saudi-context demo data)."""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "seed"
SEED.mkdir(exist_ok=True)

random.seed(42)

DEPARTMENTS = [
    "Engineering",
    "Sales",
    "Operations",
    "Finance",
    "HR",
    "Support",
]

DIETARY_POOL = [
    [],
    ["vegetarian"],
    ["no_nuts"],
    ["vegetarian", "no_nuts"],
    ["halal"],
    ["high_protein"],
]


def build_employees() -> list[dict]:
    employees: list[dict] = []
    # Ensure roughly equal department distribution (~6-7 each)
    dept_cycle = (DEPARTMENTS * 7)[:40]
    for i in range(40):
        emp_id = f"emp_{i+1:02d}"
        if i == 0:
            emp_id = "demo_user"
        employees.append(
            {
                "id": emp_id,
                "department": dept_cycle[i],
                "daily_allowance_sar": round(random.uniform(25, 40), 1),
                "dietary_tags": random.choice(DIETARY_POOL),
                "opted_in": random.random() > 0.12 if i != 0 else True,
                "language": random.choice(["en", "ar", "en", "en"]),
                "live_demo_user": i == 0,
            }
        )
    employees[0]["language"] = "en"
    employees[0]["dietary_tags"] = ["no_nuts"]
    employees[0]["daily_allowance_sar"] = 35.0
    return employees


def build_calendars(employees: list[dict], day: date) -> dict:
    calendars: dict = {}
    for emp in employees:
        uid = emp["id"]
        if uid == "demo_user":
            events = [
                {
                    "start": f"{day.isoformat()}T10:00:00",
                    "end": f"{day.isoformat()}T10:15:00",
                    "importance": "low",
                    "attendee_count": 8,
                },
                {
                    "start": f"{day.isoformat()}T11:00:00",
                    "end": f"{day.isoformat()}T11:45:00",
                    "importance": "high",
                    "attendee_count": 5,
                },
                {
                    "start": f"{day.isoformat()}T13:00:00",
                    "end": f"{day.isoformat()}T14:00:00",
                    "importance": "high",
                    "attendee_count": 4,
                },
                {
                    "start": f"{day.isoformat()}T14:00:00",
                    "end": f"{day.isoformat()}T15:00:00",
                    "importance": "medium",
                    "attendee_count": 3,
                },
                {
                    "start": f"{day.isoformat()}T15:00:00",
                    "end": f"{day.isoformat()}T16:00:00",
                    "importance": "medium",
                    "attendee_count": 5,
                },
                {
                    "start": f"{day.isoformat()}T16:00:00",
                    "end": f"{day.isoformat()}T16:30:00",
                    "importance": "low",
                    "attendee_count": 2,
                },
            ]
        else:
            events = []
            # morning standup-ish
            if random.random() > 0.3:
                start_h = random.choice([9, 10, 11])
                events.append(
                    {
                        "start": f"{day.isoformat()}T{start_h:02d}:{random.choice([0,15,30]):02d}:00",
                        "end": f"{day.isoformat()}T{start_h:02d}:{random.choice([30,45]):02d}:00",
                        "importance": random.choice(["low", "medium"]),
                        "attendee_count": random.randint(2, 12),
                    }
                )
            # afternoon block
            if random.random() > 0.25:
                start_h = random.choice([13, 14, 15])
                duration = random.choice([30, 45, 60])
                start = datetime.combine(day, datetime.min.time()).replace(
                    hour=start_h, minute=random.choice([0, 15, 30])
                )
                end = start + timedelta(minutes=duration)
                events.append(
                    {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "importance": random.choice(["low", "medium", "high"]),
                        "attendee_count": random.randint(1, 10),
                    }
                )
        calendars[uid] = events
    return calendars


def build_meals() -> list[dict]:
    meals = [
        {
            "id": "meal_protein_labneh",
            "name_en": "Labneh Cup with Cucumber",
            "name_ar": "لبنّة مع خيار",
            "restaurant": "Tamimi Fresh",
            "price_sar": 12.0,
            "est_macros": {
                "kcal": 220,
                "protein_g": 14,
                "refined_carbs_g": 8,
                "fiber_g": 3,
                "fat_g": 12,
                "sugar_g": 4,
            },
            "tags": ["high_protein", "light", "pre_meeting", "vegetarian", "no_nuts"],
            "prep_minutes": 15,
        },
        {
            "id": "meal_boiled_eggs",
            "name_en": "Boiled Eggs & Dates Box",
            "name_ar": "بيض مسلوق وتمور",
            "restaurant": "Barn's",
            "price_sar": 14.0,
            "est_macros": {
                "kcal": 280,
                "protein_g": 18,
                "refined_carbs_g": 20,
                "fiber_g": 3,
                "fat_g": 12,
                "sugar_g": 16,
            },
            "tags": ["high_protein", "pre_meeting", "no_nuts"],
            "prep_minutes": 20,
        },
        {
            "id": "meal_greek_yogurt",
            "name_en": "Greek Yogurt & Berries",
            "name_ar": "زبادي يوناني وتوت",
            "restaurant": "Caffeine Life",
            "price_sar": 18.0,
            "est_macros": {
                "kcal": 250,
                "protein_g": 20,
                "refined_carbs_g": 18,
                "fiber_g": 4,
                "fat_g": 6,
                "sugar_g": 14,
            },
            "tags": ["high_protein", "light", "pre_meeting", "vegetarian", "no_nuts"],
            "prep_minutes": 18,
        },
        {
            "id": "meal_chicken_salad",
            "name_en": "Grilled Chicken Salad",
            "name_ar": "سلطة دجاج مشوي",
            "restaurant": "Albaik Express Box",
            "price_sar": 28.0,
            "est_macros": {
                "kcal": 380,
                "protein_g": 32,
                "refined_carbs_g": 12,
                "fiber_g": 6,
                "fat_g": 16,
                "sugar_g": 5,
            },
            "tags": ["high_protein", "light", "pre_meeting", "no_nuts"],
            "prep_minutes": 25,
        },
        {
            "id": "meal_falafel_wrap",
            "name_en": "Falafel Wrap (Light)",
            "name_ar": "راب فلافل خفيف",
            "restaurant": "Shawarmer",
            "price_sar": 16.0,
            "est_macros": {
                "kcal": 340,
                "protein_g": 12,
                "refined_carbs_g": 40,
                "fiber_g": 8,
                "fat_g": 12,
                "sugar_g": 3,
            },
            "tags": ["vegetarian", "no_nuts", "light"],
            "prep_minutes": 22,
        },
        {
            "id": "meal_hummus_plate",
            "name_en": "Hummus & Veg Plate",
            "name_ar": "صحن حمص وخضار",
            "restaurant": "Najd Village Express",
            "price_sar": 15.0,
            "est_macros": {
                "kcal": 300,
                "protein_g": 10,
                "refined_carbs_g": 28,
                "fiber_g": 9,
                "fat_g": 14,
                "sugar_g": 4,
            },
            "tags": ["vegetarian", "no_nuts", "light", "pre_meeting"],
            "prep_minutes": 20,
        },
        {
            "id": "meal_tuna_sandwich",
            "name_en": "Tuna Wholegrain Sandwich",
            "name_ar": "ساندويتش تونة حبوب كاملة",
            "restaurant": "Paul Bakery",
            "price_sar": 24.0,
            "est_macros": {
                "kcal": 420,
                "protein_g": 26,
                "refined_carbs_g": 35,
                "fiber_g": 7,
                "fat_g": 14,
                "sugar_g": 4,
            },
            "tags": ["high_protein", "no_nuts"],
            "prep_minutes": 25,
        },
        {
            "id": "meal_protein_shake",
            "name_en": "Whey Protein Shake",
            "name_ar": "شيك بروتين",
            "restaurant": "GymNation Cafe",
            "price_sar": 22.0,
            "est_macros": {
                "kcal": 200,
                "protein_g": 30,
                "refined_carbs_g": 8,
                "fiber_g": 1,
                "fat_g": 3,
                "sugar_g": 5,
            },
            "tags": ["high_protein", "light", "pre_meeting", "no_nuts", "vegetarian"],
            "prep_minutes": 12,
        },
        {
            "id": "meal_mixed_nuts_skip",
            "name_en": "Mixed Nuts Cup",
            "name_ar": "كوب مكسرات مشكلة",
            "restaurant": "Tamimi Fresh",
            "price_sar": 15.0,
            "est_macros": {
                "kcal": 320,
                "protein_g": 10,
                "refined_carbs_g": 8,
                "fiber_g": 5,
                "fat_g": 26,
                "sugar_g": 4,
            },
            "tags": ["high_protein", "vegetarian"],
            "prep_minutes": 10,
        },
        {
            "id": "meal_fruit_plate",
            "name_en": "Seasonal Fruit Plate",
            "name_ar": "صحن فواكه موسمية",
            "restaurant": "Tamimi Fresh",
            "price_sar": 18.0,
            "est_macros": {
                "kcal": 180,
                "protein_g": 2,
                "refined_carbs_g": 10,
                "fiber_g": 6,
                "fat_g": 1,
                "sugar_g": 28,
            },
            "tags": ["light", "vegetarian", "no_nuts", "pre_meeting"],
            "prep_minutes": 15,
        },
        {
            "id": "meal_shakshuka",
            "name_en": "Shakshuka Bowl",
            "name_ar": "شكشوكة",
            "restaurant": "Urth Caffe",
            "price_sar": 32.0,
            "est_macros": {
                "kcal": 450,
                "protein_g": 22,
                "refined_carbs_g": 18,
                "fiber_g": 5,
                "fat_g": 28,
                "sugar_g": 8,
            },
            "tags": ["high_protein", "vegetarian", "no_nuts"],
            "prep_minutes": 30,
        },
        {
            "id": "meal_grilled_fish",
            "name_en": "Grilled Hammour Bites",
            "name_ar": "قطع هامور مشوي",
            "restaurant": "Catch",
            "price_sar": 38.0,
            "est_macros": {
                "kcal": 360,
                "protein_g": 34,
                "refined_carbs_g": 4,
                "fiber_g": 1,
                "fat_g": 18,
                "sugar_g": 1,
            },
            "tags": ["high_protein", "light", "no_nuts", "pre_meeting"],
            "prep_minutes": 35,
        },
        {
            "id": "meal_lentil_soup",
            "name_en": "Red Lentil Soup",
            "name_ar": "شوربة عدس",
            "restaurant": "Najd Village Express",
            "price_sar": 14.0,
            "est_macros": {
                "kcal": 240,
                "protein_g": 12,
                "refined_carbs_g": 28,
                "fiber_g": 9,
                "fat_g": 6,
                "sugar_g": 4,
            },
            "tags": ["vegetarian", "no_nuts", "light"],
            "prep_minutes": 20,
        },
        {
            "id": "meal_turkey_wrap",
            "name_en": "Turkey Avocado Wrap",
            "name_ar": "راب ديك رومي وأفوكادو",
            "restaurant": "Subway",
            "price_sar": 26.0,
            "est_macros": {
                "kcal": 410,
                "protein_g": 28,
                "refined_carbs_g": 32,
                "fiber_g": 6,
                "fat_g": 16,
                "sugar_g": 3,
            },
            "tags": ["high_protein", "no_nuts", "pre_meeting"],
            "prep_minutes": 22,
        },
        {
            "id": "meal_cottage_cheese",
            "name_en": "Cottage Cheese Cup",
            "name_ar": "جبن قريش",
            "restaurant": "Barn's",
            "price_sar": 13.0,
            "est_macros": {
                "kcal": 180,
                "protein_g": 22,
                "refined_carbs_g": 6,
                "fiber_g": 0,
                "fat_g": 5,
                "sugar_g": 4,
            },
            "tags": ["high_protein", "light", "pre_meeting", "vegetarian", "no_nuts"],
            "prep_minutes": 12,
        },
        {
            "id": "meal_veggie_bowl",
            "name_en": "Quinoa Veggie Bowl",
            "name_ar": "بول كينوا وخضار",
            "restaurant": "Salad Bar Co",
            "price_sar": 29.0,
            "est_macros": {
                "kcal": 390,
                "protein_g": 14,
                "refined_carbs_g": 20,
                "fiber_g": 10,
                "fat_g": 14,
                "sugar_g": 6,
            },
            "tags": ["vegetarian", "no_nuts", "light"],
            "prep_minutes": 28,
        },
        {
            "id": "meal_chicken_shawarma_light",
            "name_en": "Chicken Shawarma (Light Sauce)",
            "name_ar": "شاورما دجاج صوص خفيف",
            "restaurant": "Shawarmer",
            "price_sar": 20.0,
            "est_macros": {
                "kcal": 480,
                "protein_g": 28,
                "refined_carbs_g": 42,
                "fiber_g": 4,
                "fat_g": 18,
                "sugar_g": 5,
            },
            "tags": ["high_protein", "no_nuts"],
            "prep_minutes": 25,
        },
        {
            "id": "meal_oat_bar",
            "name_en": "Oat Protein Bar",
            "name_ar": "بار شوفان بروتين",
            "restaurant": "Tamimi Fresh",
            "price_sar": 11.0,
            "est_macros": {
                "kcal": 210,
                "protein_g": 15,
                "refined_carbs_g": 18,
                "fiber_g": 5,
                "fat_g": 8,
                "sugar_g": 7,
            },
            "tags": ["high_protein", "light", "pre_meeting", "vegetarian", "no_nuts"],
            "prep_minutes": 10,
        },
        {
            "id": "meal_arabic_coffee_dates",
            "name_en": "Arabic Coffee & 3 Dates",
            "name_ar": "قهوة عربية وثلاث تمرات",
            "restaurant": "Local Cafe",
            "price_sar": 10.0,
            "est_macros": {
                "kcal": 150,
                "protein_g": 1,
                "refined_carbs_g": 5,
                "fiber_g": 3,
                "fat_g": 0,
                "sugar_g": 28,
            },
            "tags": ["light", "vegetarian", "no_nuts", "pre_meeting"],
            "prep_minutes": 8,
        },
        {
            "id": "meal_smoothie_green",
            "name_en": "Green Spinach Smoothie",
            "name_ar": "سموذي سبانخ أخضر",
            "restaurant": "Juice Time",
            "price_sar": 19.0,
            "est_macros": {
                "kcal": 230,
                "protein_g": 8,
                "refined_carbs_g": 22,
                "fiber_g": 5,
                "fat_g": 6,
                "sugar_g": 18,
            },
            "tags": ["light", "vegetarian", "no_nuts", "pre_meeting"],
            "prep_minutes": 15,
        },
    ]
    for m in meals:
        m["url"] = "https://example.com/REPLACE_ME"
    assert len(meals) == 20
    return meals


def build_history(day: date) -> list[dict]:
    rows: list[dict] = []
    for d_offset in range(30, 0, -1):
        d = day - timedelta(days=d_offset)
        weekday = d.weekday()  # Mon=0 ... Thu=3
        for dept in DEPARTMENTS:
            base = 68 + random.randint(-5, 5)
            # Engineering often skips lunch -> slightly lower energy mid-afternoon pattern
            if dept == "Engineering":
                base -= 4
            # Thursday heaviest lunches
            if weekday == 3:
                base -= 8
            # afternoon dip reflected in lower energy
            energy = max(35, min(95, base + random.randint(-6, 6)))
            offered = random.randint(8, 22)
            accepted = int(offered * random.uniform(0.35, 0.7))
            rows.append(
                {
                    "date": d.isoformat(),
                    "department": dept,
                    "energy_index": energy,
                    "slumps_prevented": random.randint(2, 12),
                    "interventions_offered": offered,
                    "interventions_accepted": accepted,
                    "allowance_used_sar": round(random.uniform(80, 320), 1),
                    "employee_count": random.randint(5, 8),
                }
            )
    return rows


def main() -> None:
    today = date.today()
    employees = build_employees()
    calendars = build_calendars(employees, today)
    meals = build_meals()
    history = build_history(today)

    (SEED / "employees.json").write_text(
        json.dumps(employees, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (SEED / "calendars.json").write_text(
        json.dumps(calendars, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (SEED / "meals_catalog.json").write_text(
        json.dumps(meals, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (SEED / "history.json").write_text(
        json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote seed files for {today.isoformat()}")


if __name__ == "__main__":
    main()
