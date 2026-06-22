"""Simple math captcha for anti-bot verification."""

import random
from dataclasses import dataclass


@dataclass
class CaptchaChallenge:
    question: str
    answer: int


def generate_captcha() -> CaptchaChallenge:
    """Generate a simple math captcha."""
    ops = [
        ("add", "+"),
        ("sub", "-"),
        ("mul", "×"),
    ]
    op_type, op_symbol = random.choice(ops)

    if op_type == "add":
        a = random.randint(1, 50)
        b = random.randint(1, 50)
        answer = a + b
    elif op_type == "sub":
        a = random.randint(10, 99)
        b = random.randint(1, a)
        answer = a - b
    else:  # mul
        a = random.randint(2, 12)
        b = random.randint(2, 12)
        answer = a * b

    question = f"{a} {op_symbol} {b} = ?"
    return CaptchaChallenge(question=question, answer=answer)


def verify_captcha(user_answer: str, correct_answer: int) -> bool:
    """Verify a captcha answer."""
    try:
        return int(user_answer.strip()) == correct_answer
    except (ValueError, AttributeError):
        return False
