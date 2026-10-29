from collections import namedtuple

Transition = namedtuple("Transition", ("target", "label"))

IDEA_TRANSITIONS = {
    "new": [Transition("planned", "Plan"), Transition("abandoned", "Abandon")],
    "planned": [Transition("verify", "Ready to verify"), Transition("abandoned", "Abandon")],
    "verify": [
        Transition("implemented", "Verified"),
        Transition("planned", "Reopen"),
        Transition("abandoned", "Abandon"),
    ],
    "implemented": [Transition("planned", "Reopen")],
    "abandoned": [Transition("planned", "Reopen")],
}

# An idea whose needs_verification is off skips verify: finishing it closes it.
IDEA_TRANSITIONS_NO_VERIFY = {
    **IDEA_TRANSITIONS,
    "planned": [Transition("implemented", "Done"), Transition("abandoned", "Abandon")],
}

PROJECT_TRANSITIONS = {
    "idea": [Transition("defined", "Define"), Transition("canceled", "Cancel")],
    "defined": [Transition("implemented", "Implemented"), Transition("canceled", "Cancel")],
    "implemented": [Transition("discontinued", "Discontinue")],
    "discontinued": [],
    "canceled": [],
}
