"""Simple math captcha for anti-bot verification."""

import random
from dataclasses import dataclass


@dataclass
class CaptchaChallenge:
    question: str
    answer: int


def generate_captcha() -> CaptchaChallenge:
    """Generate a very simple math captcha (1-digit numbers, + or - only)."""
    op_type = random.choice(["+", "-"])

    if op_type == "+":
        a = random.randint(1, 9)
        b = random.randint(1, 9)
        answer = a + b
    else:
        a = random.randint(2, 9)
        b = random.randint(1, a - 1)  # ensure positive result
        answer = a - b

    question = f"{a} {op_type} {b} = ?"
    return CaptchaChallenge(question=question, answer=answer)


def verify_captcha(user_answer: str, correct_answer: int) -> bool:
    """Verify a captcha answer."""
    try:
        return int(user_answer.strip()) == correct_answer
    except (ValueError, AttributeError):
        return False
