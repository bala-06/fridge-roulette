import base64
import json
import os
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
}"""

DIETS = {
    "veg": "The dish must be vegetarian.",
    "nonveg": "Meat, fish, or egg is welcome.",
    "any": "Vegetarian or not, whatever suits the ingredients.",
}


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
        raw = _ask_nova(prompt)
    except ClientError as e:
        print(f"bedrock error: {e}")
        return _resp(502, {"error": "The kitchen is busy. Try again in a moment."})

    try:
        recipe = _clean(_extract_json(raw))
    except Exception as e:
        print(f"parse error: {e} | raw={raw[:400]}")
        return _resp(502, {"error": "Could not read the recipe. Try again."})

    recipe["id"] = uuid.uuid4().hex[:8]
    _save(recipe, ingredients)
    return _resp(200, recipe)


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
