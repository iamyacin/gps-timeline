import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fresh_config_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    yield
