"""
One place for all LLM calls.

The rest of the code calls ask_llm_json(prompt) and gets a Python dict back.
Which provider answers is decided by LLM_PROVIDER in .env ("bedrock" or "gemini"),
so switching providers never requires touching the classifier or extractor.
"""
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()
PROVIDER = os.getenv("LLM_PROVIDER", "bedrock").lower()

_client = None  # created once on first use, then reused


def _get_client():
    global _client
    if _client is None:
        if PROVIDER == "bedrock":
            import boto3
            _client = boto3.client(
                "bedrock-runtime",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
        elif PROVIDER == "gemini":
            from google import genai
            _client = genai.Client()
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {PROVIDER}")
    return _client


def ask_llm(prompt, max_tokens=4096):
    """Send a prompt, get the model's reply as plain text."""
    client = _get_client()

    if PROVIDER == "bedrock":
        response = client.converse(
            modelId=os.getenv("BEDROCK_MODEL_ID"),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        )
        blocks = response["output"]["message"]["content"]
        return "".join(b["text"] for b in blocks if "text" in b)

    # Gemini
    from google.genai import types
    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL"),
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
            max_output_tokens=max_tokens,
        ),
    )
    return response.text


def parse_json(text):
    """Turn the model's reply into a dict, even if it added ``` fences or extra words."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise ValueError(f"Model did not return valid JSON: {text[:200]}")


def ask_llm_json(prompt, max_tokens=4096):
    """Send a prompt that asks for JSON, get a Python dict back."""
    reply = ask_llm(
        prompt + "\n\nRespond with the JSON only. No explanation, no markdown fences.",
        max_tokens=max_tokens,
    )
    return parse_json(reply)


if __name__ == "__main__":
    # Quick connection test: python app/llm.py
    print(f"Provider: {PROVIDER}")
    print(ask_llm_json('Return this JSON exactly: {"status": "ok"}'))