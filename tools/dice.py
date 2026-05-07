import random

from pydantic import BaseModel, Field

from registry import tool


class _RollDieArgs(BaseModel):
    sides: int = Field(description="Number of sides on the die")


@tool(
    description="Roll a die with the specified number of sides (e.g. 6 for a standard die, 20 for a d20).",
    args=_RollDieArgs,
)
def roll_die(sides: int) -> str:
    result = random.randint(1, sides)
    return f"Rolled a d{sides}: {result}"
