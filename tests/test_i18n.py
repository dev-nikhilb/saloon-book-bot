"""Tests for i18n template loading."""
from app.messaging.i18n import t, reload_templates


class TestI18n:
    def setup_method(self):
        reload_templates()

    def test_english_template(self):
        result = t("welcome_new", lang="en", business_name="Style Studio")
        assert "Style Studio" in result
        assert "Welcome" in result

    def test_tamil_template(self):
        result = t("welcome_new", lang="ta", business_name="Style Studio")
        assert "Style Studio" in result
        assert "வரவேற்கிறோம்" in result

    def test_hindi_template(self):
        result = t("welcome_new", lang="hi", business_name="Style Studio")
        assert "Style Studio" in result
        assert "स्वागत" in result

    def test_fallback_to_english(self):
        result = t("welcome_new", lang="xx", business_name="Test")
        assert "Welcome" in result

    def test_missing_key_returns_marker(self):
        result = t("nonexistent_key", lang="en")
        assert "missing" in result

    def test_variable_substitution(self):
        result = t("invalid_choice", lang="en", max=5)
        assert "5" in result
