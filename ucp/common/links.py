"""Links the agent hands to the user arrive with whatever punctuation was written around them."""


def trim_link(value: str) -> str:
    """Drop punctuation stuck to the end of a link's last part. No id or state of ours ends in any of these."""
    return value.rstrip(")]}>.,;:!?'\"`* ") if isinstance(value, str) else value
