"""Spoken narration: the curator's words read aloud as the arm builds the exhibition.

Uses VeriArm's announce.speak (macOS `say`; VERIARM_VOICE=0 mutes and only prints). By default the arm
does NOT wait for speech: each artwork's reason plays while that tile is carried. wait=True waits after
every sentence instead, like VeriArm does (if the audio stutters while the arm moves).

  python -m art_curator.narrate          hear a sample narration (no robot)

`say` reads English only: text in another language falls back to a short English line from the catalog.
"""
import announce

ORDINAL = {1: "First", 2: "Next", 3: "And finally"}


def english(text: str) -> bool:
    """Mostly ASCII letters: something the English voice can read."""
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) > 0.9


class Narrator:
    def __init__(self, wait: bool = False):
        self.wait = wait

    def say(self, text: str) -> None:
        announce.speak(text, wait=self.wait)

    def curated(self, ex: dict) -> None:
        """The page just showed a new exhibition."""
        if english(ex["exhibition_title"] + ex["curatorial_statement"]):
            self.say(f"{ex['exhibition_title']}. {ex['curatorial_statement']}")
        else:
            self.say("Here is your exhibition.")

    def starting(self, ex: dict) -> None:
        title = ex["exhibition_title"] if english(ex["exhibition_title"]) else "your exhibition"
        self.say(f"Now assembling {title}.")

    def placing(self, pick: dict) -> None:
        """Called as the arm sets off for this artwork: the reason plays while it's carried."""
        intro = f"{ORDINAL.get(pick['position'], 'Then')}, {pick['title']}, by {pick['artist']}."
        self.say(f"{intro} {pick['reason']}" if english(pick["reason"]) else intro)

    def complete(self, ex: dict) -> None:
        """Ties the finished table back to the story: the title, then the path through the three works."""
        a, b, c = (p["title"] for p in ex["selected_artworks"])  # validated: positions 1, 2, 3 in order
        title = ex["exhibition_title"] if english(ex["exhibition_title"]) else "Your exhibition"
        self.say(f"{title}: from {a}, through {b}, to {c}. The exhibition is complete.")

    def failed(self, pick: dict) -> None:
        self.say(f"I couldn't place {pick['title']}. Please check the table.")


def sample() -> None:
    from art_curator.curator import curate
    ex = curate("joy", offline=True)
    n = Narrator(wait=True)
    n.curated(ex)
    n.starting(ex)
    for p in ex["selected_artworks"]:
        n.placing(p)
    n.complete(ex)


if __name__ == "__main__":
    sample()
