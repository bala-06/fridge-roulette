import base64
import json
import os
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError

MODEL_ID = os.environ.get("MODEL_ID", "apac.amazon.nova-lite-v1:0")
TABLE_NAME = os.environ.get("TABLE_NAME")  # optional; skip saving if unset
MAX_INGREDIENTS_CHARS = 500

bedrock = boto3.client("bedrock-runtime")
table = boto3.resource("dynamodb").Table(TABLE_NAME) if TABLE_NAME else None

CORS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}

SYSTEM = """You invent one simple dish from whatever ingredients someone has left over.
Assume a basic kitchen: salt, pepper, oil, water, and common spices are always available.
Never use a main ingredient the user does not have unless you list it under "missing".

Reply with ONLY a JSON object. No markdown, no code fences, no commentary.
Use exactly this shape:
{
  "name": "playful dish name, max 5 words",
  "time_mins": 20,
  "serves": 2,
  "steps": ["one sentence per step, 4 to 6 steps"],
  "missing": ["anything needed that the user did not list"],
  "substitutions": ["swap X for Y if you have no X"],
  "note": "one warm sentence about the dish"
}

The diet the user asks for is absolute. If they ask for vegetarian, no meat, fish,
seafood or egg may appear in the name, the steps or the substitutions — not even a
little, not even if they listed it, and not even if you call the dish vegetarian.
Cook with what is left instead.

Sometimes there is no honest dish to give back. That happens when the user listed no
real ingredient at all ("nothing", "my fridge is empty"), or when every usable
ingredient is ruled out by the diet they asked for — someone with only chicken and
fish who asked for vegetarian. Do not invent a dish to fill the silence, and do not
build one around an ingredient you moved to "missing". Say so instead, with ONLY this
shape:
{
  "no_recipe": true,
  "reason": "one warm, specific sentence naming what is missing or why it cannot work"
}"""

DIETS = {
    "veg": (
        "The dish must be vegetarian. Meat, fish, seafood and egg are forbidden even "
        "though I listed them — leave them in the fridge and cook with the rest. "
        "Calling a dish vegetarian does not make it vegetarian."
    ),
    "nonveg": "Meat, fish, or egg is welcome.",
    "any": "Vegetarian or not, whatever suits the ingredients.",
}

# Last line of defence for the veg promise: Nova will happily title a dish "Vegetarian
# Keema" and then cook the keema. Word-bounded so eggplant is not mistaken for egg.
NON_VEG = re.compile(
    r"\b(meat|mutton|lamb|beef|pork|ham|bacon|sausage|salami|chicken|turkey|duck|"
    r"keema|kheema|qeema|mince|fish|tuna|salmon|sardine|anchovy|anchovies|prawn|prawns|"
    r"shrimp|crab|lobster|squid|calamari|clam|clams|mussel|mussels|oyster|oysters|"
    r"egg|eggs|omelette|omelet|gelatin|gelatine|lard)\b",
    re.I,
)


def lambda_handler(event, context):
    if _method(event) == "OPTIONS":
        return _resp(204, "")

    try:
        body = _parse_body(event)
    except Exception:
        return _resp(400, {"error": "Body must be valid JSON."})

    ingredients = str(body.get("ingredients") or "").strip()
    if not ingredients:
        return _resp(400, {"error": "Tell me what is in your fridge."})
    if len(ingredients) > MAX_INGREDIENTS_CHARS:
        return _resp(400, {"error": f"Keep it under {MAX_INGREDIENTS_CHARS} characters."})

    time_budget = body.get("time_budget", 20)
    time_budget = time_budget if time_budget in (10, 20, 40) else 20
    diet = body.get("diet", "any")
    diet = diet if diet in DIETS else "any"
    use_it_up = bool(body.get("use_it_up"))

    prompt = f"""I have: {ingredients}

I have {time_budget} minutes. {DIETS[diet]}"""
    if use_it_up:
        prompt += "\nPrioritise the ingredients most likely to spoil first."

    try:
        result = _cook(prompt)
        # Nova ignores the veg rule often enough that asking nicely is not enough.
        # Name the violation and give it one more go before giving up on the dish.
        if isinstance(result, dict) and diet == "veg":
            offender = _non_veg_word(result)
            if offender:
                print(f"veg violation: {offender} — retrying")
                result = _cook(
                    prompt
                    + f"\n\nYour last attempt used {offender}. That is not vegetarian."
                    " Cook only with the vegetarian ingredients I listed, or tell me"
                    " there is no vegetarian dish."
                )
                if isinstance(result, dict) and _non_veg_word(result):
                    print(f"veg violation again: {_non_veg_word(result)} — refusing")
                    result = (
                        "The only things here I could build a dish around are not "
                        "vegetarian. Add a vegetable or two and I will try again."
                    )
    except ClientError as e:
        print(f"bedrock error: {e}")
        return _resp(502, {"error": "The kitchen is busy. Try again in a moment."})
    except Exception as e:
        print(f"parse error: {e}")
        return _resp(502, {"error": "Could not read the recipe. Try again."})

    # "I cannot cook this" is a real answer, not a failure. Send it back as a 200 so
    # the UI can explain itself instead of showing a retry that would fail the same way.
    if isinstance(result, str):
        print(f"no recipe: {result}")
        return _resp(200, {"no_recipe": True, "reason": result})

    recipe = result
    recipe["id"] = uuid.uuid4().hex[:8]
    _save(recipe, ingredients)
    return _resp(200, recipe)


def _cook(prompt):
    """Return a cleaned recipe dict, or a reason string if there is no dish to give."""
    raw = _ask_nova(prompt)
    data = _extract_json(raw)
    refusal = _refusal_reason(data)
    if refusal:
        return refusal
    try:
        return _clean(data)
    except Exception:
        print(f"clean failed | raw={raw[:400]!r}")
        raise


def _non_veg_word(recipe):
    """The first non-vegetarian word appearing where it would be eaten, or None.

    'missing' is deliberately not searched — listing what you would need is fine.
    """
    haystack = " ".join(
        [str(recipe.get("name") or ""), str(recipe.get("note") or "")]
        + [str(s) for s in recipe.get("steps") or []]
        + [str(s) for s in recipe.get("substitutions") or []]
    )
    found = NON_VEG.search(haystack)
    return found.group(0).lower() if found else None


def _ask_nova(prompt):
    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 800, "temperature": 0.8, "topP": 0.9},
    )
    usage = resp.get("usage", {})
    print(f"tokens in={usage.get('inputTokens')} out={usage.get('outputTokens')}")
    return resp["output"]["message"]["content"][0]["text"]


def _extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start : end + 1])


PLACEHOLDERS = {"", "-", "n/a", "na", "none", "missing", "null", "unknown", "no recipe"}
DEFAULT_REASON = (
    "There is nothing here I can honestly cook with. Add an ingredient or two and I will try again."
)


def _refusal_reason(data):
    """Return a reason string if the model declined to invent a dish, else None.

    Nova says no in three different ways at temperature 0.8: the documented
    no_recipe shape, an empty steps list, or a recipe whose every field is the
    literal word "missing". All three used to surface as a parse error.
    """
    if data.get("no_recipe"):
        return _first_sentence(data, DEFAULT_REASON)

    steps = data.get("steps")
    steps = steps if isinstance(steps, list) else [steps]
    real = [s for s in steps if str(s).strip().lower().strip(".") not in PLACEHOLDERS]
    if not real:
        return _first_sentence(data, DEFAULT_REASON)

    if str(data.get("name") or "").strip().lower() in PLACEHOLDERS:
        return _first_sentence(data, DEFAULT_REASON)

    return None


def _first_sentence(data, fallback):
    for key in ("reason", "note", "error"):
        text = str(data.get(key) or "").strip()
        if text and text.lower() not in PLACEHOLDERS:
            return text
    return fallback


def _clean(data):
    """Never trust the model's shape. Coerce everything to what the UI expects."""
    def as_list(value):
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        return [str(value)] if value else []

    def as_int(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    steps = as_list(data.get("steps"))
    if not steps:
        raise ValueError("model returned no steps")

    return {
        "name": str(data.get("name") or "Fridge Surprise").strip(),
        "time_mins": as_int(data.get("time_mins"), 20),
        "serves": as_int(data.get("serves"), 2),
        "steps": steps,
        "missing": as_list(data.get("missing")),
        "substitutions": as_list(data.get("substitutions")),
        "note": str(data.get("note") or "").strip(),
    }


def _save(recipe, ingredients):
    if not table:
        return
    try:
        table.put_item(
            Item={
                "id": recipe["id"],
                "ingredients": ingredients,
                "recipe": json.dumps(recipe),
                "created_at": int(time.time()),
            }
        )
    except ClientError as e:
        print(f"dynamodb save failed (ignored): {e}")


def _method(event):
    return event.get("requestContext", {}).get("http", {}).get("method", "POST")


def _parse_body(event):
    raw = event.get("body")
    if raw is None:
        return event  # console test events post the fields directly
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


def _resp(status, payload):
    return {
        "statusCode": status,
        "headers": CORS,
        "body": payload if isinstance(payload, str) else json.dumps(payload),
    }
