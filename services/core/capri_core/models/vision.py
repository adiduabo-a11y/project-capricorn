"""Does the configured model see images?

``screen.vision`` is a switch the user owns. ``auto`` is the default: it turns
visual context on for model names that are known to accept images and off for
everything else, so an install pointing at a text-only model keeps working
without a setting to find.
"""

from __future__ import annotations

# Substrings of model ids that accept images. Deliberately specific: a bare
# "vl" would match unrelated names, and guessing wrong means silently sending
# a screenshot to a model that cannot read it.
VISION_HINTS = (
    "-vl",
    "vl-",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "vision",
    "llava",
    "moondream",
    "minicpm-v",
    "pixtral",
    "internvl",
    "smolvlm",
    "gemma-3",
    "gemma3",
    "phi-4-multimodal",
)


def model_sees_images(model: str) -> bool:
    name = str(model or "").strip().lower()
    if not name:
        return False
    return any(hint in name for hint in VISION_HINTS)


# Requests that mean "use your eyes". Kept to phrases that read as looking at
# a screen: "read the README" must not attach a screenshot, and "look at this"
# must.
_LOOK_PHRASES = (
    "look at",
    "take a look",
    "have a look",
    "on my screen",
    "on the screen",
    "screenshot",
    "screen shot",
    "screengrab",
    "read the screen",
    "read my screen",
    "see the screen",
    "see my screen",
    "what do you see",
    "what can you see",
    "does it say",
    "watch my screen",
    "watch the screen",
)


def asks_to_look(text: str) -> bool:
    """Whether this request is about looking at the screen.

    Intent, not permission: the user asking to look is what allows an image to
    ride along, and a planner calling screen.observe is not the same thing.
    """
    body = " ".join(str(text or "").lower().split())
    if not body:
        return False
    return any(phrase in body for phrase in _LOOK_PHRASES)


def vision_enabled(mode: str, model: str) -> bool:
    """Resolve ``screen.vision`` against the configured model name."""
    choice = str(mode or "auto").strip().lower()
    if choice == "off":
        return False
    if choice == "on":
        return True
    return model_sees_images(model)
