"""行级差异分类单元测试 - 管理台按「新增绿 / 修改黄 / 删除红」着色靠的就是这一层

关心三件事：
- 同一块里成对出现的旧行与新行必须算「修改」，不能拆成「一删一增」两条颜色
- 只剩一侧的行算「新增」或「删除」，另一侧留空
- 每一行都要带上「它属于哪一段」，界面才有位置这一列
"""

import pytest

from openai_proxy.config_store.line_diff import ADD, MODIFY, REMOVE, LineDiff

CONFIG = """example-platform:
  baseUrl: "https://api.example.com/v1"
  weight: 10 # 优先级最高
  timeout: 120
  models:
    - "model-a"
"""


def _rows(before: str, after: str):
    return LineDiff(before, after).rows()


class Test行级差异分类:
    @pytest.mark.unit
    def test_完全一样时没有差异行(self):
        assert _rows(CONFIG, CONFIG) == []

    @pytest.mark.unit
    def test_成对出现的行算修改而不是删除加新增(self):
        rows = _rows(CONFIG, CONFIG.replace("weight: 10", "weight: 3"))

        assert [row["kind"] for row in rows] == [MODIFY]
        assert rows[0]["before"].startswith("weight: 10")
        assert rows[0]["after"].startswith("weight: 3")
        # 行尾注释也是这一行的一部分：改了取值时注释要一起显示出来
        assert "#" in rows[0]["before"]

    @pytest.mark.unit
    def test_只有新行算新增(self):
        after = CONFIG.replace('    - "model-a"', '    - "model-a"\n    - "model-b"')

        rows = _rows(CONFIG, after)

        assert [row["kind"] for row in rows] == [ADD]
        assert rows[0]["before"] == ""
        assert "model-b" in rows[0]["after"]

    @pytest.mark.unit
    def test_只有旧行算删除(self):
        rows = _rows(CONFIG, CONFIG.replace("  timeout: 120\n", ""))

        assert [row["kind"] for row in rows] == [REMOVE]
        assert "timeout: 120" in rows[0]["before"]
        assert rows[0]["after"] == ""

    @pytest.mark.unit
    def test_替换块两侧不等长时多出来的行单独分类(self):
        # 对象写法改成字符串写法：两行换成一行，多出来的那一行要单独标成删除
        before = CONFIG.replace('    - "model-a"', '    - name: "model-a"\n      timeout: 30')
        after = CONFIG.replace('    - "model-a"', '    - "model-a"')

        kinds = [row["kind"] for row in _rows(before, after)]

        assert MODIFY in kinds
        assert REMOVE in kinds
        assert len(kinds) == 2

    @pytest.mark.unit
    def test_每一行都带上所属的段名(self):
        rows = _rows(CONFIG, CONFIG.replace("weight: 10", "weight: 3").replace('    - "model-a"', '    - "model-z"'))

        assert rows
        # 改 weight 属于 example-platform 段，改模型属于 models 段
        assert rows[0]["context"] == "example-platform:"
        assert rows[1]["context"] == "models:"

    @pytest.mark.unit
    def test_段名过长时截断但不留空(self):
        long_key = "x" * 120
        rows = _rows(f"{long_key}:\n  weight: 10\n", f"{long_key}:\n  weight: 3\n")

        assert rows[0]["context"].endswith("…")
        assert len(rows[0]["context"]) < len(long_key)
