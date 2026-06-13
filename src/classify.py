#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optional merchant classification with a local LLM (Ollama).

Two cooperating layers, both entirely optional and both gated on
``cfg["ollama"]["enabled"]``:

  1. **Chat classification** (:func:`classify`): ask a local Ollama chat model
     to pick one category (and optionally one label) for a merchant. The model
     only ever *proposes*; this module *validates* the proposal against the
     hard whitelist built from the user's own ``cfg["categories"]`` and
     ``cfg["labels"]``. If the model invents a category that isn't in the
     config, the proposal is rejected and ``None`` is returned -- the pipeline
     then falls back to its keyword rules / pending label.

  2. **Embedding classification** (:func:`embed_classify`): for a merchant that
     rules and chat couldn't resolve, embed the merchant name with a small
     local embedding model and find the nearest *known* merchant from a caller-
     supplied list, inheriting its category when the cosine similarity clears a
     configurable threshold. This is a cheap reinforcement, not a replacement,
     and it likewise validates against the whitelist.

The whole module degrades gracefully: if Ollama is unreachable, disabled, or
returns junk, every public function simply returns ``None`` and the caller
proceeds without LLM help. Only the Python standard library is used.
"""
import json
import math
import urllib.request


# ---- transport ----------------------------------------------------------

def _ollama_cfg(cfg):
    """Return the ``ollama`` sub-config with sane defaults."""
    o = (cfg or {}).get("ollama", {}) or {}
    return {
        "enabled": bool(o.get("enabled", False)),
        "url": (o.get("url", "http://localhost:11434") or "").rstrip("/"),
        "classify_model": o.get("classify_model", "qwen2.5:7b"),
        "rescue_model": o.get("rescue_model", o.get("classify_model", "qwen2.5:7b")),
        "embed_model": o.get("embed_model", "all-minilm"),
        "embed_threshold": float(o.get("embed_threshold", 0.66)),
    }


def available(cfg, timeout=3):
    """Return True if the configured Ollama server answers ``/api/tags``.

    Never raises -- a dead or absent server simply yields ``False`` so the
    caller can skip the LLM tier silently.
    """
    o = _ollama_cfg(cfg)
    if not o["enabled"]:
        return False
    try:
        with urllib.request.urlopen(o["url"] + "/api/tags", timeout=timeout) as r:
            json.loads(r.read())
        return True
    except Exception:
        return False


def chat_json(system, user, model, cfg, timeout=120):
    """One ``system`` + ``user`` round-trip to Ollama, forcing JSON output.

    Uses ``format=json`` and ``temperature=0`` so the result is deterministic
    and machine-parseable. The model only proposes; validation happens in the
    caller.

    Args:
        system: system prompt.
        user: user prompt.
        model: model name to use (falls back to ``classify_model`` if falsy).
        cfg: loaded config dict (for the Ollama URL).
        timeout: per-request timeout in seconds.

    Returns:
        dict | None: the parsed JSON object, or ``None`` on any failure
        (network, timeout, disabled, or invalid JSON).
    """
    o = _ollama_cfg(cfg)
    if not o["enabled"]:
        return None
    body = {
        "model": model or o["classify_model"],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        o["url"] + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        return json.loads(resp["message"]["content"])
    except Exception:
        return None


# ---- whitelist helpers ---------------------------------------------------

def _noise_categories(cfg):
    """Categories that must never be auto-assigned (transfers, system, etc.)."""
    return set((cfg or {}).get(
        "noise_categories",
        ["Transfer", "Loan, interests", "Others", "Uncategorized"],
    ))


def _expense_categories(cfg):
    """The whitelist of category names the LLM may choose from.

    Built straight from the user's ``cfg["categories"]`` keys, minus the noise
    categories. This is the *hard* whitelist: anything the model returns that
    isn't here is rejected.
    """
    cats = (cfg or {}).get("categories", {}) or {}
    noise = _noise_categories(cfg)
    return [c for c in cats if c not in noise]


def _label_names(cfg):
    """The whitelist of label names the LLM may choose from.

    Excludes the operational labels (auto-captured / pending / usd) so the
    model picks a *semantic* taxonomy label, not a workflow marker.
    """
    labels = (cfg or {}).get("labels", {}) or {}
    operational = {
        cfg.get("autocaptured_label"),
        cfg.get("pending_label"),
        cfg.get("usd_label"),
    }
    return [name for name in labels if name not in operational]


# ---- chat classification -------------------------------------------------

def classify(merchant, amount, cfg):
    """Propose a (category, label) for ``merchant`` via the local LLM.

    The model is asked to choose exactly one category and one label from the
    user's own whitelists. The proposal is then hard-validated:

      * an invented / out-of-whitelist *category* -> the whole proposal is
        rejected and ``None`` is returned;
      * an invented / out-of-whitelist *label* -> the label is dropped to
        ``None`` (category may still stand).

    Args:
        merchant: merchant / counterparty name (raw is fine).
        amount: transaction amount (sign is informative; abs is shown to model).
        cfg: loaded config dict.

    Returns:
        tuple[str, str | None] | None: ``(category, label_or_None)`` when the
        model returns a valid, whitelisted category; otherwise ``None``.
    """
    if not available(cfg):
        return None
    cats = _expense_categories(cfg)
    labels = _label_names(cfg)
    if not cats:
        return None

    o = _ollama_cfg(cfg)
    currency = (cfg or {}).get("currency", "COP")
    system = (
        "You are a personal-expense classifier. Classify the transaction into "
        "exactly ONE category and ONE label, choosing EXCLUSIVELY from these "
        "lists. If nothing fits clearly, use category \"Others\" and label "
        "null.\n"
        f"CATEGORIES: {', '.join(cats)}\n"
        f"LABELS: {', '.join(labels) if labels else '(none configured)'}\n"
        "Payment-gateway prefixes such as BOLD*, PAYU*, MERPAGO* are not the "
        "real merchant: the real name follows the '*'.\n"
        "Respond ONLY with JSON: "
        "{\"category\":\"<from the list>\",\"label\":\"<from the list or null>\","
        "\"confidence\":0.0-1.0}"
    )
    user = f"Merchant: {merchant!r}\nAmount: {abs(amount):,.2f} {currency}"

    out = chat_json(system, user, o["classify_model"], cfg)
    if not isinstance(out, dict):
        return None

    category = (out.get("category") or "").strip()
    label = (out.get("label") or "").strip()

    # Hard whitelist: an invented category fails the whole proposal.
    if category not in (cfg.get("categories", {}) or {}):
        return None
    if category in _noise_categories(cfg):
        return None
    # An invented label is simply dropped (caller falls back to pending label).
    if label and label not in (cfg.get("labels", {}) or {}):
        label = None

    return category, (label or None)


# ---- embedding classification (optional reinforcement) -------------------

def _embed(texts, cfg, timeout=120):
    """Embed a list of strings with the configured Ollama embedding model.

    Returns a list of vectors aligned with ``texts``, or ``None`` on failure.
    """
    o = _ollama_cfg(cfg)
    body = json.dumps({
        "model": o["embed_model"],
        "input": [t if (t and t.strip()) else "?" for t in texts],
    }).encode("utf-8")
    req = urllib.request.Request(
        o["url"] + "/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["embeddings"]
    except Exception:
        return None


def _cosine(a, b):
    """Cosine similarity between two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


def embed_classify(merchant, known_merchants, cfg):
    """Inherit a category from the nearest known merchant, by embedding.

    For each entry in ``known_merchants`` the caller provides its name and the
    category (and optionally label) to inherit. The merchant is embedded and
    compared to every known name; if the best cosine similarity clears
    ``cfg["ollama"]["embed_threshold"]`` and the inherited category is on the
    whitelist (not noise), the inheritance is returned.

    Args:
        merchant: merchant name to classify.
        known_merchants: list of dicts, each
            ``{"name": str, "category": str, "label": str | None}``.
        cfg: loaded config dict.

    Returns:
        tuple[str, str | None, float, str] | None:
            ``(category, label_or_None, similarity, nearest_name)`` on a hit,
            else ``None`` (including when disabled or below threshold).
    """
    if not available(cfg) or not merchant or not merchant.strip():
        return None
    if not known_merchants:
        return None

    o = _ollama_cfg(cfg)
    names = [m.get("name", "") for m in known_merchants]
    vectors = _embed([merchant] + names, cfg)
    if not vectors or len(vectors) != len(names) + 1:
        return None

    query_vec = vectors[0]
    best_score = -1.0
    best = None
    for m, vec in zip(known_merchants, vectors[1:]):
        score = _cosine(query_vec, vec)
        if score > best_score:
            best_score = score
            best = m

    if best is None or best_score < o["embed_threshold"]:
        return None

    category = best.get("category")
    # Validate the inherited category against the whitelist + noise filter.
    if category not in (cfg.get("categories", {}) or {}):
        return None
    if category in _noise_categories(cfg):
        return None

    label = best.get("label")
    if label and label not in (cfg.get("labels", {}) or {}):
        label = None

    return category, (label or None), round(best_score, 3), best.get("name", "")
