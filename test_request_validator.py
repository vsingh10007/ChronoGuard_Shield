
from app.request_validator import validate_request_data


# Test 1: Valid request.
valid_result = validate_request_data({
    "name": "ChronoGuard Test",
    "email": "test@example.com",
    "timestamp": "2026-10-26T00:00:00Z",
})

print("TEST 1 - Valid request")
print(valid_result)
assert valid_result["valid"] is True


# Test 2: Missing email must be rejected.
missing_email_result = validate_request_data({
    "name": "Missing Email Test",
    "timestamp": "2026-10-26T00:00:00Z",
})

print("\nTEST 2 - Missing email")
print(missing_email_result)
assert missing_email_result["valid"] is False
assert any(
    "email" in error.lower()
    for error in missing_email_result["errors"]
)


# Test 3: Invalid email must be rejected.
invalid_email_result = validate_request_data({
    "name": "Invalid Email Test",
    "email": "not-an-email",
    "timestamp": "2026-10-26T00:00:00Z",
})

print("\nTEST 3 - Invalid email")
print(invalid_email_result)
assert invalid_email_result["valid"] is False


# Test 4: Missing timestamp is corrected,
# but a valid request remains valid.
missing_timestamp_result = validate_request_data({
    "name": "Timestamp Test",
    "email": "test@example.com",
})

print("\nTEST 4 - Missing timestamp")
print(missing_timestamp_result)
assert missing_timestamp_result["valid"] is True
assert missing_timestamp_result["cleaned_data"][
    "timestamp_correction"
]["corrected"] is True


# Test 5: A missing email remains invalid even if
# the missing timestamp is corrected.
both_missing_result = validate_request_data({
    "name": "Combined Test",
})

print("\nTEST 5 - Missing email and timestamp")
print(both_missing_result)
assert both_missing_result["valid"] is False
assert "email" not in both_missing_result["cleaned_data"]


print("\nAll validator tests passed.")
