class InputGuard:
    SMALL_NUMBERS = {
        "zero": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
    }

    TENS = {
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
    }

    @classmethod
    def parse_age(cls, value):
        if isinstance(value, bool):
            return {"valid": False, "error": "Age must be a number"}

        if isinstance(value, int):
            age = value

        elif isinstance(value, str):
            text = value.strip().lower()

            try:
                age = int(text)
            except ValueError:
                words = text.replace("-", " ").split()

                if len(words) == 1 and words[0] in cls.SMALL_NUMBERS:
                    age = cls.SMALL_NUMBERS[words[0]]

                elif len(words) == 1 and words[0] in cls.TENS:
                    age = cls.TENS[words[0]]

                elif (
                    len(words) == 2
                    and words[0] in cls.TENS
                    and words[1] in cls.SMALL_NUMBERS
                    and 1 <= cls.SMALL_NUMBERS[words[1]] <= 9
                ):
                    age = cls.TENS[words[0]] + cls.SMALL_NUMBERS[words[1]]

                else:
                    return {
                        "valid": False,
                        "error": "Age must be numeric or a supported number word",
                    }

        else:
            return {"valid": False, "error": "Invalid age type"}

        if not 0 <= age <= 130:
            return {
                "valid": False,
                "error": "Age must be between 0 and 130",
            }

        return {"valid": True, "age": age}