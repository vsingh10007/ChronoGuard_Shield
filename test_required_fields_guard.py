
from app.shield.required_fields_guard import RequiredFieldsGuard

# Test 1: All required fields present
result = RequiredFieldsGuard.validate(
    {"name": "Test User", "email": "test@example.com"},
    ("name", "email"),
)
assert result["valid"] is True
print("TEST 1 PASSED: Valid request")

# Test 2: Email missing
result = RequiredFieldsGuard.validate(
    {"name": "Test User"},
    ("name", "email"),
)
assert result["valid"] is False
assert "email" in result["missing_fields"]
print("TEST 2 PASSED: Missing email rejected")

# Test 3: Email empty
result = RequiredFieldsGuard.validate(
    {"name": "Test User", "email": "   "},
    ("name", "email"),
)
assert result["valid"] is False
print("TEST 3 PASSED: Empty email rejected")

# Test 4: Email has wrong type
result = RequiredFieldsGuard.validate(
    {"name": "Test User", "email": 123},
    ("name", "email"),
)
assert result["valid"] is False
assert "email" in result["invalid_fields"]
print("TEST 4 PASSED: Incorrect field type rejected")

print("\nAll RequiredFieldsGuard tests passed.")
