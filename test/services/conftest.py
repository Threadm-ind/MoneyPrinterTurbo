import pytest

from app.config import config


@pytest.fixture(autouse=True)
def _force_full_webui_mode():
    """WebUI AppTest 用例断言完整四栏布局；本地 config.toml 打开简洁模式时
    面板控件不会渲染，这里统一固定为完整模式，保证测试与本地配置解耦。"""
    original = config.ui.get("simple_mode", False)
    config.ui["simple_mode"] = False
    yield
    config.ui["simple_mode"] = original
